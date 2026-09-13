"""Deterministic RGB-D source for workstation integration tests."""

from __future__ import annotations

import time

import cv2
import numpy as np


class MockSource:
    def __init__(self, serial: str):
        self.serial = serial
        self._index = 0
        self._closed = False

    def start(self):
        return {
            "serial": self.serial, "name": "Mock Orbbec",
            "color": {
                "width": 640, "height": 480, "fps": 30, "format": "BGR",
                "camera_matrix": [[600.0, 0.0, 320.0], [0.0, 600.0, 240.0], [0.0, 0.0, 1.0]],
                "distortion": [0.0] * 8, "distortion_model": "brown_conrady",
            },
            "depth": {"width": 640, "height": 480, "fps": 30, "format": "Y16"},
            "synchronized": True,
        }

    def read(self, timeout_s):
        if self._closed:
            return None
        time.sleep(min(timeout_s, 1 / 30))
        image = np.full((480, 640, 3), 235, np.uint8)
        square = 36
        cols, rows = 12, 9
        x0, y0 = 104, 78
        for row in range(rows):
            for col in range(cols):
                color = 25 if (row + col) % 2 == 0 else 245
                cv2.rectangle(
                    image,
                    (x0 + col * square, y0 + row * square),
                    (x0 + (col + 1) * square, y0 + (row + 1) * square),
                    (color, color, color), -1,
                )
        self._index += 1
        image.setflags(write=False)
        depth = np.full((480, 640), 1000, np.uint16)
        depth.setflags(write=False)
        return {
            "timestamp_ns": time.time_ns(), "color": image, "depth": depth,
            "depth_scale_mm": 1.0, "metadata": {"mock_index": self._index},
        }

    def stop(self):
        self._closed = True
