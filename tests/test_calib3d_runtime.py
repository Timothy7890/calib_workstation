from pathlib import Path

import pytest

from calib_workstation.calib3d import runtime


def test_real_runtime_creates_record_directory_before_offline_backend(tmp_path, monkeypatch):
    class BackendReached(RuntimeError):
        pass

    def assert_existing_directory(task_dir, _calibration_path, *, arm):
        assert arm == "right"
        assert Path(task_dir).is_dir()
        raise BackendReached

    monkeypatch.setattr(runtime, "ManagedRGBDCamera", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "make_pose_provider", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runtime, "OfflineEpisodeBackend", assert_existing_directory)

    with pytest.raises(BackendReached):
        runtime.configure(
            object(),
            data_root=tmp_path / "workstation_data",
            rgbd_calibration_path=tmp_path / "rgbd.json",
            capability_url="http://127.0.0.1:18000",
            mock=False,
        )

    assert (
        tmp_path / "workstation_data" / "_hand_eye_3d" / "teleop" / "right"
    ).is_dir()
