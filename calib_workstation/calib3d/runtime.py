"""Initialize the vendored 3D backend inside the 18005 process."""

from __future__ import annotations

import os
from pathlib import Path

from ..camera import CameraManager
from . import app as app_module
from .managed_camera import ManagedRGBDCamera
from .offline import OfflineEpisodeBackend
from .robot import make_pose_provider


def configure(
    manager: CameraManager,
    *,
    data_root: Path,
    rgbd_calibration_path: Path,
    capability_url: str,
    mock: bool,
    hand_service_url: str = "http://127.0.0.1:18089",
) -> ManagedRGBDCamera:
    arm = os.environ.get("CALIB_DEFAULT_ARM", "right").strip()
    if arm not in ("left", "right"):
        arm = "right"
    network_interface = os.environ.get("NETWORK_INTERFACE", "enp86s0")
    camera = ManagedRGBDCamera(
        manager, rgbd_calibration_path, mock=mock
    )
    pose_provider = make_pose_provider(
        "mock" if mock else "h2",
        network_interface=None if mock else network_interface,
        arm=arm,
    )
    save_root = (data_root / "_hand_eye_3d" / "biaoding").resolve()
    save_path = save_root / arm
    record_root = (data_root / "_hand_eye_3d" / "teleop").resolve()
    record_task_dir = record_root / arm
    replay_runs = (data_root.parent / "calibration_replay_data" / "runs").resolve()
    episode_backend = None
    if not mock:
        # 这是工作站自己的采集输出目录。首次部署时它尚不存在，而
        # OfflineEpisodeBackend 是只读校验器，要求传入目录已经存在。
        record_task_dir.mkdir(parents=True, exist_ok=True)
        episode_backend = OfflineEpisodeBackend(
            record_task_dir, rgbd_calibration_path, arm=arm
        )

    app_module.camera = camera
    app_module.pose_provider = pose_provider
    app_module.arm_side = arm
    app_module.arm_factory = None
    app_module.arm_controller = None
    app_module.save_path = save_path
    app_module.save_root = save_root
    app_module.episode_task_roots = [record_root, replay_runs]
    app_module.offline_backend = None
    app_module.episode_backend = episode_backend
    app_module.teleop_task_dir = None
    app_module.record_task_dir = None if mock else record_task_dir
    app_module.rgbd_calib_path = rgbd_calibration_path.resolve()
    app_module.mount_calib_path = None
    app_module.mount_profile_dir = (
        data_root / "_hand_eye_3d" / "mount_model_profiles"
    ).resolve()
    app_module.capability_url = capability_url.rstrip("/")
    app_module.hand_service_url = hand_service_url.rstrip("/")
    app_module.capability_snapshot = None
    app_module.workstation_data_root = data_root.resolve()
    app_module.init_state()
    return camera
