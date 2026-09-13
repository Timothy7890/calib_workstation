import json
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from calib_workstation.app import Job
from calib_workstation.contract import normalize_manifest
from calib_workstation.manifest import ArtifactStore
from calib_workstation.tool_calibration import solve_tool
from calib_workstation.tool_workflow import install_object_routes
from calib_workstation.calib3d import app as engine, mount_api


def observations(points):
    rows = []
    for i in range(3):
        angle = i * 0.4
        c, s = np.cos(angle), np.sin(angle)
        T = np.eye(4)
        T[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
        T[:3, 3] = [i * .1, .2, .3]
        for key, p in points.items():
            rows.append(dict(episode=f"episode_{i:04d}", point_id=key,
                             qpos_median_rad=[angle, 0, 0, 0, 0, 0, 0],
                             p_camera=(T[:3, :3] @ np.asarray(p) + T[:3, 3]).tolist(),
                             T_base_wrist=T.tolist(), wrist_link="left_wrist_yaw_link", pixel=[100, 100]))
    return rows


def test_single_tcp_has_position_but_no_invented_orientation():
    result = solve_tool(observations({"tcp": [.1, -.02, .15]}), "tcp", np.eye(3), np.zeros(3))
    np.testing.assert_allclose(result["tcp_points_wrist_m"][0]["p_wrist_m"], [.1, -.02, .15])
    assert not result["orientation_defined"]
    assert "T_wrist2tool" not in result
    assert result["residual_mm"]["rms"] < 1e-8


def test_tool_frame_and_degenerate_data():
    rows = observations({"origin": [.1, 0, 0], "x": [.2, 0, 0], "xy": [.1, .1, 0]})
    result = solve_tool(rows, "tool", np.eye(3), np.zeros(3))
    np.testing.assert_allclose(np.asarray(result["T_wrist2tool"])[:3, :3], np.eye(3), atol=1e-10)
    with pytest.raises(ValueError, match="共线"):
        solve_tool(observations({"origin": [0, 0, 0], "x": [.1, 0, 0], "xy": [.2, 0, 0]}), "tool", np.eye(3), np.zeros(3))
    with pytest.raises(ValueError, match="3个"):
        solve_tool(rows[:2], "tool", np.eye(3), np.zeros(3))
    repeated = observations({"tcp": [.1, 0, 0]})
    for s in repeated:
        s["qpos_median_rad"] = [0] * 7
    with pytest.raises(ValueError, match="重复"):
        solve_tool(repeated, "tcp", np.eye(3), np.zeros(3))


def test_generic_workflow_without_hand_and_archive_is_not_a_hand(tmp_path, monkeypatch):
    job = Job(tmp_path / "job.json")
    job.update(calibration_kind="3d", step="annotating", arm="left", camera_role="head",
               run_dir=str(tmp_path), extrinsic_artifact_id="camera-1")
    artifact_store = ArtifactStore(tmp_path / "calibrations", unit_code="H2-TEST", vendor="test", model="H2")
    app = FastAPI()
    def fail(code, message):
        return HTTPException(code, message)
    def require(*steps):
        data = job.snapshot()
        if data["step"] not in steps:
            raise fail(409, "step")
        return data
    monkeypatch.setattr(engine, "_available_episode_backend", lambda: SimpleNamespace(task_dir=tmp_path))
    monkeypatch.setattr(mount_api, "_load_mount_calibration", lambda _: ({}, np.eye(3), np.zeros(3), {}))
    rows = observations({"tcp": [.1, .02, .15]})
    async def pick(body):
        return dict(next(s for s in rows if s["episode"] == body["episode"]))
    monkeypatch.setattr(engine, "api_offline_pick", pick)
    solve, archive = install_object_routes(app, job=lambda: job, require_job=require,
        registry=lambda: {"active": {"arm": "left_arm"}},
        extrinsic=lambda _: {"artifact_id": "camera-1", "path": str(tmp_path)},
        store=lambda: artifact_store, fail=fail, payload=lambda x: x)
    app.post("/solve")(solve)
    @app.post("/archive")
    def archive_endpoint(body: dict):
        return archive(body)
    with TestClient(app) as client:
        assert client.post("/api/hand-calibration/object", json={"mode": "tcp", "tool_id": "probe-1"}).status_code == 200
        for s in rows:
            assert client.post("/api/hand-calibration/tool-point", json=s).status_code == 200
        assert client.post("/solve").status_code == 200
        # An edit invalidates the previous solve before anything can be archived.
        assert client.post("/api/hand-calibration/tool-point/remove", json=rows[0]).status_code == 200
        assert client.post("/archive", json={"run_id": "one"}).status_code == 409
        assert client.post("/solve").status_code == 422
        client.post("/api/hand-calibration/tool-point", json=rows[0])
        assert client.post("/solve").status_code == 200
        response = client.post("/archive", json={"run_id": "one"})
        assert response.status_code == 200, response.text
        manifest = response.json()["artifacts"]["tcp_profile"]
        assert normalize_manifest(manifest)["subject"] == {
            "kind": "tool", "unit_code": "H2-TEST", "arm": "left_arm", "tool_id": "probe-1"}
        assert "hand_id" not in manifest["subject"]
        assert manifest["local_only"] is True
        path = artifact_store.artifact_dir("tcp_profile", manifest["subject_key"], "one") / "tcp_profile.json"
        assert json.loads(path.read_text())["orientation_defined"] is False


def test_hand_hold_uses_device_mapping_without_geometry_catalog(monkeypatch):
    calls = []
    async def hint():
        return dict(available=True, hand_id="qiangnao-revo2-left", arm="left",
                    design_side="left", hand_web_device_id="brainco_revo2")
    state = SimpleNamespace(get_capability_hint=hint, hand_hold=SimpleNamespace(
        start=lambda device, side: calls.append((device, side)) or {"running": True}))
    monkeypatch.setattr(mount_api, "_state", lambda: state)
    def forbidden(*args):
        raise AssertionError("zero hold must not load a geometric model")
    monkeypatch.setattr(mount_api, "get_hand_model", forbidden)
    result = asyncio.run(mount_api.api_mount_hand_hold_start({"hand_id": "qiangnao-revo2-left", "side": "left"}))
    assert result["ok"]
    assert calls == [("brainco_revo2", "left")]
    result = asyncio.run(mount_api.api_mount_hand_hold_start({"hand_id": "qiangnao-revo2-left", "side": "right"}))
    assert result.status_code == 409
    assert len(calls) == 1
