"""18005 标定工作站后端：把 8131 + 18004 的多步操作编排成向导的一步。

不直接碰相机与手臂：相机/采集/求解在 8131，运动/安全在 18004。本服务只做
编排、校验（计划目标与相机位置一致、臂一致）、产物打包与登记。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .clients import HttpClient, ServiceError
from .config import CAMERA_ROLES, UNIT_CODE_RE, Config, validate_unit_code
from .manifest import ARTIFACT_TYPES, ArtifactStore

ARMS = ("left", "right")
_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_CALIB_ROOT = Path(__file__).resolve().parents[2]


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

    he2d = HttpClient("hand_eye_2D(8131)", config.hand_eye_2d_url)
    replay = HttpClient("回放(18004)", config.replay_url)
    capability = HttpClient("能力中心(18000)", config.capability_url)
    ws = Workspace(config)

    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError):
        status = 409 if exc.status in (400, 404, 409, 422) else 502
        return JSONResponse({"ok": False, "error": str(exc), **exc.to_dict()}, status_code=status)

    def fail(status: int, message: str) -> HTTPException:
        return HTTPException(status, {"ok": False, "error": message, "message": message})

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
            ws.select(str(body.get("unit_code") or ""))
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
        ok2d, err2d = he2d.reachable("/api/status")
        okr, errr = replay.reachable("/api/status")
        okc, errc = capability.reachable("/api/capability/registry")
        out["services"]["hand_eye_2d"] = {"ok": ok2d, "error": err2d}
        out["services"]["replay"] = {"ok": okr, "error": errr}
        out["services"]["capability"] = {"ok": okc, "error": errc}
        if ok2d:
            status = he2d.get("/api/status")
            arm = he2d.get("/api/arm/status")
            out["hand_eye_2d"] = {
                "run_id": status.get("run_id"), "count": status.get("count"),
                "arm": status.get("arm"), "arm_selectable": status.get("arm_selectable"),
                "camera": status.get("camera"), "board_size": status.get("board_size"),
                "arm_control": bool(arm.get("available") or arm.get("engaged")),
            }
            if out["hand_eye_2d"]["arm_control"]:
                out["ok"] = False
                out["services"]["hand_eye_2d"]["error"] = "8131 启用了手臂控制（--arm-control），会和回放抢控制权"
        if okr:
            rs = replay.get("/api/status")
            out["replay"] = {"state": rs.get("state"), "message": rs.get("message"),
                             "run_id": rs.get("run_id"), "mock": rs.get("mock"),
                             "arm": (rs.get("arm") or {}).get("arm"),
                             "engaged": (rs.get("arm") or {}).get("engaged")}
        out["ok"] = out["ok"] and ok2d and okr
        return out

    # ---------------- 相机 ----------------

    @app.get("/api/cameras")
    def api_cameras():
        devices = he2d.get("/api/camera/devices")
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
        return he2d.post("/api/camera/select", {"serial": serial})

    @app.post("/api/cameras/detect")
    def api_cameras_detect():
        return he2d.post("/api/checkerboard/detect", {"board_size": config.board_size})

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
        """向导的聚合状态：当前任务 + 18004 状态 + 8131 会话。"""
        data = job().snapshot()
        out: dict[str, Any] = {"job": data, "replay": None, "session": None}
        try:
            out["replay"] = replay.get("/api/status")
        except ServiceError as exc:
            out["replay_error"] = str(exc)
        try:
            out["session"] = he2d.get("/api/session")
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

        # 计划里的相机序列号以本次选择为准（回放预检会 select+校验）
        if plan.get("camera_serial") != serial:
            plan["camera_serial"] = serial
            plan = replay.put(f"/api/plans/{plan_id}", plan)
        # 先切到该相机让预览就是它；首个样本前可随时再切
        camera = he2d.post("/api/camera/select", {"serial": serial})
        # 记住：这台机器人的该位置用这台相机，下次自动选中（重选即覆盖）
        ws.remember_camera(role.id, serial, str((camera.get("camera") or {}).get("name") or ""))

        job().reset()
        return {"ok": True, "job": job().update(
            step="prepared", camera_role=role.id, camera_label=role.label, target=role.target,
            arm=arm, camera_serial=serial, plan_id=plan_id, plan_name=plan.get("name"),
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
        data = _require_job()
        body = body or {}
        run_name = str(body.get("run_name") or "").strip()
        if run_name and not _RUN_NAME_RE.match(run_name):
            raise fail(422, "运行名只能包含字母、数字、. _ -")
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
        return {"ok": True, "job": job().update(step="captured", run_dir=run_dir, sample_count=n,
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
        result = he2d.post("/api/solve", payload)
        job().update(step="solving", square_size_mm=square, solve_started_at=datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, **result}

    @app.get("/api/calibration/solve/status")
    def api_solve_status():
        status = he2d.get("/api/solve/status")
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
            )
        except FileExistsError as exc:
            raise fail(409, str(exc)) from exc
        except FileNotFoundError as exc:
            raise fail(409, str(exc)) from exc
        if activate:
            for artifact_type in artifacts:
                artifacts[artifact_type] = store().set_active(artifact_type, data["camera_role"], data["run_id"])
        return {"ok": True, "job": job().update(step="finalized", finalized=True, activated=activate,
                                              artifacts={k: v["files"] for k, v in artifacts.items()})}

    @app.post("/api/calibration/reset")
    def api_reset():
        return {"ok": True, "job": job().reset()}

    # ---------------- 产物 / 历史 ----------------

    @app.get("/api/artifacts")
    def api_artifacts(type: str | None = None, camera_role: str | None = None):
        if type and type not in ARTIFACT_TYPES:
            raise fail(422, f"type 只能是 {ARTIFACT_TYPES}")
        return {"items": store().list(type, camera_role), "root": str(store().root)}

    @app.get("/api/artifacts/active")
    def api_artifacts_active():
        out: dict[str, Any] = {}
        for artifact_type in ARTIFACT_TYPES:
            out[artifact_type] = {role: store().active(artifact_type, role) for role in CAMERA_ROLES if role in config.cameras}
        return out

    @app.get("/api/artifacts/{artifact_type}/{role}/{run_id}")
    def api_artifact(artifact_type: str, role: str, run_id: str):
        manifest = store().get(artifact_type, role, run_id)
        if manifest is None:
            raise fail(404, "产物不存在")
        return manifest

    @app.post("/api/artifacts/{artifact_type}/{role}/{run_id}/activate")
    def api_artifact_activate(artifact_type: str, role: str, run_id: str):
        try:
            return store().set_active(artifact_type, role, run_id)
        except FileNotFoundError as exc:
            raise fail(404, str(exc)) from exc

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

    # ---------------- 前端 ----------------

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
