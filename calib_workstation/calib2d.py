"""Native 2D capture/session engine for the unified workstation."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .camera import CameraManager

JOINT_NAMES = {
    arm: [f"{arm}_{name}" for name in (
        "shoulder_pitch_joint", "shoulder_roll_joint", "shoulder_yaw_joint",
        "elbow_joint", "wrist_roll_joint", "wrist_pitch_joint", "wrist_yaw_joint",
    )]
    for arm in ("left", "right")
}


class Calib2DEngine:
    consumer = "calib2d"

    def __init__(self, manager: CameraManager, sessions_root: Path, board_size=(11, 8),
                 urdf_path: Path | None = None):
        self.manager = manager
        self.sessions_root = sessions_root.resolve()
        self.board_size = tuple(board_size)
        self.lock = threading.RLock()
        self.role: str | None = None
        self.serial: str | None = None
        self.run_id: str | None = None
        self.save_path: Path | None = None
        self.arm = "right"
        self.capture_count = 0
        self.capture_ids: dict[str, dict[str, Any]] = {}
        self.urdf_path = urdf_path
        self.solve_job: dict[str, Any] = {
            "running": False, "session": "", "log": [], "result": None, "error": ""}
        self.solve_lock = threading.Lock()

    def select(self, role: str, serial: str) -> dict[str, Any]:
        with self.lock:
            self.manager.bind_role(role, serial)
            self.manager.acquire(role, self.consumer)
            self.role, self.serial = role, serial
            return self.camera_info()

    def camera_info(self) -> dict[str, Any]:
        if not self.serial:
            return {"connected": False}
        state = next((item for item in self.manager.states() if item.serial == self.serial), None)
        profile = dict(state.profile) if state else {}
        color = profile.get("color") or {}
        return {
            "connected": state is not None and state.running,
            "serial": self.serial, "name": profile.get("name"),
            "width": color.get("width"), "height": color.get("height"),
            "fps": color.get("fps"), "format": color.get("format"),
            "source": "workstation_camera_manager",
            "single_camera": True,
        }

    def start_session(self, body: dict[str, Any]) -> dict[str, Any]:
        run_id = str(body.get("run_id") or "").strip()
        arm = str(body.get("arm") or "").strip()
        role = str(body.get("camera_role") or self.role or "").strip()
        if not run_id or any(char in run_id for char in "/\\ "):
            raise ValueError("非法 run_id")
        if arm not in JOINT_NAMES:
            raise ValueError("arm 必须是 left 或 right")
        record_dir = Path(str(body.get("record_dir") or self.sessions_root / run_id)).expanduser()
        if not record_dir.is_absolute():
            raise ValueError("record_dir 必须是绝对路径")
        existing = len(list((record_dir / "left").glob("*.jpg")))
        if existing and not body.get("resume"):
            raise FileExistsError(f"{record_dir} 已有 {existing} 个样本")
        with self.lock:
            self.run_id, self.arm, self.role = run_id, arm, role
            self.save_path = record_dir.resolve()
            for name in ("left", "joints"):
                (self.save_path / name).mkdir(parents=True, exist_ok=True)
            self.capture_count = existing
            self.capture_ids.clear()
            self._write_session_meta()
            return self.status()

    def detect(self) -> dict[str, Any]:
        frame = self._frame()
        found, corners = detect_corners(frame.color, self.board_size)
        return {"success": True, "found": found,
                "corner_count": 0 if corners is None else len(corners)}

    def capture(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.save_path is None or self.run_id is None:
                raise RuntimeError("尚未开始采集会话")
            if body.get("run_id") not in (None, "", self.run_id):
                raise ValueError("run_id 与当前会话不一致")
            if body.get("arm") not in (None, "", self.arm):
                raise ValueError("arm 与当前会话不一致")
            capture_id = str(body.get("capture_id") or "").strip()
            if capture_id and capture_id in self.capture_ids:
                return {**self.capture_ids[capture_id], "duplicate": True}
            frame = self._frame()
            found, _ = detect_corners(frame.color, self.board_size)
            if body.get("require_corners") and not found:
                raise RuntimeError("未检出完整棋盘格，拒绝采集")
            stability = body.get("stability") or {}
            q = np.asarray(stability.get("measured_q_rad"), dtype=float).reshape(-1)
            if q.shape != (7,) or not np.all(np.isfinite(q)):
                raise ValueError("采集请求缺少18004稳定性证书中的7轴实测关节角")
            index = self.capture_count
            stem = f"{index:04d}"
            image_path = self.save_path / "left" / f"{stem}.jpg"
            joints_path = self.save_path / "joints" / f"{stem}.json"
            if not cv2.imwrite(str(image_path), frame.color):
                raise OSError(f"保存图像失败: {image_path}")
            record = {
                "index": index, "timestamp": frame.timestamp_ns / 1e9,
                "datetime": datetime.now().isoformat(timespec="seconds"),
                "joint_source": "calibration_replay_stability_certificate",
                "joint_names": JOINT_NAMES[self.arm], "q_rad": q.tolist(),
                "image": f"left/{stem}.jpg", "robot": "h2", "arm": self.arm,
                "base_link": "torso_link", "tip_link": f"{self.arm}_wrist_yaw_link",
                "run_id": self.run_id, "corners_detected": bool(found),
                "camera_serial": self.serial,
                "orchestration": {key: body[key] for key in (
                    "capture_id", "waypoint_id", "target_q_rad", "stability") if key in body},
            }
            joints_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            self.capture_count += 1
            result = {
                "success": True, "index": index, "count": self.capture_count,
                "run_id": self.run_id, "arm": self.arm,
                "corners_detected": bool(found), "path": str(joints_path),
                "q_rad": q.tolist(), "joint_names": JOINT_NAMES[self.arm],
            }
            if capture_id:
                result["capture_id"] = capture_id
                self.capture_ids[capture_id] = result
            return result

    def jpeg(self) -> bytes:
        frame = self._frame()
        ok, encoded = cv2.imencode(".jpg", frame.color, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            raise RuntimeError("JPEG编码失败")
        return encoded.tobytes()

    def status(self) -> dict[str, Any]:
        return {
            "success": True, "run_id": self.run_id,
            "save_path": None if self.save_path is None else str(self.save_path),
            "count": self.capture_count, "arm": self.arm,
            "arm_selectable": True, "board_size": f"{self.board_size[0]}x{self.board_size[1]}",
            "camera": self.camera_info(), "recording": {"enabled": True, "arm_selectable": True},
        }

    def solve(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.solve_lock:
            if self.solve_job["running"]:
                raise RuntimeError(f"已有求解在运行: {self.solve_job['session']}")
        data_dir = Path(str(body.get("session") or "")).expanduser().resolve()
        intrinsics = data_dir / "camera_intrinsics.json"
        if not data_dir.is_dir() or not (data_dir / "joints").is_dir():
            raise ValueError(f"无效的采集目录 {data_dir}")
        if not intrinsics.is_file():
            raise ValueError(f"缺少内参文件 {intrinsics}")
        if self.urdf_path is None or not self.urdf_path.is_file():
            raise ValueError(f"机器人URDF不存在: {self.urdf_path}")
        square = float(body.get("square_size_mm") or 0)
        if not 0.1 < square < 1000:
            raise ValueError("square_size_mm 无效")
        method = str(body.get("method") or "park")
        if method not in ("tsai", "park", "horaud", "andreff", "daniilidis"):
            raise ValueError(f"未知求解方法 {method}")
        meta = json.loads((data_dir / "session_meta.json").read_text())
        tip_link = str(meta.get("tip_link") or f"{meta.get('arm', 'right')}_wrist_yaw_link")
        cmd = [
            sys.executable, "-u", "-m", "calib_workstation.solve_handeye",
            "--data", str(data_dir), "--intrinsics", str(intrinsics),
            "--urdf", str(self.urdf_path), "--board-size", str(body.get("board_size") or "11x8"),
            "--square-size", str(square), "--base-link", "torso_link",
            "--tip-link", tip_link, "--method", method, "--eye", "left",
        ]
        with self.solve_lock:
            self.solve_job.update({
                "running": True, "session": str(body.get("session")),
                "session_dir": str(data_dir), "log": ["$ " + " ".join(cmd)],
                "result": None, "error": "",
            })
        threading.Thread(target=self._run_solver, args=(cmd, data_dir), daemon=True).start()
        return {"success": True, "session": str(body.get("session"))}

    def solve_status(self) -> dict[str, Any]:
        with self.solve_lock:
            return {**self.solve_job, "log": list(self.solve_job["log"]),
                    "urdf": str(self.urdf_path), "base_link": "torso_link"}

    def _run_solver(self, cmd: list[str], data_dir: Path):
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            assert process.stdout is not None
            for line in process.stdout:
                with self.solve_lock:
                    self.solve_job["log"].append(line.rstrip())
            code = process.wait()
            result_path = data_dir / "handeye_result_left.json"
            result = json.loads(result_path.read_text()) if result_path.is_file() else None
            with self.solve_lock:
                self.solve_job["result"] = ({"left": result} if result else None)
                if code != 0 and result is None:
                    self.solve_job["error"] = f"求解进程退出码 {code}"
        except Exception as exc:
            with self.solve_lock:
                self.solve_job["error"] = str(exc)
        finally:
            with self.solve_lock:
                self.solve_job["running"] = False

    def _frame(self):
        if not self.role or not self.serial:
            raise RuntimeError("尚未选择相机")
        return self.manager.wait_frame(self.role, consumer=self.consumer, timeout_s=3.0)

    def _write_session_meta(self):
        assert self.save_path is not None
        profile = next(item.profile for item in self.manager.states()
                       if item.serial == self.serial)
        color = dict(profile.get("color") or {})
        meta = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id, "board_size": f"{self.board_size[0]}x{self.board_size[1]}",
            "joint_source": "calibration_replay_stability_certificate",
            "joint_names": JOINT_NAMES[self.arm], "robot": "h2", "arm": self.arm,
            "base_link": "torso_link", "tip_link": f"{self.arm}_wrist_yaw_link",
            "camera": self.camera_info(),
        }
        (self.save_path / "session_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        intrinsics = {
            "source": "orbbec_sdk", "serial": self.serial,
            "width": color.get("width"), "height": color.get("height"),
            "camera_matrix": color.get("camera_matrix"),
            "distortion": color.get("distortion"),
            "distortion_model": color.get("distortion_model"),
        }
        (self.save_path / "camera_intrinsics.json").write_text(
            json.dumps(intrinsics, indent=2, ensure_ascii=False) + "\n")


def detect_corners(image: np.ndarray, board_size: tuple[int, int]):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    flags = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    return cv2.findChessboardCornersSB(gray, board_size, flags=flags)
