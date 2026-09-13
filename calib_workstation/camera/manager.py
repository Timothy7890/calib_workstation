"""Thread-safe, single-owner camera fan-out for the unified workstation.

Only a :class:`CameraSource` talks to the device SDK. 2D and 3D engines acquire
named consumer leases and read the same immutable frame object. Keeping device
ownership here prevents two SDK pipelines from opening one physical camera.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol


@dataclass(frozen=True)
class CameraFrame:
    """One synchronized capture from a physical camera."""

    serial: str
    sequence: int
    timestamp_ns: int
    color: Any
    depth: Any | None = None
    depth_scale_mm: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def has_depth(self) -> bool:
        return self.depth is not None and self.depth_scale_mm is not None


class CameraSource(Protocol):
    """Blocking camera driver used by ``CameraManager`` worker threads."""

    serial: str

    def start(self) -> Mapping[str, Any]: ...

    def read(self, timeout_s: float) -> Mapping[str, Any] | None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class CameraState:
    serial: str
    running: bool
    consumers: tuple[str, ...]
    roles: tuple[str, ...]
    sequence: int
    profile: Mapping[str, Any]
    error: str | None


class _CameraRuntime:
    def __init__(self, serial: str, source: CameraSource):
        self.serial = serial
        self.source = source
        self.profile: Mapping[str, Any] = MappingProxyType({})
        self.consumers: set[str] = set()
        self.condition = threading.Condition()
        self.frame: CameraFrame | None = None
        self.sequence = -1
        self.error: str | None = None
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        profile = dict(self.source.start())
        actual = str(profile.get("serial") or self.source.serial or "").strip()
        if actual != self.serial:
            try:
                self.source.stop()
            finally:
                raise RuntimeError(
                    f"相机驱动返回序列号 {actual!r}，请求的是 {self.serial!r}")
        self.profile = MappingProxyType(profile)
        self.thread = threading.Thread(
            target=self._run, name=f"camera-{self.serial}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                raw = self.source.read(timeout_s=0.5)
                if raw is None:
                    continue
                if self.stopping.is_set():
                    break
                color = raw.get("color")
                if color is None:
                    raise RuntimeError("相机帧缺少 color")
                timestamp_ns = int(raw.get("timestamp_ns") or time.time_ns())
                depth = raw.get("depth")
                depth_scale = raw.get("depth_scale_mm")
                if depth is not None and depth_scale is None:
                    raise RuntimeError("RGB-D 帧有 depth 但缺少 depth_scale_mm")
                metadata = MappingProxyType(dict(raw.get("metadata") or {}))
                with self.condition:
                    self.sequence += 1
                    self.frame = CameraFrame(
                        serial=self.serial,
                        sequence=self.sequence,
                        timestamp_ns=timestamp_ns,
                        color=color,
                        depth=depth,
                        depth_scale_mm=(None if depth_scale is None else float(depth_scale)),
                        metadata=metadata,
                    )
                    self.error = None
                    self.condition.notify_all()
            except Exception as exc:  # driver failures are reported to every consumer
                with self.condition:
                    self.error = str(exc)
                    self.condition.notify_all()
                if not self.stopping.is_set():
                    time.sleep(0.05)

    def wait_frame(
        self, *, after_sequence: int = -1, require_depth: bool = False,
        timeout_s: float = 2.0,
    ) -> CameraFrame:
        deadline = time.monotonic() + timeout_s
        with self.condition:
            while True:
                frame = self.frame
                if (
                    frame is not None
                    and frame.sequence > after_sequence
                    and (not require_depth or frame.has_depth)
                ):
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f"；最近错误: {self.error}" if self.error else ""
                    kind = "RGB-D" if require_depth else "彩色"
                    raise TimeoutError(
                        f"{timeout_s:.1f}s 内没有收到 {self.serial} 的新{kind}帧{detail}")
                self.condition.wait(remaining)

    def stop(self) -> None:
        self.stopping.set()
        try:
            self.source.stop()
        finally:
            with self.condition:
                self.condition.notify_all()
            if self.thread is not None:
                self.thread.join(timeout=3.0)


class CameraManager:
    """Own every workstation camera once and fan frames out to consumers.

    Releasing the last consumer deliberately does not close the device: the
    workstation owns it until ``close()`` so an external process cannot race in
    between 2D and 3D operations.
    """

    def __init__(
        self,
        source_factory: Callable[[str], CameraSource],
        discover: Callable[[], list[dict[str, Any]]] | None = None,
    ):
        self._source_factory = source_factory
        self._discover = discover or (lambda: [])
        self._lock = threading.RLock()
        self._cameras: dict[str, _CameraRuntime] = {}
        self._roles: dict[str, str] = {}
        self._closed = False

    def devices(self) -> list[dict[str, Any]]:
        discovered = [dict(item) for item in self._discover()]
        with self._lock:
            running = set(self._cameras)
            roles = {serial: role for role, serial in self._roles.items()}
        for item in discovered:
            serial = str(item.get("serial") or "")
            item["owned"] = serial in running
            item["role"] = roles.get(serial)
            if item["owned"]:
                item["busy"] = False
        return discovered

    def bind_role(self, role: str, serial: str) -> None:
        role, serial = str(role).strip(), str(serial).strip()
        if not role or not serial:
            raise ValueError("相机位置和序列号不能为空")
        with self._lock:
            current = self._roles.get(role)
            if current and current != serial and current in self._cameras:
                raise RuntimeError(f"{role} 相机 {current} 已打开，运行中不能切换")
            other = next((key for key, value in self._roles.items()
                          if value == serial and key != role), None)
            if other:
                raise ValueError(f"相机 {serial} 已绑定到 {other}，不能同时冒充 {role}")
            self._roles[role] = serial

    def acquire(self, role: str, consumer: str) -> CameraState:
        consumer = str(consumer).strip()
        if not consumer:
            raise ValueError("consumer 不能为空")
        with self._lock:
            if self._closed:
                raise RuntimeError("CameraManager 已关闭")
            serial = self._roles.get(role)
            if not serial:
                raise KeyError(f"相机位置 {role!r} 尚未绑定序列号")
            runtime = self._cameras.get(serial)
            if runtime is None:
                runtime = _CameraRuntime(serial, self._source_factory(serial))
                runtime.start()
                self._cameras[serial] = runtime
            runtime.consumers.add(consumer)
            return self._state(runtime)

    def release(self, role: str, consumer: str) -> CameraState:
        with self._lock:
            runtime = self._runtime_for_role(role)
            runtime.consumers.discard(str(consumer).strip())
            return self._state(runtime)

    def wait_frame(
        self, role: str, *, consumer: str, after_sequence: int = -1,
        require_depth: bool = False, timeout_s: float = 2.0,
    ) -> CameraFrame:
        with self._lock:
            runtime = self._runtime_for_role(role)
            if consumer not in runtime.consumers:
                raise PermissionError(f"{consumer} 尚未 acquire {role} 相机")
        return runtime.wait_frame(
            after_sequence=after_sequence,
            require_depth=require_depth,
            timeout_s=timeout_s,
        )

    def states(self) -> list[CameraState]:
        with self._lock:
            return [self._state(runtime) for runtime in self._cameras.values()]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = list(self._cameras.values())
        for runtime in runtimes:
            runtime.stop()

    def _runtime_for_role(self, role: str) -> _CameraRuntime:
        serial = self._roles.get(role)
        runtime = self._cameras.get(serial or "")
        if runtime is None:
            raise RuntimeError(f"相机位置 {role!r} 尚未 acquire")
        return runtime

    def _state(self, runtime: _CameraRuntime) -> CameraState:
        roles = tuple(sorted(role for role, serial in self._roles.items()
                             if serial == runtime.serial))
        return CameraState(
            serial=runtime.serial,
            running=not runtime.stopping.is_set(),
            consumers=tuple(sorted(runtime.consumers)),
            roles=roles,
            sequence=runtime.sequence,
            profile=runtime.profile,
            error=runtime.error,
        )

    def __enter__(self) -> "CameraManager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
