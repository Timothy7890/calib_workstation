"""3D camera facade backed by the workstation's single CameraManager pipeline."""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..camera import CameraManager
from .rgbd import RGBDCalibration, SoftwareDepthAligner


class ManagedRGBDCamera:
    """Expose the legacy 3D camera protocol without opening another SDK device."""

    source = "workstation_camera_manager"

    def __init__(
        self,
        manager: CameraManager,
        calibration_path: str | Path,
        *,
        mock: bool = False,
    ):
        self.manager = manager
        self.mock = bool(mock)
        self.calibration = (
            None if self.mock else RGBDCalibration.from_file(calibration_path)
        )
        self.aligner = (
            None if self.calibration is None else SoftwareDepthAligner(self.calibration)
        )
        self.role: str | None = None
        self.serial: str | None = None
        self.consumer = "calib3d"
        self._lock = threading.RLock()
        self._last_sequence = -1
        self._last_aligned_sequence = -1
        self._last_preview: np.ndarray | None = None
        self._history: deque[np.ndarray] = deque(maxlen=8)

    def select(self, role: str, serial: str) -> dict[str, Any]:
        if self.calibration is not None and self.calibration.serial not in (None, serial):
            raise ValueError(
                f"RGB-D标定属于 {self.calibration.serial}，不能用于相机 {serial}"
            )
        with self._lock:
            self.manager.bind_role(role, serial)
            self.manager.acquire(role, self.consumer)
            self.role, self.serial = role, serial
            self._history.clear()
            self._last_sequence = -1
            self._last_aligned_sequence = -1
            self._last_preview = None
        return self.info()

    def supports(self, serial: str) -> bool:
        return self.mock or self.calibration is None or self.calibration.serial in (None, serial)

    def start(self) -> None:
        """Lifecycle is owned by CameraManager; selection starts the pipeline."""

    def stop(self) -> None:
        if self.role:
            try:
                self.manager.release(self.role, self.consumer)
            except RuntimeError:
                pass

    def _frame(self, *, after_sequence: int = -1, timeout_s: float = 2.0):
        if not self.role or not self.serial:
            raise RuntimeError("尚未在18005选择3D相机")
        return self.manager.wait_frame(
            self.role,
            consumer=self.consumer,
            after_sequence=after_sequence,
            require_depth=True,
            timeout_s=timeout_s,
        )

    def _profile(self) -> dict[str, Any]:
        if not self.serial:
            return {}
        state = next(
            (item for item in self.manager.states() if item.serial == self.serial),
            None,
        )
        return {} if state is None else dict(state.profile)

    def info(self) -> dict[str, Any]:
        profile = self._profile()
        color = dict(profile.get("color") or {})
        return {
            "source": self.source,
            "connected": bool(profile),
            "serial": self.serial,
            "name": profile.get("name"),
            "width": color.get("width"),
            "height": color.get("height"),
            "recording_supported": not self.mock,
            "single_owner": True,
        }

    def get_jpeg(self) -> bytes | None:
        frame = self._frame(timeout_s=3.0)
        ok, encoded = cv2.imencode(
            ".jpg", np.asarray(frame.color), [cv2.IMWRITE_JPEG_QUALITY, 85]
        )
        return encoded.tobytes() if ok else None

    def _aligned(self, *, after_sequence: int = -1, timeout_s: float = 2.0):
        frame = self._frame(after_sequence=after_sequence, timeout_s=timeout_s)
        with self._lock:
            if frame.sequence != self._last_aligned_sequence:
                raw = np.asarray(frame.depth)
                if self.mock:
                    aligned = raw.astype(np.float32) * float(frame.depth_scale_mm)
                else:
                    if not np.isclose(
                        float(frame.depth_scale_mm),
                        self.calibration.depth_scale_mm,
                        rtol=0.0,
                        atol=1e-6,
                    ):
                        raise RuntimeError(
                            f"depth scale {frame.depth_scale_mm} 与RGB-D标定 "
                            f"{self.calibration.depth_scale_mm} 不一致"
                        )
                    aligned = self.aligner.align(raw)
                self._last_preview = aligned
                self._history.append(aligned)
                self._last_aligned_sequence = frame.sequence
            return frame, self._last_preview

    def _intrinsics(self) -> tuple[float, float, float, float]:
        if self.calibration is not None:
            return self.calibration.color_intrinsics
        matrix = np.asarray((self._profile().get("color") or {}).get("camera_matrix"))
        if matrix.shape != (3, 3):
            raise RuntimeError("相机Profile缺少彩色内参")
        return float(matrix[0, 0]), float(matrix[1, 1]), float(matrix[0, 2]), float(matrix[1, 2])

    def depth_preview_snapshot(self):
        _frame, aligned = self._aligned(timeout_s=3.0)
        return np.array(aligned, copy=True), self._intrinsics()

    def depth_snapshot(self):
        self._aligned(timeout_s=3.0)
        with self._lock:
            if not self._history:
                return None
            depth = np.median(np.stack(tuple(self._history)), axis=0).astype(np.float32)
        return depth, self._intrinsics()

    def pick(self, u: int, v: int, win: int = 5) -> dict[str, Any]:
        snapshot = self.depth_snapshot()
        if snapshot is None:
            return {"ok": False, "error": "尚无深度帧"}
        depth, (fx, fy, cx, cy) = snapshot
        height, width = depth.shape
        if not (0 <= u < width and 0 <= v < height):
            return {"ok": False, "error": "像素坐标超出图像范围"}
        radius = max(1, int(win))
        patch = depth[max(0, v - radius):min(height, v + radius + 1),
                      max(0, u - radius):min(width, u + radius + 1)]
        valid = patch[np.isfinite(patch) & (patch >= 60.0) & (patch <= 15000.0)]
        if valid.size == 0:
            return {"ok": False, "error": "点击区域没有有效深度", "pixel": [u, v]}
        z_mm = float(np.median(valid))
        z = z_mm / 1000.0
        return {
            "ok": True,
            "p_camera": [(u - cx) * z / fx, (v - cy) * z / fy, z],
            "depth_mm": z_mm,
            "valid_ratio": float(valid.size / patch.size),
            "pixel": [u, v],
        }

    def wait_record_frame(self, after_sequence: int, timeout_s: float = 2.0) -> dict[str, Any]:
        frame = self._frame(after_sequence=after_sequence, timeout_s=timeout_s)
        return {
            "sequence": frame.sequence,
            "timestamp_ns": frame.timestamp_ns,
            "color_bgr": np.asarray(frame.color),
            "depth_z16": np.asarray(frame.depth),
            "depth_scale_mm": float(frame.depth_scale_mm),
        }
