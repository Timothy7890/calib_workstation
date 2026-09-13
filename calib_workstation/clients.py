"""对 18004 / 18000 及迁移期服务的最小 HTTP 客户端。

所有下游错误统一抛 ServiceError（含服务名、HTTP 状态、对方返回的 error 文本），
由 app 层转成给前端的 502/409，前端只需展示 message。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ServiceError(RuntimeError):
    def __init__(self, service: str, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.service = service
        self.status = status
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "status": self.status,
            "message": str(self),
            "payload": self.payload,
        }


# 下游全是本机/局域网服务，绝不能走系统代理（终端里常有 http_proxy 环境变量，
# 会把 127.0.0.1 的请求发到代理机器上，返回空 body 的错误）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpClient:
    def __init__(self, name: str, base_url: str, timeout_s: float = 10.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                *, timeout_s: float | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = detail
            payload: Any = detail
            try:
                payload = json.loads(detail)
                if isinstance(payload, dict):
                    message = str(payload.get("error") or payload.get("detail") or detail)
            except ValueError:
                pass
            raise ServiceError(self.name, message, exc.code, payload) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError(self.name, f"{self.name} 不可达（{self.base_url}）: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except ValueError as exc:
            raise ServiceError(self.name, f"{self.name} 返回了非 JSON: {raw[:200]}") from exc

    def get(self, path: str, **kw) -> dict[str, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict[str, Any] | None = None, **kw) -> dict[str, Any]:
        return self.request("POST", path, body or {}, **kw)

    def put(self, path: str, body: dict[str, Any], **kw) -> dict[str, Any]:
        return self.request("PUT", path, body, **kw)

    def reachable(self, path: str = "/api/status") -> tuple[bool, str]:
        try:
            self.get(path, timeout_s=2.0)
            return True, ""
        except ServiceError as exc:
            return False, str(exc)
