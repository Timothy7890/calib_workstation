"""18089 灵巧手保持零位：手安装标定采样期间周期性下发全零位置。

手安装标定要求灵巧手 6 关节零位（模型点按零位 FK 提供），但手指电机不加
持时可被外力扳动。开启保持后，后端周期向 18089 hand_web 服务发送全零
positions（0 = 张开 = URDF 零位，见 hand_web poses.json），让手主动回到并
停在零位。18089 把所有 HTTP 指令统一视作 manual 源，与其视觉控制等占用
互斥：被占用时它返回 409，本模块只透传错误、绝不抢占。
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

HOLD_INTERVAL_S = 0.3
HOLD_DURATION_MS = 300
HOLD_POSITIONS = (0.0,) * 6
HOLD_SIDES = ("left", "right")
# hands.yaml 的 vendor → 18089 设备目录 id
VENDOR_DEVICE_IDS = {"brainco": "brainco_revo2", "inspire": "inspire_dfx"}


class HandHoldError(RuntimeError):
    """18089 交互失败（不可达、被其他控制源占用、参数被拒）。"""


def _request_json(
    url: str, payload: dict | None = None, timeout: float = 3.0
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    context = (
        ssl._create_unverified_context()  # 18089 使用本机自签名证书
        if url.lower().startswith("https://")
        else None
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=context
        ) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = str(detail.get("error") or f"HTTP {exc.code}")
        except (ValueError, OSError):
            message = f"HTTP {exc.code}"
        raise HandHoldError(f"18089: {message}") from exc
    except OSError as exc:
        raise HandHoldError(f"18089 不可达: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise HandHoldError(f"18089 返回非 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HandHoldError("18089 返回格式不是 JSON object")
    if body.get("ok") is False:
        raise HandHoldError(f"18089: {body.get('error') or '未知错误'}")
    return body


class HandHoldController:
    """单实例保持线程：start 幂等（同设备同侧），stop 后线程退出。

    强脑默认使用Modbus，左126/右127；本工作站配置可覆盖连接参数。
    不抢占或断开其他连接，确认设备、侧别、地址及在线反馈后才发送零位。
    """

    def __init__(self, url_getter: Callable[[], str], connections_getter: Callable[[], dict] = lambda: {}) -> None:
        self._url_getter = url_getter
        self._connections_getter = connections_getter
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state: dict[str, Any] = {"running": False}

    def _url(self, path: str) -> str:
        return str(self._url_getter()).rstrip("/") + path

    def _connection_request(self, device_id: str, side: str) -> dict:
        config = self._connections_getter().get(device_id, {})
        if not isinstance(config, dict) or not isinstance(config.get(side, {}), dict):
            raise ValueError(f"hand_connections.{device_id} 及侧别配置必须是映射")
        transport = config.get("transport") or ("modbus" if device_id == "brainco_revo2" else None)
        if transport is None:
            defaults = _request_json(self._url("/api/devices")).get("defaults", {})
            transport = defaults.get(device_id, {}).get("default_transport")
        if transport not in ("modbus", "dds"):
            raise ValueError(f"未支持的灵巧手通信方式: {transport}")
        options = dict(config.get(side, {}))
        if transport == "modbus":
            options["side"] = side
            if device_id == "brainco_revo2":
                options.setdefault("slave_id", 126 if side == "left" else 127)
                address = options["slave_id"]
                if isinstance(address, bool) or not isinstance(address, int) or not 1 <= address <= 247:
                    raise ValueError("强脑 slave_id 必须是1–247之间的整数")
        else:
            options["sides"] = side
        return {"device_id": device_id, "transport": transport, "options": options}

    def _validate_connection(self, status: dict, request: dict, side: str) -> None:
        if status.get("control_owner") is not None:
            raise HandHoldError("18089 正被其他控制源占用，请先停止该控制源")
        options = request["options"]
        hand = (status.get("hands") or {}).get(side) or {}
        matching = (status.get("connected") and status.get("device_id") == request["device_id"]
                    and status.get("transport") == request["transport"] and bool(hand))
        if request["transport"] == "modbus":
            matching = matching and ("slave_id" not in options or status.get("slave_id") == options["slave_id"])
            # A configured path may be a stable /dev/serial/by-id alias.
            if options.get("port"):
                matching = matching and Path(str(status.get("port") or "")).resolve() == Path(options["port"]).resolve()
        if not matching:
            raise HandHoldError(
                f"18089连接与目标 {request['device_id']}/{side} "
                f"({request['transport']}, ID={options.get('slave_id', '—')}) 不一致；"
                "请先在18089断开旧连接，再从18005重试，不会自动抢占或切换设备"
            )
        if not hand.get("online") or status.get("error"):
            raise HandHoldError(f"18089目标手无有效在线反馈: {status.get('error') or side}")

    def start(self, device_id: str, side: str) -> dict[str, Any]:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id 必须是非空字符串")
        if side not in HOLD_SIDES:
            raise ValueError(f"side 必须是 left/right，收到 {side!r}")
        device_id = device_id.strip()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if (
                    self._state.get("device_id") == device_id
                    and self._state.get("side") == side
                ):
                    return self._snapshot_locked()
                raise HandHoldError(
                    f"正在保持 {self._state.get('device_id')}/{self._state.get('side')}，"
                    "请先停止再切换设备或侧"
                )
            connection = self._connection_request(device_id, side)
            current = _request_json(self._url("/api/status"))
            if current.get("control_owner") is not None:
                raise HandHoldError("18089 正被其他控制源占用，请先停止该控制源")
            if not current.get("connected"):
                _request_json(self._url("/api/connect"), connection)
                current = _request_json(self._url("/api/status"))
            self._validate_connection(current, connection, side)
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._state = {
                "running": True,
                "device_id": device_id,
                "side": side,
                "connection": connection,
                "started_at": time.time(),
                "sent_count": 0,
                "error_count": 0,
                "last_error": None,
                "last_ok_at": None,
                "interval_s": HOLD_INTERVAL_S,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(side, stop_event, connection),
                name="hand-hold-18089",
                daemon=True,
            )
            self._thread.start()
            return self._snapshot_locked()

    def _run(self, side: str, stop_event: threading.Event, connection: dict) -> None:
        payload = {
            "side": side,
            "positions": list(HOLD_POSITIONS),
            "duration_ms": HOLD_DURATION_MS,
            "continuous": True,
        }
        while not stop_event.is_set():
            try:
                self._validate_connection(_request_json(self._url("/api/status")), connection, side)
                if stop_event.is_set():
                    return
                _request_json(self._url("/api/command"), payload)
                with self._lock:
                    if not stop_event.is_set():
                        self._state["sent_count"] += 1
                        self._state["last_ok_at"] = time.time()
                        self._state["last_error"] = None
            except HandHoldError as exc:
                with self._lock:
                    if not stop_event.is_set():
                        self._state["error_count"] += 1
                        self._state["last_error"] = str(exc)
                        self._state["running"] = False
                stop_event.set()
                return
            stop_event.wait(HOLD_INTERVAL_S)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            self._thread = None
            self._state = {**self._state, "running": False}
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            return self._snapshot_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        state = dict(self._state)
        state["running"] = bool(
            state.get("running") and self._thread is not None and self._thread.is_alive()
        )
        return state
