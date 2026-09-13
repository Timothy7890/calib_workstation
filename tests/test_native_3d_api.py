import json
import pytest

from fastapi.testclient import TestClient

import calib_workstation.app as workstation_app
from calib_workstation.app import create_app
from calib_workstation.config import load_config
from calib_workstation.manifest import ArtifactStore


def test_2d_and_3d_share_one_managed_rgbd_pipeline(tmp_path):
    config = load_config(mock=True)
    config.data_root = tmp_path / "data"
    app = create_app(config)

    with TestClient(app) as client:
        selected = client.post(
            "/api/camera/select",
            json={"serial": "MOCK-HEAD-0001", "camera_role": "head"},
        )
        assert selected.status_code == 200

        status = client.get("/three-d/api/status")
        assert status.status_code == 200
        assert status.json()["camera"]["serial"] == "MOCK-HEAD-0001"
        assert status.json()["camera"]["single_owner"] is True

        image = client.get("/three-d/api/frame.jpg")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")

        picked = client.post("/three-d/api/pick", json={"u": 320, "v": 240})
        assert picked.status_code == 200
        assert picked.json()["ok"] is True
        assert picked.json()["depth_mm"] == 1000.0

        states = app.state.camera_manager.states()
        assert len(states) == 1
        assert states[0].serial == "MOCK-HEAD-0001"
        assert states[0].consumers == ("calib2d", "calib3d")


@pytest.mark.parametrize("hand_id", [None, "unregistered-hand"])
def test_3d_wizard_prepares_automatic_capture_with_active_context(tmp_path, monkeypatch, hand_id):
    plan = {
        "id": "plan-3d", "name": "3D right", "target": "hand_eye_3D",
        "base_url": "http://127.0.0.1:18005/three-d", "arm": "right",
        "camera_serial": None, "draft": False,
        "nodes": [
            {"id": "home", "role": "home", "enabled": True},
            {"id": "sample", "role": "sample", "enabled": True},
        ],
    }

    class FakeHttpClient:
        def __init__(self, name, _base_url):
            self.name = name

        def get(self, path, **_kwargs):
            if "能力中心" in self.name:
                return {"registry": {
                    "active": {"arm": "right_arm", "hand_id": hand_id, "camera_role": "head"},
                    "hands": [{"id": "hand-r", "name": "Right hand"}],
                }}
            if path == "/api/status":
                return {"state": "idle", "arm": {"arm": "right", "engaged": False}, "captures": []}
            if path == "/api/plans":
                return {"plans": [dict(plan)]}
            if path == "/api/plans/plan-3d":
                return dict(plan)
            raise AssertionError(path)

        def post(self, _path, _body=None, **_kwargs):
            return {"ok": True}

        def put(self, path, body, **_kwargs):
            assert path == "/api/plans/plan-3d"
            plan.update(body)
            return dict(plan)

        def reachable(self, _path="/api/status"):
            return True, ""

    monkeypatch.setattr(workstation_app, "HttpClient", FakeHttpClient)
    config = load_config(mock=True)
    config.data_root = tmp_path / "data"
    app = create_app(config)

    with TestClient(app) as client:
        assert client.put("/api/robot", json={"unit_code": "H2-TEST"}).status_code == 200
        run_dir = tmp_path / "solved-2d"
        run_dir.mkdir()
        (run_dir / "handeye_result_left.json").write_text(json.dumps({
            "base_link": "torso_link", "tip_link": "right_wrist_yaw_link",
            "T_cam2base": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        }), encoding="utf-8")
        (run_dir / "camera_intrinsics.json").write_text(json.dumps({
            "serial": "MOCK-HEAD-0001", "width": 640, "height": 480,
        }), encoding="utf-8")
        store = ArtifactStore(
            config.data_root / "H2-TEST" / "calibrations",
            unit_code="H2-TEST", vendor=config.vendor, model=config.model,
        )
        artifacts = store.finalize_2d_run(
            run_dir=run_dir, camera_role="head", run_id="camera-1", arm="right_arm",
        )
        for artifact_type in artifacts:
            store.set_active(artifact_type, "head", "camera-1")

        plans = client.get("/api/hand-calibration/plans?arm=right")
        assert plans.status_code == 200
        assert plans.json()["plans"][0]["id"] == "plan-3d"

        prepared = client.post("/api/hand-calibration/prepare", json={
            "camera_role": "head", "arm": "right",
            "camera_serial": "MOCK-HEAD-0001", "plan_id": "plan-3d",
        })
        assert prepared.status_code == 200
        job = prepared.json()["job"]
        assert job["calibration_kind"] == "3d"
        assert job["step"] == "prepared"
        assert job["sample_total"] == 1
        assert job["hand_id"] == hand_id
        assert plan["hold_hand_zero"] is False

        overview = client.get("/api/hand-calibration")
        assert overview.status_code == 200
        assert overview.json()["annotation"]["usable_point_count"] == 0

        app.state.calib3d_camera.serial = None
        monkeypatch.setattr(
            app.state.calib3d_camera,
            "select",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("camera busy")),
        )
        occupied = client.get("/api/hand-calibration")
        assert occupied.status_code == 200
        assert occupied.json()["service"] == {"ok": False, "error": "camera busy"}

        solve = client.post("/api/hand-calibration/solve", json={"camera_role": "head"})
        assert solve.status_code == 409
        assert "当前步骤" in solve.json()["detail"]["message"]
