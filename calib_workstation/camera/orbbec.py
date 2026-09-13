"""Direct Orbbec source for the workstation's single-owner CameraManager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _usb_serial_from_uid(uid: str) -> str:
    """Gemini 335 may expose only a USB UID through the SDK."""
    if "-" not in uid:
        return ""
    topology = uid.rsplit("-", 1)[0]
    try:
        return (Path("/sys/bus/usb/devices") / topology / "serial").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def discover_orbbec() -> list[dict[str, Any]]:
    """Enumerate devices without intentionally starting any stream."""
    import pyorbbecsdk as ob

    context = ob.Context()
    try:
        context.set_logger_level(ob.OBLogLevel.ERROR)
    except Exception:
        pass
    devices = context.query_devices()
    result = []
    for index in range(devices.get_count()):
        try:
            uid = str(devices.get_device_uid_by_index(index))
        except Exception:
            uid = ""
        try:
            serial = str(devices.get_device_serial_number_by_index(index))
        except Exception:
            serial = ""
        try:
            name = str(devices.get_device_name_by_index(index))
        except Exception:
            name = "Orbbec"
        result.append({
            "serial": serial or _usb_serial_from_uid(uid),
            "name": name,
            "uid": uid,
            "index": index,
        })
    return result


class OrbbecSource:
    """One blocking RGB-D SDK pipeline, consumed by one manager worker.

    Shapes are ``(height, width)`` and are mandatory. A calibration made for a
    specific RGB-D profile must never be silently applied to another profile.
    """

    def __init__(
        self,
        serial: str,
        *,
        calibration_path: str | Path,
    ):
        if not serial:
            raise ValueError("Orbbec 序列号不能为空")
        self.calibration_path = Path(calibration_path).expanduser().resolve()
        try:
            calibration = json.loads(self.calibration_path.read_text(encoding="utf-8"))
            color = calibration["color"]
            depth = calibration["depth"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"无效的RGB-D标定文件 {self.calibration_path}: {exc}") from exc
        calibrated_serial = str((calibration.get("device") or {}).get("serial") or "")
        if calibrated_serial != serial:
            raise ValueError(
                f"RGB-D标定属于 {calibrated_serial!r}，不能用于相机 {serial!r}")
        self.serial = serial
        self.color_shape = (int(color["height"]), int(color["width"]))
        self.depth_shape = (int(depth["height"]), int(depth["width"]))
        self.color_fps = int(color["fps"])
        self.depth_fps = int(depth["fps"])
        self.color_format = str(color["format"]).upper()
        self.depth_format = str(depth["format"]).upper()
        self.expected_depth_scale_mm = float((calibration.get("depth_scale") or {})["value"])
        if min(*self.color_shape, *self.depth_shape, self.color_fps, self.depth_fps) <= 0:
            raise ValueError(f"RGB-D标定文件包含无效Profile: {self.calibration_path}")
        self._ob = None
        self._pipeline = None
        self._profile: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        import pyorbbecsdk as ob

        self._ob = ob
        context = ob.Context()
        try:
            context.set_logger_level(ob.OBLogLevel.ERROR)
        except Exception:
            pass
        devices = context.query_devices()
        inventory = self._inventory(devices)
        entry = next((item for item in inventory if item["serial"] == self.serial), None)
        if entry is None:
            raise RuntimeError(
                f"Orbbec {self.serial} 不在线；已发现 "
                f"{[item['serial'] for item in inventory]}")
        try:
            device = (
                devices.get_device_by_uid(entry["uid"])
                if entry.get("uid")
                else devices.get_device_by_index(entry["index"])
            )
        except Exception as exc:
            raise RuntimeError(
                f"无法独占打开 Orbbec {self.serial}；请确认外部推流和其他相机程序已停止"
            ) from exc

        pipeline = ob.Pipeline(device)
        color = self._select_profile(
            pipeline.get_stream_profile_list(ob.OBSensorType.COLOR_SENSOR),
            shape=self.color_shape,
            formats=(ob.OBFormat.RGB, ob.OBFormat.MJPG, ob.OBFormat.NV12, ob.OBFormat.YUYV),
            expected_format=self.color_format,
            expected_fps=self.color_fps,
            label="彩色",
        )
        depth = self._select_profile(
            pipeline.get_stream_profile_list(ob.OBSensorType.DEPTH_SENSOR),
            shape=self.depth_shape,
            formats=(ob.OBFormat.Y16,),
            expected_format=self.depth_format,
            expected_fps=self.depth_fps,
            label="深度",
        )
        config = ob.Config()
        config.enable_stream(color)
        config.enable_stream(depth)
        config.set_frame_aggregate_output_mode(ob.OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE)
        pipeline.enable_frame_sync()

        intr = color.get_intrinsic()
        distortion = color.get_distortion()
        coefficients = [
            float(getattr(distortion, key))
            for key in ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")
        ]
        profile = {
            "serial": self.serial,
            "name": str(device.get_device_info().get_name()),
            "color": {
                "width": int(color.get_width()), "height": int(color.get_height()),
                "fps": int(color.get_fps()), "format": self._format_name(color.get_format()),
                "camera_matrix": [
                    [float(intr.fx), 0.0, float(intr.cx)],
                    [0.0, float(intr.fy), float(intr.cy)],
                    [0.0, 0.0, 1.0],
                ],
                "distortion": coefficients,
                "distortion_model": "brown_conrady",
            },
            "depth": {
                "width": int(depth.get_width()), "height": int(depth.get_height()),
                "fps": int(depth.get_fps()), "format": self._format_name(depth.get_format()),
            },
            "synchronized": True,
        }
        try:
            pipeline.start(config)
        except Exception as exc:
            raise RuntimeError(
                f"Orbbec {self.serial} 无法同时启动指定RGB-D Profile: {profile}") from exc
        self._pipeline = pipeline
        self._profile = profile
        return dict(profile)

    @staticmethod
    def _inventory(devices) -> list[dict[str, Any]]:
        result = []
        for index in range(devices.get_count()):
            try:
                uid = str(devices.get_device_uid_by_index(index))
            except Exception:
                uid = ""
            try:
                serial = str(devices.get_device_serial_number_by_index(index))
            except Exception:
                serial = ""
            result.append({
                "serial": serial or _usb_serial_from_uid(uid),
                "uid": uid,
                "index": index,
            })
        return result

    def _select_profile(
        self, profiles, *, shape, formats, expected_format, expected_fps, label,
    ):
        candidates = []
        available = []
        for index in range(profiles.get_count()):
            try:
                profile = profiles.get_stream_profile_by_index(index).as_video_stream_profile()
            except Exception:
                continue
            height, width = int(profile.get_height()), int(profile.get_width())
            fps, fmt = int(profile.get_fps()), profile.get_format()
            available.append(f"{width}x{height}@{fps} {self._format_name(fmt)}")
            if (
                (height, width) != tuple(shape)
                or fps != expected_fps
                or fmt not in formats
                or self._format_name(fmt).upper() != expected_format
            ):
                continue
            format_rank = len(formats) - formats.index(fmt)
            candidates.append(((format_rank, fps), profile))
        if not candidates:
            raise RuntimeError(
                f"找不到标定要求的{label} Profile "
                f"{shape[1]}x{shape[0]}@{expected_fps} {expected_format}；"
                f"可用: {available}")
        return max(candidates, key=lambda item: item[0])[1]

    @staticmethod
    def _format_name(value: Any) -> str:
        return str(value).rsplit(".", 1)[-1]

    def read(self, timeout_s: float) -> dict[str, Any] | None:
        if self._pipeline is None or self._ob is None:
            raise RuntimeError("OrbbecSource 尚未启动")
        import time

        import cv2
        import numpy as np

        frames = self._pipeline.wait_for_frames(max(1, int(timeout_s * 1000)))
        if frames is None:
            return None
        frame_set = frames.as_frame_set()
        color = frame_set.get_color_frame()
        depth = frame_set.get_depth_frame()
        if color is None or depth is None:
            return None
        width, height = color.get_width(), color.get_height()
        data = np.frombuffer(color.get_data(), np.uint8)
        fmt = color.get_format()
        ob = self._ob
        if fmt == ob.OBFormat.RGB:
            bgr = cv2.cvtColor(data.reshape(height, width, 3), cv2.COLOR_RGB2BGR)
        elif fmt == ob.OBFormat.MJPG:
            bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
        elif fmt == ob.OBFormat.YUYV:
            bgr = cv2.cvtColor(data.reshape(height, width, 2), cv2.COLOR_YUV2BGR_YUYV)
        elif fmt == ob.OBFormat.NV12:
            bgr = cv2.cvtColor(data.reshape(height * 3 // 2, width), cv2.COLOR_YUV2BGR_NV12)
        else:
            raise RuntimeError(f"不支持的Orbbec彩色格式 {fmt}")
        if bgr is None:
            raise RuntimeError("Orbbec彩色帧解码失败")
        bgr.setflags(write=False)
        depth_width, depth_height = depth.get_width(), depth.get_height()
        depth_scale_mm = float(depth.get_depth_scale())
        if abs(depth_scale_mm - self.expected_depth_scale_mm) > 1e-6:
            raise RuntimeError(
                f"Orbbec depth scale {depth_scale_mm} 与标定 "
                f"{self.expected_depth_scale_mm} mm/raw 不一致")
        depth_z16 = np.frombuffer(depth.get_data(), np.uint16).reshape(
            depth_height, depth_width).copy()
        depth_z16.setflags(write=False)
        return {
            "timestamp_ns": time.time_ns(),
            "color": bgr,
            "depth": depth_z16,
            "depth_scale_mm": depth_scale_mm,
            "metadata": {"profile": self._profile},
        }

    def stop(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass
