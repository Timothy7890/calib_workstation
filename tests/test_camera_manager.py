import json
import threading
import time

import pytest

from calib_workstation.camera import CameraManager
from calib_workstation.camera.orbbec import OrbbecSource
from calib_workstation.camera.color_orbbec import ColorOrbbecSource


class FakeSource:
    starts = 0
    stops = 0

    def __init__(self, serial):
        self.serial = serial
        self.next_frame = threading.Event()
        self.index = 0
        self.closed = False

    def start(self):
        type(self).starts += 1
        return {"serial": self.serial, "color": [1920, 1080], "depth": [640, 576]}

    def read(self, timeout_s):
        if not self.next_frame.wait(timeout_s):
            return None
        self.next_frame.clear()
        self.index += 1
        return {
            "timestamp_ns": time.time_ns(),
            "color": f"color-{self.index}",
            "depth": f"depth-{self.index}",
            "depth_scale_mm": 1.0,
        }

    def stop(self):
        if not self.closed:
            type(self).stops += 1
            self.closed = True
        self.next_frame.set()


@pytest.fixture(autouse=True)
def reset_counts():
    FakeSource.starts = 0
    FakeSource.stops = 0


def manager_and_sources():
    sources = {}

    def factory(serial):
        source = FakeSource(serial)
        sources[serial] = source
        return source

    manager = CameraManager(factory, lambda: [
        {"serial": "CAM-1", "name": "Gemini 335"},
        {"serial": "CAM-2", "name": "Gemini 335"},
    ])
    return manager, sources


def test_one_pipeline_fans_same_frame_to_2d_and_3d():
    manager, sources = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    manager.acquire("head", "calib2d")
    manager.acquire("head", "calib3d")
    assert FakeSource.starts == 1

    sources["CAM-1"].next_frame.set()
    frame_2d = manager.wait_frame("head", consumer="calib2d")
    frame_3d = manager.wait_frame("head", consumer="calib3d", require_depth=True)

    assert frame_2d is frame_3d
    assert frame_2d.color == "color-1"
    assert frame_3d.depth == "depth-1"
    manager.close()
    assert FakeSource.stops == 1


def test_release_keeps_device_owned_until_manager_closes():
    manager, _ = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    manager.acquire("head", "calib2d")
    manager.release("head", "calib2d")

    assert manager.states()[0].running
    assert manager.states()[0].consumers == ()
    assert FakeSource.stops == 0
    manager.close()
    assert FakeSource.stops == 1


def test_roles_cannot_alias_one_physical_camera():
    manager, _ = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    with pytest.raises(ValueError, match="不能同时冒充"):
        manager.bind_role("waist", "CAM-1")


def test_role_cannot_switch_while_camera_is_open():
    manager, _ = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    manager.acquire("head", "calib2d")
    with pytest.raises(RuntimeError, match="运行中不能切换"):
        manager.bind_role("head", "CAM-2")
    manager.close()


def test_consumer_must_acquire_before_reading():
    manager, _ = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    manager.acquire("head", "calib2d")
    with pytest.raises(PermissionError, match="尚未 acquire"):
        manager.wait_frame("head", consumer="calib3d", timeout_s=0.01)
    manager.close()


def test_device_inventory_marks_owned_camera():
    manager, _ = manager_and_sources()
    manager.bind_role("head", "CAM-1")
    manager.acquire("head", "calib2d")
    devices = manager.devices()
    assert devices[0]["owned"] is True
    assert devices[0]["busy"] is False
    assert devices[0]["role"] == "head"
    assert devices[1]["owned"] is False
    manager.close()


class FakeProfile:
    def __init__(self, width, height, fps, fmt):
        self.values = width, height, fps, fmt

    def as_video_stream_profile(self):
        return self

    def get_width(self): return self.values[0]
    def get_height(self): return self.values[1]
    def get_fps(self): return self.values[2]
    def get_format(self): return self.values[3]


class FakeProfiles:
    def __init__(self, *profiles): self.profiles = profiles
    def get_count(self): return len(self.profiles)
    def get_stream_profile_by_index(self, index): return self.profiles[index]


def orbbec_source(tmp_path):
    path = tmp_path / "rgbd.json"
    path.write_text(json.dumps({
        "device": {"serial": "CAM-1"},
        "color": {"width": 1920, "height": 1080, "fps": 30, "format": "MJPG"},
        "depth": {"width": 1280, "height": 800, "fps": 30, "format": "Y16"},
        "depth_scale": {"value": 1.0},
    }))
    return OrbbecSource("CAM-1", calibration_path=path)


def test_orbbec_profile_selection_is_calibration_strict(tmp_path):
    source = orbbec_source(tmp_path)
    profiles = FakeProfiles(
        FakeProfile(1280, 720, 30, "RGB"),
        FakeProfile(1920, 1080, 15, "MJPG"),
        FakeProfile(1920, 1080, 30, "MJPG"),
    )

    selected = source._select_profile(
        profiles, shape=(1080, 1920), formats=("RGB", "MJPG"),
        expected_format="MJPG", expected_fps=30, label="彩色")

    assert selected.get_width() == 1920
    assert selected.get_fps() == 30


def test_orbbec_profile_selection_refuses_silent_fallback(tmp_path):
    source = orbbec_source(tmp_path)
    profiles = FakeProfiles(FakeProfile(1280, 720, 30, "RGB"))

    with pytest.raises(RuntimeError, match="找不到标定要求"):
        source._select_profile(
            profiles, shape=(1080, 1920), formats=("RGB", "MJPG"),
            expected_format="MJPG", expected_fps=30, label="彩色")


def test_orbbec_source_rejects_calibration_from_another_camera(tmp_path):
    source = orbbec_source(tmp_path)
    with pytest.raises(ValueError, match="不能用于"):
        OrbbecSource("CAM-2", calibration_path=source.calibration_path)


def test_color_only_source_selects_best_2d_profile():
    class OBFormat:
        RGB = "RGB"
        MJPG = "MJPG"
        NV12 = "NV12"
        YUYV = "YUYV"

    Formats = type("Formats", (), {"OBFormat": OBFormat})

    profiles = FakeProfiles(
        FakeProfile(1280, 720, 30, "RGB"),
        FakeProfile(1920, 1080, 30, "MJPG"),
        FakeProfile(1920, 1080, 15, "RGB"),
    )
    selected = ColorOrbbecSource._best_profile(Formats, profiles)
    assert (selected.get_width(), selected.get_height(), selected.get_fps()) == (1920, 1080, 30)
