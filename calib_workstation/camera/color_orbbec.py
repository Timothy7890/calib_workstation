"""Color-only Orbbec source for 2D cameras without an RGB-D calibration."""

from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np

from .orbbec import _usb_serial_from_uid


class ColorOrbbecSource:
    def __init__(self, serial: str):
        if not serial:
            raise ValueError("Orbbec序列号不能为空")
        self.serial = serial
        self._ob = None
        self._pipeline = None
        self._profile: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        import pyorbbecsdk as ob

        self._ob = ob
        context = ob.Context()
        devices = context.query_devices()
        entries = []
        for index in range(devices.get_count()):
            try:
                uid = str(devices.get_device_uid_by_index(index))
            except Exception:
                uid = ""
            try:
                serial = str(devices.get_device_serial_number_by_index(index))
            except Exception:
                serial = ""
            entries.append({"index": index, "uid": uid,
                            "serial": serial or _usb_serial_from_uid(uid)})
        entry = next((item for item in entries if item["serial"] == self.serial), None)
        if entry is None:
            raise RuntimeError(
                f"Orbbec {self.serial}不在线；已发现{[item['serial'] for item in entries]}"
            )
        device = (devices.get_device_by_uid(entry["uid"])
                  if entry["uid"] else devices.get_device_by_index(entry["index"]))
        pipeline = ob.Pipeline(device)
        profiles = pipeline.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR)
        profile = self._best_profile(ob, profiles)
        config = ob.Config()
        config.enable_stream(profile)
        intr = profile.get_intrinsic()
        distortion = profile.get_distortion()
        coefficients = [float(getattr(distortion, key)) for key in (
            "k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")]
        self._profile = {
            "serial": self.serial,
            "name": str(device.get_device_info().get_name()),
            "color": {
                "width": int(profile.get_width()), "height": int(profile.get_height()),
                "fps": int(profile.get_fps()),
                "format": str(profile.get_format()).rsplit(".", 1)[-1],
                "camera_matrix": [
                    [float(intr.fx), 0.0, float(intr.cx)],
                    [0.0, float(intr.fy), float(intr.cy)],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": coefficients, "distortion_model": "brown_conrady",
            },
            "depth": None, "synchronized": False, "purpose": "2d_calibration",
        }
        pipeline.start(config)
        self._pipeline = pipeline
        return dict(self._profile)

    @staticmethod
    def _best_profile(ob, profiles):
        preference = {ob.OBFormat.RGB: 4, ob.OBFormat.MJPG: 3,
                      ob.OBFormat.NV12: 2, ob.OBFormat.YUYV: 1}
        candidates = []
        for index in range(profiles.get_count()):
            try:
                profile = profiles.get_stream_profile_by_index(index).as_video_stream_profile()
            except Exception:
                continue
            if profile.get_format() not in preference or profile.get_fps() > 30:
                continue
            candidates.append(((int(profile.get_width()) * int(profile.get_height()),
                                int(profile.get_fps()), preference[profile.get_format()]), profile))
        return (max(candidates, key=lambda item: item[0])[1] if candidates
                else profiles.get_default_video_stream_profile())

    def read(self, timeout_s: float):
        if self._pipeline is None or self._ob is None:
            raise RuntimeError("ColorOrbbecSource尚未启动")
        frames = self._pipeline.wait_for_frames(max(1, int(timeout_s * 1000)))
        if frames is None:
            return None
        color = frames.get_color_frame()
        if color is None:
            return None
        width, height = color.get_width(), color.get_height()
        data = np.frombuffer(color.get_data(), np.uint8)
        fmt, ob = color.get_format(), self._ob
        if fmt == ob.OBFormat.RGB:
            image = cv2.cvtColor(data.reshape(height, width, 3), cv2.COLOR_RGB2BGR)
        elif fmt == ob.OBFormat.MJPG:
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        elif fmt == ob.OBFormat.YUYV:
            image = cv2.cvtColor(data.reshape(height, width, 2), cv2.COLOR_YUV2BGR_YUYV)
        elif fmt == ob.OBFormat.NV12:
            image = cv2.cvtColor(data.reshape(height * 3 // 2, width), cv2.COLOR_YUV2BGR_NV12)
        else:
            raise RuntimeError(f"不支持的Orbbec彩色格式{fmt}")
        if image is None:
            raise RuntimeError("Orbbec彩色帧解码失败")
        image.setflags(write=False)
        return {"timestamp_ns": time.time_ns(), "color": image,
                "metadata": {"profile": self._profile}}

    def stop(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass
