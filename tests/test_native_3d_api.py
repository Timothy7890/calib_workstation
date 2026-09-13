from fastapi.testclient import TestClient

from calib_workstation.app import create_app
from calib_workstation.config import load_config


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
