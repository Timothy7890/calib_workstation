"""18005 标定工作站后端：统一相机、标定、运动编排、产物归档与云端同步。"""

from __future__ import annotations

import json
import logging
import asyncio
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .clients import HttpClient, ServiceError
from .cloud import CloudError, CloudSettings, CloudSync
from .config import CAMERA_ROLES, UNIT_CODE_RE, Config, validate_unit_code
from .manifest import ARTIFACT_TYPES, ArtifactStore
from .calib2d import Calib2DEngine
from .camera import CameraManager, MockSource, OrbbecSource, discover_orbbec
from .calib3d import app as calib3d_app
from .calib3d import mount_api as calib3d_mount
from .calib3d.runtime import configure as configure_calib3d

ARMS = ("left", "right")
# 运行名：允许中文等 Unicode 字母/数字、. _ -；不能有空格、斜杠，不能以 . 开头（与 18004 一致）
_RUN_NAME_RE = re.compile(r"^[^\W.][\w.-]{0,63}$")
# 相机位置或手部 subject_key（目录名）：小写字母/数字/下划线/连字符
_ROLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_CALIB_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class Job:
    """当前向导会话（一次 2D 标定从选相机到生效）。进程内单例，落盘以便刷新/重启恢复。"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **fields: Any) -> dict[str, Any]:
        with self.lock:
            self.data.update(fields)
            self.data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return dict(self.data)

    def reset(self) -> dict[str, Any]:
        with self.lock:
            self.data = {}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("{}\n", encoding="utf-8")
            return {}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


class Workspace:
    """当前操作的机器人。编号由页面输入并持久化到 <data_root>/workstation_state.json，
    切换编号即切换产物目录 <data_root>/<unit_code>/ 与向导任务。"""

    BUSY_STEPS = {"engaged", "running", "solving"}

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.unit_code: str | None = None
        self.store: ArtifactStore | None = None
        self.job: Job | None = None
        self.set_at: str | None = None
        # 相机位置 → 记住的序列号（按机器人，首次标定选择后记住，可重选）
        self.cameras: dict[str, dict[str, Any]] = {}
        self.cameras_path: Path | None = None
        try:
            saved = json.loads(config.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("unit_code"):
            try:
                self._activate(validate_unit_code(saved["unit_code"]), persist=False)
                self.set_at = saved.get("set_at")
            except ValueError:
                pass

    def _activate(self, unit_code: str, *, persist: bool) -> None:
        root = self.config.unit_root(unit_code)
        root.mkdir(parents=True, exist_ok=True)
        self.store = ArtifactStore(
            root / "calibrations",
            unit_code=unit_code, vendor=self.config.vendor, model=self.config.model,
            tool_projects={"hand_eye_2D": _CALIB_ROOT / "hand_eye_2D"},
        )
        self.job = Job(root / "state" / "current_job.json")
        self.cameras_path = root / "state" / "cameras.json"
        try:
            saved = json.loads(self.cameras_path.read_text(encoding="utf-8"))
            self.cameras = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            self.cameras = {}
        self.unit_code = unit_code
        if persist:
            self.set_at = datetime.now().isoformat(timespec="seconds")
            self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.state_path.write_text(
                json.dumps({"unit_code": unit_code, "set_at": self.set_at}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def select(self, unit_code: str) -> None:
        unit_code = validate_unit_code(unit_code)
        with self.lock:
            if self.job is not None and self.job.snapshot().get("step") in self.BUSY_STEPS:
                raise ValueError("当前标定任务正在进行（已接管/运行/求解中），请先停止或完成后再切换机器人")
            self._activate(unit_code, persist=True)

    def camera_serial(self, role: str) -> str | None:
        return (self.cameras.get(role) or {}).get("serial")

    def role_for_serial(self, serial: str) -> str | None:
        for role, entry in self.cameras.items():
            if entry.get("serial") == serial:
                return role
        return None

    def remember_camera(self, role: str, serial: str, name: str | None = None) -> dict[str, Any]:
        """记住 role 用哪台相机；同一序列号不能同时是头部又是腰部，会从旧位置移除。"""
        with self.lock:
            if self.cameras_path is None:
                raise ValueError("还没有设置机器人编号")
            for other, entry in list(self.cameras.items()):
                if other != role and entry.get("serial") == serial:
                    del self.cameras[other]
            self.cameras[role] = {
                "serial": serial, "name": name or "",
                "set_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save_cameras()
            return dict(self.cameras)

    def forget_camera(self, role: str) -> dict[str, Any]:
        with self.lock:
            if self.cameras_path is None:
                raise ValueError("还没有设置机器人编号")
            self.cameras.pop(role, None)
            self._save_cameras()
            return dict(self.cameras)

    def _save_cameras(self) -> None:
        assert self.cameras_path is not None
        self.cameras_path.parent.mkdir(parents=True, exist_ok=True)
        self.cameras_path.write_text(
            json.dumps(self.cameras, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def known_unit_codes(self) -> list[str]:
        """data_root 下已有产物目录的机器人编号，供页面快速选择。"""
        if not self.config.data_root.is_dir():
            return []
        return sorted(
            p.name for p in self.config.data_root.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_")) and UNIT_CODE_RE.match(p.name)
            and (p / "calibrations").is_dir()
        )

    def info(self) -> dict[str, Any]:
        return {
            "unit_code": self.unit_code,
            "unit_root": str(self.config.unit_root(self.unit_code)) if self.unit_code else None,
            "set_at": self.set_at,
            "cameras": dict(self.cameras),
            "known": self.known_unit_codes(),
            "state_path": str(self.config.state_path),
        }


def _public_url(url: str, request: Request) -> str:
    """配置里的 127.0.0.1 对浏览器没意义：换成浏览器访问本服务用的主机名。"""
    host = request.url.hostname or "127.0.0.1"
    return url.replace("127.0.0.1", host).replace("localhost", host)


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="标定工作站", version=__version__)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    replay = HttpClient("回放(18004)", config.replay_url)
    capability = HttpClient("能力中心(18000)", config.capability_url)
    ws = Workspace(config)
    cloud = CloudSync(CloudSettings(config.data_root / "cloud.json"))
    if config.mock:
        mock_devices = [
            {"serial": "MOCK-HEAD-0001", "name": "Mock Orbbec (head)"},
            {"serial": "MOCK-WAIST-0002", "name": "Mock Orbbec (waist)"},
        ]
        camera_manager = CameraManager(
            lambda serial: MockSource(serial), lambda: mock_devices)
    else:
        camera_manager = CameraManager(
            lambda serial: OrbbecSource(
                serial, calibration_path=config.rgbd_calibration_path),
            discover_orbbec,
        )
    calib2d = Calib2DEngine(
        camera_manager, config.data_root / "_hand_eye_2d_sessions",
        tuple(int(value) for value in config.board_size.lower().split("x")),
        config.robot_urdf_path,
    )
    app.state.camera_manager = camera_manager
    app.state.calib2d = calib2d
    calib3d_camera = configure_calib3d(
        camera_manager,
        data_root=config.data_root,
        rgbd_calibration_path=config.rgbd_calibration_path,
        capability_url=config.capability_url,
        mock=config.mock,
    )
    app.state.calib3d_camera = calib3d_camera
    app.router.add_event_handler("shutdown", camera_manager.close)
    app.router.add_event_handler("shutdown", calib3d_app.pose_provider.close)

    def auto_push() -> None:
        """归档 / 切换生效后，若开启自动推送则后台把待同步产物推到云端。"""
        if cloud.settings.configured and cloud.settings.auto_push and ws.store is not None:
            cloud.push_pending_async(ws.store)

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError):
        status = 409 if exc.status in (400, 404, 409, 422) else 502
        return JSONResponse({"ok": False, "error": str(exc), **exc.to_dict()}, status_code=status)

    def fail(status: int, message: str) -> HTTPException:
        return HTTPException(status, {"ok": False, "error": message, "message": message})

    def register_capability_robot(unit_code: str) -> None:
        capability.post("/api/capability/robot", {
            "unit_code": unit_code,
            "vendor": config.vendor,
            "model": config.model,
        })

    def register_saved_robot() -> None:
        """Reconcile persisted workstation identity after an upgrade/restart."""
        if not ws.unit_code:
            return
        try:
            register_capability_robot(ws.unit_code)
        except ServiceError as exc:
            # 18000 may still be starting. Any 3D request retries this and
            # surfaces the error, so startup remains available for 2D work.
            logger.warning("未能向 18000 登记已保存的机器人 %s: %s", ws.unit_code, exc)

    app.router.add_event_handler("startup", register_saved_robot)

    def store() -> ArtifactStore:
        if ws.store is None:
            raise fail(409, "还没有设置机器人编号，请先在页面输入")
        return ws.store

    def job() -> Job:
        if ws.job is None:
            raise fail(409, "还没有设置机器人编号，请先在页面输入")
        return ws.job

    def camera_role(role: str):
        if role not in config.cameras:
            raise fail(422, f"未知相机位置 {role!r}，可选 {list(config.cameras)}")
        return config.cameras[role]

    def any_role(role: str) -> str:
        """校验相机位置或手部产物的 subject_key。"""
        if role in config.cameras or _ROLE_ID_RE.match(role or ""):
            return role
        raise fail(422, f"非法标定对象键 {role!r}")

    def role_label(role: str, label: str | None = None) -> str:
        if role in config.cameras:
            return config.cameras[role].label
        return (label or "").strip() or role

    # ---------------- 配置 / 健康 ----------------

    @app.get("/api/config")
    def api_config(request: Request):
        public = config.to_public_dict()
        public["robot"]["unit_code"] = ws.unit_code
        public["unit_root"] = ws.info()["unit_root"]
        for role_id, entry in public["cameras"].items():
            remembered = ws.cameras.get(role_id) or {}
            entry["serial"] = remembered.get("serial")
            entry["serial_name"] = remembered.get("name")
            entry["serial_set_at"] = remembered.get("set_at")
        public["services_public"] = {k: _public_url(v, request) for k, v in public["services"].items()}
        public["version"] = __version__
        return public

    @app.get("/api/robot")
    def api_robot():
        return ws.info()

    @app.put("/api/robot")
    def api_robot_set(body: dict):
        try:
            unit_code = validate_unit_code(str(body.get("unit_code") or ""))
            register_capability_robot(unit_code)
            ws.select(unit_code)
        except ValueError as exc:
            raise fail(409 if "正在进行" in str(exc) else 422, str(exc)) from exc
        return {"ok": True, **ws.info()}

    @app.put("/api/robot/cameras")
    def api_robot_camera_set(body: dict):
        role = camera_role(str(body.get("role") or body.get("camera_role") or ""))
        serial = str(body.get("serial") or "").strip()
        if not serial:
            raise fail(422, "缺少相机序列号")
        try:
            cams = ws.remember_camera(role.id, serial, str(body.get("name") or ""))
        except ValueError as exc:
            raise fail(409, str(exc)) from exc
        return {"ok": True, "cameras": cams}

    @app.delete("/api/robot/cameras/{role}")
    def api_robot_camera_forget(role: str):
        camera_role(role)
        try:
            cams = ws.forget_camera(role)
        except ValueError as exc:
            raise fail(409, str(exc)) from exc
        return {"ok": True, "cameras": cams}

    @app.get("/api/health")
    def api_health():
        out: dict[str, Any] = {"ok": True, "services": {}}
        ok2d, err2d = True, None
        ok3d, err3d = True, None
        okr, errr = replay.reachable("/api/status")
        okc, errc = capability.reachable("/api/capability/registry")
        out["services"]["hand_eye_2d"] = {"ok": ok2d, "error": err2d}
        out["services"]["hand_eye_3d"] = {"ok": ok3d, "error": err3d, "optional": True}
        out["services"]["replay"] = {"ok": okr, "error": errr}
        out["services"]["capability"] = {"ok": okc, "error": errc}
        if ok2d:
            status = calib2d.status()
            arm = {"available": False, "engaged": False}
            out["hand_eye_2d"] = {
                "run_id": status.get("run_id"), "count": status.get("count"),
                "arm": status.get("arm"), "arm_selectable": status.get("arm_selectable"),
                "camera": status.get("camera"), "board_size": status.get("board_size"),
                "arm_control": bool(arm.get("available") or arm.get("engaged")),
            }
            if out["hand_eye_2d"]["arm_control"]:
                out["ok"] = False
                out["services"]["hand_eye_2d"]["error"] = "2D 引擎不应控制手臂；手臂控制只由 18004 持有"
        if okr:
            rs = replay.get("/api/status")
            out["replay"] = {"state": rs.get("state"), "message": rs.get("message"),
                             "run_id": rs.get("run_id"), "mock": rs.get("mock"),
                             "arm": (rs.get("arm") or {}).get("arm"),
                             "engaged": (rs.get("arm") or {}).get("engaged")}
        out["ok"] = out["ok"] and ok2d and okr
        return out

    # ---- 原生2D兼容接口；18004切换后直接以18005作为采集目标 ----

    def native_camera_role(serial: str, requested: str | None = None) -> str:
        role = str(requested or ws.role_for_serial(serial) or "").strip()
        if role not in config.cameras:
            raise fail(409, f"相机 {serial} 尚未绑定位置，请先在18005选择头部或腰部")
        return role

    @app.get("/api/status")
    def api_native_2d_status():
        return calib2d.status()

    @app.get("/api/session")
    def api_native_2d_session():
        return calib2d.status()

    @app.post("/api/session/start")
    def api_native_2d_session_start(body: dict):
        try:
            return calib2d.start_session(body)
        except FileExistsError as exc:
            raise fail(409, str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise fail(422, str(exc)) from exc

    @app.get("/api/camera/devices")
    def api_native_camera_devices():
        devices = camera_manager.devices()
        return {"success": True, "devices": devices,
                "current_serial": calib2d.serial,
                "connected": bool(calib2d.serial), "camera": calib2d.camera_info()}

    @app.post("/api/camera/select")
    def api_native_camera_select(body: dict):
        serial = str(body.get("serial") or "").strip()
        try:
            role = native_camera_role(serial, body.get("camera_role"))
            camera = calib2d.select(role, serial)
            calib3d_camera.select(role, serial)
            return {"success": True, "camera": camera}
        except (KeyError, ValueError, RuntimeError) as exc:
            raise fail(409, str(exc)) from exc

    @app.post("/api/checkerboard/detect")
    def api_native_checkerboard_detect(_body: dict | None = None):
        try:
            return calib2d.detect()
        except (TimeoutError, RuntimeError) as exc:
            raise fail(503, str(exc)) from exc

    @app.post("/api/capture")
    def api_native_capture(body: dict | None = None):
        try:
            return calib2d.capture(body or {})
        except ValueError as exc:
            raise fail(409, str(exc)) from exc
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise fail(503, str(exc)) from exc

    @app.post("/api/solve")
    def api_native_solve(body: dict):
        try:
            return calib2d.solve(body)
        except (ValueError, RuntimeError, OSError) as exc:
            raise fail(409, str(exc)) from exc

    @app.get("/api/solve/status")
    def api_native_solve_status():
        return calib2d.solve_status()

    @app.get("/api/arm/status")
    def api_native_arm_status():
        return {"available": False, "enabled": False, "engaged": False,
                "note": "手臂控制只由18004持有"}

    @app.websocket("/ws/stream")
    async def ws_native_stream(socket: WebSocket):
        await socket.accept()
        try:
            while True:
                try:
                    jpeg = await asyncio.to_thread(calib2d.jpeg)
                    await socket.send_bytes(jpeg)
                except (TimeoutError, RuntimeError):
                    await asyncio.sleep(0.2)
                    continue
                await asyncio.sleep(1 / 15)
        except WebSocketDisconnect:
            return

    # ---------------- 3D 手安装 / TCP ----------------

    def capability_registry() -> dict[str, Any]:
        if ws.unit_code:
            register_capability_robot(ws.unit_code)
        payload = capability.get("/api/capability/registry")
        registry = payload.get("registry")
        if not isinstance(registry, dict):
            raise fail(502, "18000 返回缺少 registry")
        return registry

    def active_camera_manifest(artifact_type: str, role: str) -> dict[str, Any] | None:
        manifest = store().active(artifact_type, role)
        if manifest is None:
            return None
        manifest["path"] = str(store().artifact_dir(artifact_type, role, manifest["run_id"]))
        return manifest

    def local_3d_payload(response: Any) -> dict[str, Any]:
        if isinstance(response, JSONResponse):
            payload = json.loads(response.body.decode("utf-8"))
            if response.status_code >= 400:
                raise fail(response.status_code, str(payload.get("error") or "3D请求失败"))
            return payload
        if not isinstance(response, dict):
            raise fail(500, "3D引擎返回了无效响应")
        return response

    @app.get("/api/hand-calibration")
    async def api_hand_calibration(request: Request):
        registry = capability_registry()
        active = registry.get("active") or {}
        role = str(active.get("camera_role") or "head")
        ok3d, error3d = True, None
        mount = local_3d_payload(await calib3d_mount.api_mount_result())
        current = {
            artifact_type: active_camera_manifest(artifact_type, role)
            for artifact_type in ("extrinsic", "intrinsic", "camera_transform")
        }
        return {
            "ok": True,
            "service": {"ok": ok3d, "error": error3d},
            "active": active,
            "hands": registry.get("hands") or [],
            "camera_role": role,
            "camera_artifacts": current,
            "mount": mount,
            "ui_url": "/three-d-ui/",
        }

    @app.post("/api/hand-calibration/solve")
    async def api_hand_calibration_solve(body: dict):
        registry = capability_registry()
        active = registry.get("active") or {}
        role = str(body.get("camera_role") or active.get("camera_role") or "head")
        camera_role(role)
        extrinsic = active_camera_manifest("extrinsic", role)
        if extrinsic is None:
            raise fail(409, f"{role_label(role)}还没有生效的 2D 外参")
        calib_path = Path(extrinsic["path"]) / str(extrinsic.get("primary_file") or "handeye_result_left.json")
        return local_3d_payload(
            await calib3d_mount.api_mount_solve({"calib_path": str(calib_path)})
        )

    @app.post("/api/hand-calibration/finalize")
    async def api_hand_calibration_finalize(body: dict):
        run_id = str(body.get("run_id") or "").strip()
        if not _RUN_NAME_RE.match(run_id):
            raise fail(422, "运行名不能为空，只能含 Unicode 字母/数字及 . _ -，且不能以 . 开头")
        registry = capability_registry()
        active = registry.get("active") or {}
        arm = str(active.get("arm") or "")
        hand_id = str(active.get("hand_id") or "")
        if arm not in ("left_arm", "right_arm") or not hand_id:
            raise fail(409, "18000 尚未选择有效的激活臂和手型号")
        role = str(body.get("camera_role") or active.get("camera_role") or "head")
        camera_role(role)
        extrinsic = active_camera_manifest("extrinsic", role)
        if extrinsic is None:
            raise fail(409, f"{role_label(role)}还没有生效的 2D 外参")
        mount_payload = local_3d_payload(await calib3d_mount.api_mount_result())
        result = mount_payload.get("result")
        if not isinstance(result, dict):
            raise fail(409, "3D引擎还没有安装标定结果，请先标注并解算")
        if mount_payload.get("stale"):
            raise fail(409, "安装标定结果已过期：样本在解算后发生变化，请重新解算")
        result_arm = str(result.get("arm") or "").replace("_arm", "")
        if result_arm and f"{result_arm}_arm" != arm:
            raise fail(409, f"3D结果属于 {result.get('arm')}，18000 当前激活的是 {arm}")
        result_hand = str(result.get("hand_id") or "")
        if result_hand and result_hand != hand_id:
            raise fail(409, f"3D结果属于手 {result_hand}，18000 当前激活的是 {hand_id}")
        result_path = Path(str(result.get("saved_to") or ""))
        hand = next((item for item in registry.get("hands") or []
                     if item.get("id") == hand_id), {})
        try:
            artifacts = store().finalize_3d_mount(
                result_path=result_path,
                result=result,
                run_id=run_id,
                arm=arm,
                hand_id=hand_id,
                hand_serial=str(body.get("hand_serial") or "").strip() or None,
                extrinsic_artifact_id=extrinsic.get("artifact_id"),
                tcp_point_id=str(body.get("tcp_point_id") or hand.get("tcp_point_id") or "") or None,
                overwrite=bool(body.get("overwrite", False)),
            )
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            raise fail(409, str(exc)) from exc
        subject_partition = artifacts["hand_mount"]["subject_key"]
        for artifact_type in ("hand_mount", "tcp_profile"):
            store().set_active(artifact_type, subject_partition, run_id)

        cloud_error = None
        if cloud.settings.configured and cloud.settings.auto_push:
            try:
                for artifact_type in ("hand_mount", "tcp_profile"):
                    cloud.push_one(store(), artifact_type, subject_partition, run_id)
            except CloudError as exc:
                cloud_error = str(exc)

        selected: dict[str, dict[str, Any]] = {}
        for artifact_type in ("extrinsic", "intrinsic", "camera_transform"):
            manifest = active_camera_manifest(artifact_type, role)
            if manifest is not None:
                selected[artifact_type] = manifest
        for artifact_type in ("hand_mount", "tcp_profile"):
            manifest = store().get(artifact_type, subject_partition, run_id)
            if manifest is not None:
                selected[artifact_type] = manifest
        for manifest in selected.values():
            capability.post("/api/capability/calibration-artifacts", {
                "manifest": manifest,
                "local_path": manifest.get("path"),
            })
        capability.post("/api/capability/calibration-bindings", {
            "arm": arm,
            "hand_id": hand_id,
            "camera_role": role,
            "artifacts": {kind: manifest["artifact_id"] for kind, manifest in selected.items()},
        })
        auto_push()
        return {
            "ok": True,
            "run_id": run_id,
            "subject_key": subject_partition,
            "artifacts": selected,
            "binding": {kind: manifest["artifact_id"] for kind, manifest in selected.items()},
            "cloud_error": cloud_error,
        }

    # ---------------- 相机 ----------------

    @app.get("/api/cameras")
    def api_cameras():
        devices = api_native_camera_devices()
        for d in devices.get("devices", []):
            d["role_hint"] = ws.role_for_serial(str(d.get("serial") or ""))
        devices["roles"] = {
            c.id: {"label": c.label, "serial": ws.camera_serial(c.id), "target": c.target}
            for c in config.cameras.values()
        }
        return devices

    @app.post("/api/cameras/select")
    def api_cameras_select(body: dict):
        serial = str(body.get("serial") or "").strip()
        if not serial:
            raise fail(422, "缺少相机序列号")
        role = native_camera_role(serial, body.get("camera_role"))
        return api_native_camera_select({"serial": serial, "camera_role": role})

    @app.post("/api/cameras/detect")
    def api_cameras_detect():
        return api_native_checkerboard_detect()

    # ---------------- 计划 ----------------

    def _plans_for(role_id: str, arm: str | None = None) -> list[dict[str, Any]]:
        role = camera_role(role_id)
        plans = replay.get("/api/plans").get("plans", [])
        out = []
        for plan in plans:
            if plan.get("target") != role.target:
                continue
            if arm and plan.get("arm") != arm:
                continue
            nodes = plan.get("nodes") or []
            out.append({
                "id": plan["id"], "name": plan["name"], "arm": plan.get("arm"),
                "target": plan.get("target"), "draft": plan.get("draft"),
                "camera_serial": plan.get("camera_serial"),
                "sample_count": sum(1 for n in nodes if n.get("enabled", True) and n.get("role") == "sample"),
                "node_count": len(nodes),
            })
        return out

    @app.get("/api/plans")
    def api_plans(camera_role_id: str, arm: str | None = None):
        return {"plans": _plans_for(camera_role_id, arm)}

    # ---------------- 向导：准备 → 接管 → 归位 → 运行 → 求解 → 生效 ----------------

    @app.get("/api/calibration")
    def api_calibration():
        """向导的聚合状态：当前任务 + 18004 状态 + 原生 2D 会话。"""
        data = job().snapshot()
        out: dict[str, Any] = {"job": data, "replay": None, "session": None}
        try:
            out["replay"] = replay.get("/api/status")
        except ServiceError as exc:
            out["replay_error"] = str(exc)
        try:
            out["session"] = calib2d.status()
        except ServiceError as exc:
            out["session_error"] = str(exc)
        return out

    @app.post("/api/calibration/prepare")
    def api_prepare(body: dict):
        role = camera_role(str(body.get("camera_role") or ""))
        arm = str(body.get("arm") or "")
        if arm not in ARMS:
            raise fail(422, "arm 只能是 left 或 right")
        serial = str(body.get("camera_serial") or ws.camera_serial(role.id) or "").strip()
        if not serial:
            raise fail(422, "请选择相机序列号")
        plan_id = str(body.get("plan_id") or "")
        if not plan_id:
            raise fail(422, "请选择采集计划")

        replay_state = replay.get("/api/status").get("state")
        if replay_state in {"moving", "settling", "capturing", "returning", "paused", "preflight"}:
            raise fail(409, f"回放服务正在运行（{replay_state}），请先停止")

        plan = replay.get(f"/api/plans/{plan_id}")
        if plan.get("target") != role.target:
            raise fail(409, f"计划 {plan.get('name')} 的目标是 {plan.get('target')}，与 {role.label} 不符")
        if plan.get("arm") != arm:
            raise fail(409, f"计划 {plan.get('name')} 是 {plan.get('arm')} 臂，与所选 {arm} 臂不符")
        if plan.get("draft"):
            raise fail(409, f"计划 {plan.get('name')} 还是草稿（缺少原点或校验未通过），请先在计划编辑里完成")
        if plan.get("base_url") != config.hand_eye_2d_url:
            plan["base_url"] = config.hand_eye_2d_url
            plan = replay.put(f"/api/plans/{plan_id}", plan)

        # 计划里的相机序列号以本次选择为准（回放预检会 select+校验）；
        # 未检出棋盘格时：图像总是保存；continue=继续采后面的点，abort=停止采样沿剩余路径回原点
        on_missing = str(body.get("on_missing_corners") or "continue")
        if on_missing not in ("continue", "abort"):
            raise fail(422, "on_missing_corners 只能是 continue 或 abort")
        if plan.get("camera_serial") != serial or plan.get("on_missing_corners") != on_missing:
            plan["camera_serial"] = serial
            plan["on_missing_corners"] = on_missing
            plan = replay.put(f"/api/plans/{plan_id}", plan)
        # 先切到该相机让预览就是它；首个样本前可随时再切
        camera = api_native_camera_select({"serial": serial, "camera_role": role.id})
        # 记住：这台机器人的该位置用这台相机，下次自动选中（重选即覆盖）
        ws.remember_camera(role.id, serial, str((camera.get("camera") or {}).get("name") or ""))

        job().reset()
        return {"ok": True, "job": job().update(
            step="prepared", camera_role=role.id, camera_label=role.label, target=role.target,
            arm=arm, camera_serial=serial, plan_id=plan_id, plan_name=plan.get("name"),
            on_missing_corners=on_missing,
            camera=camera.get("camera"), run_id=None, run_dir=None,
            solved=False, finalized=False, artifacts=None,
        )}

    def _require_job(*steps: str) -> dict[str, Any]:
        data = job().snapshot()
        if not data.get("plan_id"):
            raise fail(409, "还没有准备好的标定任务，请先选择相机与计划")
        if steps and data.get("step") not in steps:
            raise fail(409, f"当前步骤是 {data.get('step')}，不能执行此操作")
        return data

    @app.post("/api/calibration/engage")
    def api_engage():
        data = _require_job()
        result = replay.post("/api/control/engage", {"arm": data["arm"]})
        job().update(step="engaged")
        return {"ok": True, **result}

    @app.post("/api/calibration/guide")
    def api_guide():
        _require_job()
        return replay.post("/api/control/guide")

    @app.post("/api/calibration/catch")
    def api_catch():
        _require_job()
        return replay.post("/api/control/catch")

    @app.post("/api/calibration/disarm")
    def api_disarm():
        result = replay.post("/api/control/disarm")
        data = job().snapshot()
        if data.get("step") in {"engaged", "prepared"}:
            job().update(step="prepared")
        return result

    @app.post("/api/calibration/run")
    def api_run(body: dict | None = None):
        # 手臂会自动运动：只允许在"已接管"步骤、且请求显式带 confirm=true 时启动，
        # 防止任何误触 / 重复请求 / 旧页面把手臂跑起来
        data = _require_job("prepared", "engaged")
        body = body or {}
        if body.get("confirm") is not True:
            raise fail(409, "启动自动采集需要在页面上确认（confirm=true）")
        replay_status = replay.get("/api/status")
        if not (replay_status.get("arm") or {}).get("engaged"):
            raise fail(409, "手臂尚未接管，不能开始自动采集")
        run_name = str(body.get("run_name") or "").strip()
        if run_name and not _RUN_NAME_RE.match(run_name):
            raise fail(422, "运行名可用中英文、数字、. _ -，不能含空格或斜杠")
        if not run_name:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run_name = f"{ws.unit_code}_{data['camera_role']}_{data['arm']}_{stamp}"
        result = replay.post(f"/api/control/run/{data['plan_id']}", {"run_id": run_name})
        job().update(step="running", run_id=result.get("run_id"), run_dir=result.get("run_dir"),
                   started_at=datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, **result}

    @app.post("/api/calibration/pause")
    def api_pause():
        return replay.post("/api/control/pause")

    @app.post("/api/calibration/resume")
    def api_resume():
        return replay.post("/api/control/resume")

    @app.post("/api/calibration/stop")
    def api_stop():
        return replay.post("/api/control/stop")

    @app.post("/api/calibration/mark-captured")
    def api_mark_captured():
        """运行结束（completed / stopped）后前端调用，进入求解步骤。"""
        data = _require_job()
        status = replay.get("/api/status")
        if status.get("state") in {"moving", "settling", "capturing", "returning", "paused", "preflight"}:
            raise fail(409, "轨迹还在运行")
        run_dir = data.get("run_dir") or status.get("run_dir")
        if not run_dir:
            raise fail(409, "没有运行目录，请先运行采集")
        n = len(list((Path(run_dir) / "joints").glob("*.json"))) if (Path(run_dir) / "joints").is_dir() else 0
        caps = status.get("captures") or []
        skipped = sum(1 for c in caps if c.get("skipped"))
        no_corners = sum(1 for c in caps if c.get("corners_detected") is False)
        return {"ok": True, "job": job().update(step="captured", run_dir=run_dir, sample_count=n, skipped_count=skipped,
                                              no_corners_count=no_corners, usable_count=max(0, n - no_corners),
                                              sampling_aborted_at=(status.get("progress") or {}).get("sampling_aborted"),
                                              outcome=status.get("state"))}

    @app.post("/api/calibration/solve")
    def api_solve(body: dict | None = None):
        data = _require_job("captured", "solved")
        body = body or {}
        square = float(body.get("square_size_mm") or config.square_size_mm)
        payload = {
            "session": data["run_dir"],
            "square_size_mm": square,
            "board_size": str(body.get("board_size") or config.board_size),
            "method": str(body.get("method") or "park"),
            "eye": "left",
        }
        result = api_native_solve(payload)
        job().update(step="solving", square_size_mm=square, solve_started_at=datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, **result}

    @app.get("/api/calibration/solve/status")
    def api_solve_status():
        status = api_native_solve_status()
        data = job().snapshot()
        if data.get("step") == "solving" and not status.get("running"):
            if status.get("result") and not status.get("error"):
                result = status["result"]
                left = result.get("left") if isinstance(result, dict) and "left" in result else result
                job().update(step="solved", solved=True, result_summary={
                    "num_samples": left.get("num_samples"), "num_inliers": left.get("num_inliers"),
                    "residual_translation_mm": left.get("residual_translation_mm"),
                    "residual_rotation_deg": left.get("residual_rotation_deg"),
                    "t_cam2base_m": left.get("t_cam2base_m"), "rpy_rad": left.get("rpy_rad"),
                })
            elif status.get("error"):
                job().update(step="captured", solve_error=status["error"])
        status["job"] = job().snapshot()
        return status

    @app.post("/api/calibration/finalize")
    def api_finalize(body: dict | None = None):
        data = _require_job("solved", "finalized")
        body = body or {}
        activate = bool(body.get("activate", True))
        try:
            artifacts = store().finalize_2d_run(
                run_dir=Path(data["run_dir"]), camera_role=data["camera_role"],
                run_id=data["run_id"], arm=data["arm"], overwrite=bool(body.get("overwrite", False)),
                camera_label=data.get("camera_label"),
            )
        except FileExistsError as exc:
            raise fail(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise fail(409, str(exc)) from exc
        if activate:
            for artifact_type in artifacts:
                artifacts[artifact_type] = store().set_active(artifact_type, data["camera_role"], data["run_id"])
        out = {"ok": True, "job": job().update(step="finalized", finalized=True, activated=activate,
                                             artifacts={k: v["files"] for k, v in artifacts.items()})}
        auto_push()
        return out

    @app.post("/api/calibration/reset")
    def api_reset():
        return {"ok": True, "job": job().reset()}

    @app.post("/api/calibration/role")
    def api_set_job_role(body: dict):
        """求解 / 归档前改这次标定归属的相机位置。Body: {camera_role, camera_label?}

        camera_role 可以是配置里的 head/waist，也可以是自定义 id（如 chest），此时需给 camera_label。
        已归档（finalized）后不能再改。
        """
        data = _require_job("captured", "solved")
        role = str(body.get("camera_role") or "").strip().lower()
        any_role(role)
        label = role_label(role, str(body.get("camera_label") or ""))
        if role not in config.cameras and not str(body.get("camera_label") or "").strip():
            raise fail(422, "自定义相机位置需要填写名称")
        target = config.cameras[role].target if role in config.cameras else None
        return {"ok": True, "job": job().update(camera_role=role, camera_label=label, target=target,
                                              camera_role_custom=role not in config.cameras)}

    @app.post("/api/calibration/load-run")
    def api_load_run(body: dict):
        """把一次历史 2D 采集运行装入当前任务，直接进入求解（已有结果则进入结果/归档）步骤。

        Body: {run_id, arm}。数据取自 18004 的 runs/<arm>/<run_id>/run.json。
        """
        run_id = str(body.get("run_id") or "").strip()
        arm = str(body.get("arm") or "").strip()
        if not _RUN_NAME_RE.match(run_id) or arm not in ARMS:
            raise fail(422, "需要 run_id 与 arm")
        current = job().snapshot()
        if current.get("step") in {"running", "solving"}:
            raise fail(409, f"当前任务正在 {current.get('step')}，请先完成或停止")
        replay_state = replay.get("/api/status").get("state")
        if replay_state in {"moving", "settling", "capturing", "returning", "paused", "preflight"}:
            raise fail(409, f"回放服务正在运行（{replay_state}），请先停止")

        run = next((r for r in replay.get("/api/runs").get("runs", [])
                    if r.get("run_id") == run_id and r.get("arm") == arm), None)
        if run is None:
            raise fail(404, f"没有找到运行 {arm}/{run_id}")
        run_dir = Path(run["path"])
        try:
            record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise fail(409, f"读取 run.json 失败: {exc}") from exc
        plan = record.get("plan") or {}
        target = str(plan.get("target") or run.get("target") or "")
        role = next((r for r in config.cameras.values() if r.target == target), None)
        if role is None:
            raise fail(409, f"运行目标 {target!r} 不属于任何已配置的相机位置，无法在工作站中求解")

        joints_dir = run_dir / "joints"
        n = len(list(joints_dir.glob("*.json"))) if joints_dir.is_dir() else 0
        if n == 0:
            raise fail(409, "该运行没有保存任何样本，无法求解")
        caps = record.get("captures") or []
        skipped = sum(1 for c in caps if c.get("skipped"))
        no_corners = sum(1 for c in caps if c.get("corners_detected") is False)

        fields: dict[str, Any] = dict(
            step="captured", camera_role=role.id, camera_label=role.label, target=role.target,
            arm=arm, camera_serial=plan.get("camera_serial") or run.get("camera_serial"),
            plan_id=plan.get("id"), plan_name=plan.get("name"),
            on_missing_corners=plan.get("on_missing_corners"),
            run_id=run_id, run_dir=str(run_dir), started_at=record.get("started_at"),
            sample_count=n, skipped_count=skipped, no_corners_count=no_corners,
            usable_count=max(0, n - no_corners), sampling_aborted_at=record.get("sampling_aborted_at"),
            outcome=record.get("outcome"), loaded_from_history=True,
            solved=False, finalized=False, artifacts=None,
        )
        result_path = run_dir / "handeye_result_left.json"
        if result_path.is_file():
            try:
                left = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                left = {}
            fields.update(step="solved", solved=True, square_size_mm=left.get("square_size_mm"), result_summary={
                "num_samples": left.get("num_samples"), "num_inliers": left.get("num_inliers"),
                "residual_translation_mm": left.get("residual_translation_mm"),
                "residual_rotation_deg": left.get("residual_rotation_deg"),
                "t_cam2base_m": left.get("t_cam2base_m"), "rpy_rad": left.get("rpy_rad"),
            })
            # 已归档过（可能归到了别的相机位置，如自定义名）：沿用归档时的位置
            archived = next((m for m in store().list("extrinsic") if m.get("run_id") == run_id), None)
            if archived is not None:
                fields.update(step="finalized", finalized=True,
                              camera_role=archived.get("camera_role"),
                              camera_label=archived.get("camera_label") or role_label(archived.get("camera_role")),
                              camera_role_custom=archived.get("camera_role") not in config.cameras)
        job().reset()
        return {"ok": True, "job": job().update(**fields)}

    # ---------------- 产物 / 历史 ----------------

    @app.get("/api/artifacts")
    def api_artifacts(type: str | None = None, camera_role: str | None = None):
        if type and type not in ARTIFACT_TYPES:
            raise fail(422, f"type 只能是 {ARTIFACT_TYPES}")
        items = store().list(type, camera_role)
        for m in items:
            m["sync_state"] = cloud.sync_state(m)
        return {"items": items, "root": str(store().root)}

    @app.get("/api/artifacts/active")
    def api_artifacts_active():
        out: dict[str, Any] = {}
        for artifact_type in ARTIFACT_TYPES:
            configured = ([r for r in CAMERA_ROLES if r in config.cameras]
                          if artifact_type in ("extrinsic", "intrinsic", "camera_transform") else [])
            keys = configured + [key for key in store().subject_keys(artifact_type)
                                 if key not in configured]
            out[artifact_type] = {key: store().active(artifact_type, key) for key in keys}
        return out

    @app.get("/api/artifacts/{artifact_type}/{role}/{run_id}")
    def api_artifact(artifact_type: str, role: str, run_id: str):
        manifest = store().get(artifact_type, role, run_id)
        if manifest is None:
            raise fail(404, "产物不存在")
        return manifest

    @app.post("/api/artifacts/{artifact_type}/{role}/{run_id}/activate")
    def api_artifact_activate(artifact_type: str, role: str, run_id: str, all_types: bool = True):
        """设为生效。默认连同同一 run_id 一起归档的其他类型（外参 ↔ 内参）一并切换，
        保证生效的外参与它求解时用的内参始终配对。"""
        any_role(role)
        try:
            result = store().set_active(artifact_type, role, run_id)
        except FileNotFoundError as exc:
            raise fail(404, str(exc)) from exc
        if all_types:
            for t in ARTIFACT_TYPES:
                if t != artifact_type and store().get(t, role, run_id) is not None:
                    store().set_active(t, role, run_id)
        auto_push()
        return result

    @app.delete("/api/artifacts/{artifact_type}/{role}/{run_id}")
    def api_artifact_delete(artifact_type: str, role: str, run_id: str, all_types: bool = True):
        """删除归档产物。默认把同一 run_id 一起归档的所有类型（外参 + 内参）都删掉；
        只删 calibrations/ 下的副本，18004 的原始采集目录保留，之后仍可在"采集运行"里重新归档。
        """
        if artifact_type not in ARTIFACT_TYPES:
            raise fail(422, f"type 只能是 {ARTIFACT_TYPES}")
        any_role(role)
        if not _RUN_NAME_RE.match(run_id):
            raise fail(422, "非法 run_id")
        types = list(ARTIFACT_TYPES) if all_types else [artifact_type]
        deleted = []
        cloud_errors = []
        for t in types:
            manifest = store().get(t, role, run_id)
            try:
                deleted.append(store().delete(t, role, run_id))
            except FileNotFoundError:
                if t == artifact_type:
                    raise fail(404, "产物不存在")
                continue
            # 云端上有副本的话一并删掉（尽力而为，失败只提示）
            err = cloud.delete_remote(store().unit_code, manifest)
            if err:
                cloud_errors.append(f"{t}: {err}")
        # 当前任务若正是这次归档，退回"已求解"状态，允许重新归档
        current = job().snapshot()
        if current.get("run_id") == run_id and current.get("camera_role") == role and current.get("finalized"):
            job().update(step="solved", finalized=False, activated=False, artifacts=None)
        return {"ok": True, "deleted": deleted,
                "cloud_error": "；".join(cloud_errors) if cloud_errors else None}

    @app.get("/api/artifacts/{artifact_type}/{role}/{run_id}/files/{name}")
    def api_artifact_file(artifact_type: str, role: str, run_id: str, name: str):
        if "/" in name or name.startswith("."):
            raise fail(422, "非法文件名")
        path = store().artifact_dir(artifact_type, role, run_id) / name
        if not path.is_file():
            raise fail(404, "文件不存在")
        return FileResponse(path, filename=name)

    @app.get("/api/runs")
    def api_runs():
        """18004 的 2D 运行列表，标注是否已求解 / 已打包。"""
        runs = replay.get("/api/runs").get("runs", [])
        finalized = {(m.get("run_id")) for m in store().list("extrinsic")}
        out = []
        for run in runs:
            if not str(run.get("target") or "").startswith("hand_eye_2D"):
                continue
            run_dir = Path(run.get("path") or "")
            out.append({
                **run,
                "solved": (run_dir / "handeye_result_left.json").is_file() if run_dir.name else False,
                "finalized": run.get("run_id") in finalized,
            })
        return {"runs": out}

    # ---------------- 云端推送 ----------------

    @app.get("/api/cloud")
    def api_cloud():
        """云端设置（token 只回掩码）+ 当前机器人的同步统计 + 最近一次同步结果。"""
        return cloud.summary(ws.store)

    @app.put("/api/cloud")
    def api_cloud_set(body: dict):
        """Body: {url?, token?, auto_push?}。token 传空字符串表示不改。"""
        try:
            cloud.settings.update(
                url=str(body["url"]) if "url" in body else None,
                token=str(body["token"]) if "token" in body else None,
                auto_push=bool(body["auto_push"]) if "auto_push" in body else None,
            )
        except ValueError as exc:
            raise fail(422, str(exc)) from exc
        return {"ok": True, **cloud.summary(ws.store)}

    @app.post("/api/cloud/test")
    def api_cloud_test():
        try:
            health = cloud.client.health()
        except CloudError as exc:
            raise fail(502, str(exc)) from exc
        if not health.get("write_enabled"):
            raise fail(502, "云端未配置 CALIB_API_TOKEN，不接受上传")
        return {"ok": True, "health": health}

    @app.post("/api/cloud/sync")
    def api_cloud_sync(force: bool = False):
        """把当前机器人所有待同步（未推送 / 状态变过）的产物推到云端；force=true 全部重推。"""
        result = cloud.push_pending(store(), force=force)
        return {"ok": bool(result.get("ok")), **result, **cloud.summary(ws.store)}

    @app.post("/api/artifacts/{artifact_type}/{role}/{run_id}/push")
    def api_artifact_push(artifact_type: str, role: str, run_id: str, all_types: bool = True):
        """推送一条产物（默认连同同一 run_id 的其他类型一起，保持外参/内参配对）。"""
        if artifact_type not in ARTIFACT_TYPES:
            raise fail(422, f"type 只能是 {ARTIFACT_TYPES}")
        any_role(role)
        if store().get(artifact_type, role, run_id) is None:
            raise fail(404, "产物不存在")
        types = [artifact_type] + [t for t in ARTIFACT_TYPES if all_types and t != artifact_type
                                   and store().get(t, role, run_id) is not None]
        pushed = []
        try:
            for t in types:
                pushed.append(cloud.push_one(store(), t, role, run_id))
        except CloudError as exc:
            raise fail(502, str(exc)) from exc
        return {"ok": True, "pushed": pushed}

    # 完整3D API和资源也由18005同一进程提供；该子应用不持有相机设备。
    app.mount("/three-d", calib3d_app.app, name="calib3d")

    # ---------------- 前端 ----------------

    calib3d_frontend = _FRONTEND_DIST / "three-d-ui"
    if calib3d_frontend.is_dir():
        app.mount(
            "/three-d-ui",
            StaticFiles(directory=str(calib3d_frontend), html=True),
            name="calib3d-ui",
        )

    if (_FRONTEND_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        candidate = _FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = _FRONTEND_DIST / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return JSONResponse({"error": "前端尚未构建", "hint": "cd frontend && npm ci && npm run build"}, status_code=503)

    return app
