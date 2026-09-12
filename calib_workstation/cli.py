from __future__ import annotations

import argparse
import sys

import uvicorn

from .app import create_app
from .config import DEFAULT_CONFIG_PATH, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="标定工作站 18005")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="workstation.yaml 路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18005)
    parser.add_argument("--mock", action="store_true", help="仅标记为联调模式（下游服务自行以 mock 启动）")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, mock=args.mock)
    except (OSError, ValueError) as exc:
        print(f"[workstation] 配置错误: {exc}", file=sys.stderr)
        return 1
    config.data_root.mkdir(parents=True, exist_ok=True)
    print(f"[workstation] 产物根目录 {config.data_root}（机器人编号在页面输入，落 <data_root>/<编号>/calibrations）")
    print(f"[workstation] 2D {config.hand_eye_2d_url}  3D {config.hand_eye_3d_url}  回放 {config.replay_url}  18000 {config.capability_url}")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")
    return 0
