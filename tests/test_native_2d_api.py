from fastapi.testclient import TestClient

from calib_workstation.app import create_app
from calib_workstation.config import load_config


def test_native_2d_compatibility_api(tmp_path):
    config_path = tmp_path / "workstation.yaml"
    config_path.write_text(f"""
robot:
  vendor: unitree
  model: h2
services: {{}}
data_root: {tmp_path / 'data'}
cameras:
  head:
    label: 头部相机
    target: hand_eye_2D_head
board:
  size: 11x8
  square_size_mm: 20
""")
    app = create_app(load_config(config_path, mock=True))
    with TestClient(app) as client:
        devices = client.get("/api/camera/devices")
        assert devices.status_code == 200
        selected = client.post("/api/camera/select", json={
            "serial": "MOCK-HEAD-0001", "camera_role": "head"})
        assert selected.status_code == 200, selected.text
        detected = client.post("/api/checkerboard/detect", json={})
        assert detected.json()["found"] is True
        run_dir = tmp_path / "run"
        session = client.post("/api/session/start", json={
            "run_id": "run", "arm": "right", "camera_role": "head",
            "record_dir": str(run_dir),
        })
        assert session.status_code == 200, session.text
        capture = client.post("/api/capture", json={
            "run_id": "run", "arm": "right", "capture_id": "one",
            "require_corners": True,
            "stability": {"measured_q_rad": [0.0] * 7},
        })
        assert capture.status_code == 200, capture.text
        assert (run_dir / "left/0000.jpg").is_file()
