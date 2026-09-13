from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

UNIT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_unit_code(value: str) -> str:
    code = str(value or "").strip()
    if not UNIT_CODE_RE.match(code):
        raise ValueError(f"机器人编号只能由字母/数字/._- 组成（如 H2-1336），收到 {code!r}")
    return code
CAMERA_ROLES = ("head", "waist")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "workstation.yaml"


@dataclass
class CameraRole:
    id: str
    label: str
    target: str


@dataclass
class Config:
    vendor: str
    model: str
    hand_eye_2d_url: str
    replay_url: str
    capability_url: str
    rgbd_calibration_path: Path
    robot_urdf_path: Path
    data_root: Path
    cameras: dict[str, CameraRole]
    board_size: str = "11x8"
    square_size_mm: float = 20.0
    mock: bool = False
    path: Path | None = None

    @property
    def state_path(self) -> Path:
        """当前选中的机器人编号等运行时状态（不放配置文件，页面里设置）。"""
        return self.data_root / "workstation_state.json"

    def unit_root(self, unit_code: str) -> Path:
        """<data_root>/<unit_code>/ —— 机器人编号是产物目录的第一层。"""
        return self.data_root / unit_code

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "robot": {"vendor": self.vendor, "model": self.model},
            "services": {
                "hand_eye_2d": self.hand_eye_2d_url,
                "replay": self.replay_url,
                "capability": self.capability_url,
            },
            "data_root": str(self.data_root),
            "cameras": {
                role.id: {"label": role.label, "target": role.target}
                for role in self.cameras.values()
            },
            "board": {"size": self.board_size, "square_size_mm": self.square_size_mm},
            "rgbd_calibration_path": str(self.rgbd_calibration_path),
            "robot_urdf_path": str(self.robot_urdf_path),
            "mock": self.mock,
        }


def load_config(path: str | Path | None = None, *, mock: bool = False) -> Config:
    config_path = Path(path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    robot = raw.get("robot") or {}
    if robot.get("unit_code"):
        raise ValueError("robot.unit_code 不再写在配置文件里：机器人编号在页面首次进入时输入（保存在 data_root/workstation_state.json）")

    services = raw.get("services") or {}
    cameras: dict[str, CameraRole] = {}
    for role_id, spec in (raw.get("cameras") or {}).items():
        if role_id not in CAMERA_ROLES:
            raise ValueError(f"cameras.{role_id}: 只支持 {CAMERA_ROLES}")
        spec = spec or {}
        if spec.get("serial"):
            raise ValueError(
                f"cameras.{role_id}.serial 不再写在配置文件里：相机序列号在页面首次标定时选择并记住"
                "（保存在 <data_root>/<编号>/state/cameras.json），可随时重新选择")
        cameras[role_id] = CameraRole(
            id=role_id,
            label=str(spec.get("label") or role_id),
            target=str(spec.get("target") or f"hand_eye_2D_{role_id}"),
        )
    if not cameras:
        raise ValueError("cameras 至少要配置 head 或 waist 一个")

    board = raw.get("board") or {}
    camera = raw.get("camera") or {}
    return Config(
        vendor=str(robot.get("vendor") or "unitree"),
        model=str(robot.get("model") or "h2"),
        hand_eye_2d_url=str(services.get("hand_eye_2d") or "http://127.0.0.1:18005").rstrip("/"),
        replay_url=str(services.get("replay") or "http://127.0.0.1:18004").rstrip("/"),
        capability_url=str(services.get("capability") or "http://127.0.0.1:18000").rstrip("/"),
        rgbd_calibration_path=Path(
            camera.get("rgbd_calibration")
            or "/home/robot/yx/project/IK_replay/config/camera/orbbec_rgbd_calibration.json"
        ).expanduser().resolve(),
        robot_urdf_path=Path(
            robot.get("urdf")
            or "/home/robot/yx/project/IK_replay/assets/robots/h2/robot.urdf"
        ).expanduser().resolve(),
        data_root=Path(raw.get("data_root") or "./calib_workstation_data").expanduser().resolve(),
        cameras=cameras,
        board_size=str(board.get("size") or "11x8"),
        square_size_mm=float(board.get("square_size_mm") or 20.0),
        mock=mock,
        path=config_path,
    )
