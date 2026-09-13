from pathlib import Path

from calib_workstation.calib2d import Calib2DEngine
from calib_workstation.camera import CameraManager, MockSource


def test_native_2d_capture_uses_shared_frame_and_stability_joints(tmp_path: Path):
    manager = CameraManager(lambda serial: MockSource(serial))
    engine = Calib2DEngine(manager, tmp_path)
    engine.select("head", "MOCK-HEAD")
    run_dir = tmp_path / "run-1"
    session = engine.start_session({
        "run_id": "run-1", "arm": "right", "camera_role": "head",
        "record_dir": str(run_dir),
    })

    result = engine.capture({
        "run_id": "run-1", "arm": "right", "capture_id": "capture-1",
        "require_corners": True,
        "stability": {"measured_q_rad": [0.1] * 7},
    })

    assert session["save_path"] == str(run_dir)
    assert result["corners_detected"] is True
    assert (run_dir / "left/0000.jpg").is_file()
    assert (run_dir / "joints/0000.json").is_file()
    assert (run_dir / "camera_intrinsics.json").is_file()
    duplicate = engine.capture({
        "run_id": "run-1", "arm": "right", "capture_id": "capture-1",
        "stability": {"measured_q_rad": [0.2] * 7},
    })
    assert duplicate["duplicate"] is True
    assert len(list((run_dir / "left").glob("*.jpg"))) == 1
    manager.close()
