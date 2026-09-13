"""Selection and persistence for the three 3D calibration object modes."""
import asyncio
import json
import re
import tempfile
from pathlib import Path

from .calib3d import app as engine
from .calib3d import mount_api
from .calib3d.hands import get_hand_model, canonical_hand_id
from .contract import canonical_subject, subject_key
from .tool_calibration import solve_tool


def install_object_routes(app, *, job, require_job, registry, extrinsic, store, fail, payload):
    def check_context(data):
        active = registry().get("active") or {}
        if active.get("arm") != f"{data['arm']}_arm":
            raise fail(409, "当前激活臂与采集数据不符")
        manifest = extrinsic(data["camera_role"])
        if not manifest or manifest.get("artifact_id") != data.get("extrinsic_artifact_id"):
            raise fail(409, "采集时使用的2D外参已变化，请重新准备任务")
        return manifest

    async def restore_annotation_session(data):
        check_context(data)
        backend = engine._available_episode_backend()
        if backend is None or Path(backend.task_dir).resolve() != Path(data["run_dir"]).resolve():
            payload(await engine.api_offline_switch_task({"path": data["run_dir"]}))
        if job().snapshot() != data:
            raise fail(409, "载入期间任务已变化，请重新进入选点")

    @app.post("/api/hand-calibration/annotation-session")
    async def annotation_session():
        data = require_job("annotating", "annotated", "solved")
        await restore_annotation_session(data)
        return {"ok": True, "job": data}

    @app.post("/api/hand-calibration/object")
    async def select_object(body: dict):
        data = require_job("annotating", "annotated", "solved")
        check_context(data)
        mode = body.get("mode")
        if mode not in ("hand", "tool", "tcp"):
            raise fail(422, "请选择标定对象")
        model_id = canonical_hand_id(body.get("model_id")) if mode == "hand" else None
        if mode == "hand":
            if not isinstance(model_id, str) or not model_id:
                raise fail(422, "请选择几何模型")
            try:
                model = get_hand_model(model_id)
            except (KeyError, ValueError) as exc:
                raise fail(422, str(exc)) from exc
            if model.spec.side != data["arm"]:
                raise fail(409, "模型侧别与采集手臂不符")
        tool_id = str(body.get("tool_id") or "").strip() if mode != "hand" else None
        if mode != "hand" and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", tool_id):
            raise fail(422, "工具编号请使用小写字母、数字、下划线或连字符（最多48字符）")
        await restore_annotation_session(data)
        if mode == "hand" and any(s.get("hand_id") != model_id for s in mount_api._load_mount_samples()):
            raise fail(409, "已有其他模型的选点，请先在高级操作台删除旧选点，再切换模型；原始采集数据不受影响")
        same = data.get("object_mode") == mode and data.get("tool_id") == tool_id
        return {"ok": True, "job": job().update(
            object_mode=mode, model_id=model_id, tool_id=tool_id,
            hand_id=(registry().get("active") or {}).get("hand_id") if mode == "hand" else None,
            tool_samples=data.get("tool_samples", []) if same else [],
            tool_result=None, solved=False, step="annotating",
        )}

    def tool_job(*steps):
        data = require_job(*steps)
        if data.get("object_mode") not in ("tool", "tcp"):
            raise fail(409, "请先选择普通刚性工具或单点TCP")
        check_context(data)
        backend = engine._available_episode_backend()
        if backend is None or Path(backend.task_dir).resolve() != Path(data["run_dir"]).resolve():
            raise fail(409, "选点数据目录已切换，请重新载入任务")
        return data

    @app.post("/api/hand-calibration/tool-point")
    async def pick_tool_point(body: dict):
        data = tool_job("annotating", "annotated", "solved")
        ids = ("tcp",) if data["object_mode"] == "tcp" else ("origin", "x", "xy")
        if body.get("point_id") not in ids:
            raise fail(422, "请选择当前模式中的特征点")
        observation = payload(await engine.api_offline_pick(body))
        if job().snapshot() != data:
            raise fail(409, "任务在深度处理期间已变化，请重新选点")
        observation["point_id"] = body["point_id"]
        samples = [s for s in data.get("tool_samples", [])
                   if (s["episode"], s["point_id"]) != (observation["episode"], observation["point_id"])]
        samples.append(observation)
        return {"ok": True, "job": job().update(tool_samples=samples, tool_result=None, solved=False, step="annotating")}

    @app.post("/api/hand-calibration/tool-point/remove")
    def remove_tool_point(body: dict):
        data = tool_job("annotating", "annotated", "solved")
        samples = [s for s in data.get("tool_samples", [])
                   if (s["episode"], s["point_id"]) != (body.get("episode"), body.get("point_id"))]
        return {"ok": True, "job": job().update(tool_samples=samples, tool_result=None, solved=False, step="annotating")}

    async def solve_generic():
        data = tool_job("annotating", "annotated", "solved")
        manifest = check_context(data)
        path = Path(manifest["path"]) / manifest.get("primary_file", "handeye_result_left.json")
        try:
            _, R, t, _ = mount_api._load_mount_calibration(path)
            result = await asyncio.to_thread(solve_tool, data.get("tool_samples", []), data["object_mode"], R, t)
        except (ValueError, OSError) as exc:
            raise fail(422, str(exc)) from exc
        if job().snapshot() != data:
            raise fail(409, "选点在求解期间已变化，请重新求解")
        check_context(data)
        result.update(arm=data["arm"], tool_id=data["tool_id"], extrinsic_artifact_id=data["extrinsic_artifact_id"])
        job().update(tool_result=result, step="solved", solved=True)
        return {"ok": True, "result": result}

    def archive_generic(body):
        data = tool_job("solved")
        result = data.get("tool_result")
        run_id = str(body.get("run_id") or "")
        if not result or not re.fullmatch(r"[^\W.][\w.-]{0,63}", run_id):
            raise fail(422, "需要有效求解结果和归档运行名")
        artifact_store = store()
        arm = f"{data['arm']}_arm"
        subject = canonical_subject("tcp_profile", unit_code=artifact_store.unit_code, arm=arm, tool_id=data["tool_id"])
        with tempfile.TemporaryDirectory(prefix="tool-tcp-") as directory:
            path = Path(directory) / "tcp_profile.json"
            path.write_text(json.dumps({"schema": "tcp-profile/1", **result}, ensure_ascii=False, indent=2))
            observations = Path(directory) / "tool_observations.json"
            observations.write_text(json.dumps(data.get("tool_samples", []), ensure_ascii=False, indent=2))
            try:
                manifest = artifact_store._write_artifact(
                    artifact_type="tcp_profile", camera_role=subject_key("tcp_profile", subject), run_id=run_id,
                    files=[path, observations], arm=arm, tool_id=data["tool_id"], camera_serial=data.get("camera_serial"),
                    tool="hand_eye_3D", source_run_dir=Path(data["run_dir"]), quality=result["residual_mm"],
                    extra={"primary_file": path.name, "source_method": data["object_mode"], "local_only": True,
                           "dependencies": [{"artifact_id": data["extrinsic_artifact_id"], "relation": "solved_with", "type": "extrinsic"}]},
                )
            except (ValueError, FileExistsError) as exc:
                raise fail(409, str(exc)) from exc
        job().update(finalized=True, step="finalized", artifacts={"tcp_profile": manifest["artifact_id"]})
        return {"ok": True, "local_only": True, "artifacts": {"tcp_profile": manifest}}

    return solve_generic, archive_generic
