"""把归档产物推送到云端管理平台（Camera-Tools-for-Robot 后端）。

云端地址与 token 不写配置文件：页面里填写，保存在 <data_root>/cloud.json。
推送单位是一个产物包目录（manifest.json + 文件），走云端的
POST /api/robots/units/{unit_code}/calibrations（multipart，幂等）。
成功后回写本地 manifest 的 cloud 字段：
  pushed / pushed_at / remote_id / pushed_status（推送时的 status）/ url
本地 status 之后若变了（生效 / 被替代），pushed_status != status 即视为"待同步"，再推一次即可。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest import ArtifactStore

_TIMEOUT_S = 60.0


class CloudError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mask(token: str) -> str:
    if not token:
        return ""
    return token[:4] + "…" + token[-4:] if len(token) > 12 else "…" + token[-2:]


class CloudSettings:
    """<data_root>/cloud.json: {url, token, auto_push}"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.url: str = str(saved.get("url") or "").rstrip("/")
        self.token: str = str(saved.get("token") or "")
        self.auto_push: bool = bool(saved.get("auto_push", True))
        self.updated_at: str | None = saved.get("updated_at")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def update(self, *, url: str | None = None, token: str | None = None, auto_push: bool | None = None) -> None:
        with self.lock:
            if url is not None:
                url = url.strip().rstrip("/")
                if url and not url.startswith(("http://", "https://")):
                    raise ValueError("云端地址需要以 http:// 或 https:// 开头")
                self.url = url
            if token is not None and token != "":
                self.token = token.strip()
            if auto_push is not None:
                self.auto_push = bool(auto_push)
            self.updated_at = datetime.now().isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "url": self.url, "token": self.token, "auto_push": self.auto_push, "updated_at": self.updated_at,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def public(self) -> dict[str, Any]:
        return {
            "url": self.url, "token_set": bool(self.token), "token_masked": _mask(self.token),
            "auto_push": self.auto_push, "configured": self.configured, "updated_at": self.updated_at,
            "path": str(self.path),
        }


class CloudClient:
    """云端 HTTP。云端在公网，走系统代理设置（urllib 默认行为）。"""

    def __init__(self, settings: CloudSettings):
        self.settings = settings

    def _request(self, method: str, path: str, *, body: bytes | None = None,
                 content_type: str | None = None, timeout_s: float = _TIMEOUT_S) -> dict[str, Any]:
        if not self.settings.url:
            raise CloudError("还没有设置云端地址")
        headers = {"Accept": "application/json"}
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.settings.url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = detail[:300]
            try:
                payload = json.loads(detail)
                inner = payload.get("detail", payload) if isinstance(payload, dict) else payload
                if isinstance(inner, dict):
                    message = str(inner.get("error") or inner.get("message") or detail[:300])
            except ValueError:
                pass
            if exc.code == 401:
                message = "云端拒绝：token 无效"
            raise CloudError(f"云端返回 {exc.code}: {message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"连不上云端（{self.settings.url}）: {getattr(exc, 'reason', exc)}") from exc
        try:
            return json.loads(raw) if raw else {}
        except ValueError as exc:
            raise CloudError(f"云端返回了非 JSON: {raw[:200]}") from exc

    def health(self) -> dict[str, Any]:
        out = self._request("GET", "/api/health", timeout_s=8.0)
        if not out.get("ok"):
            raise CloudError(f"云端健康检查失败: {out}")
        # 顺手验证 token：拿一个必然需要写权限的接口试探（不存在的 id → 有 token 是 404，无/错 token 是 401）
        if self.settings.token:
            try:
                self._request("DELETE", "/api/robots/units/_probe/calibrations/0", timeout_s=8.0)
            except CloudError as exc:
                if "401" in str(exc) or "token" in str(exc):
                    raise
        return out

    def upload(self, manifest: dict[str, Any], files: list[Path]) -> dict[str, Any]:
        boundary = "----calib" + uuid.uuid4().hex
        parts: list[bytes] = []
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"manifest\"\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n\r\n".encode()
            + json.dumps(manifest, ensure_ascii=False).encode("utf-8") + b"\r\n")
        for path in files:
            name = path.name.replace('"', "_")
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        unit = urllib.request.quote(str(manifest["unit_code"]), safe="")
        return self._request("POST", f"/api/robots/units/{unit}/calibrations", body=b"".join(parts),
                             content_type=f"multipart/form-data; boundary={boundary}")

    def delete(self, unit_code: str, remote_id: int) -> None:
        unit = urllib.request.quote(unit_code, safe="")
        try:
            self._request("DELETE", f"/api/robots/units/{unit}/calibrations/{int(remote_id)}", timeout_s=15.0)
        except CloudError as exc:
            if "404" in str(exc):
                return  # 云端已经没有了，视为成功
            raise


class CloudSync:
    """针对一个机器人的 ArtifactStore 做推送。"""

    def __init__(self, settings: CloudSettings):
        self.settings = settings
        self.client = CloudClient(settings)
        self.lock = threading.Lock()
        self.last: dict[str, Any] = {}  # 最近一次同步结果，页面展示

    # ---------- 状态判定 ----------

    @staticmethod
    def needs_push(manifest: dict[str, Any]) -> bool:
        cloud = manifest.get("cloud") or {}
        if not cloud.get("pushed"):
            return True
        return cloud.get("pushed_status") != manifest.get("status")

    @staticmethod
    def sync_state(manifest: dict[str, Any]) -> str:
        """synced | stale（本地状态变了）| pending（从没推过）"""
        cloud = manifest.get("cloud") or {}
        if not cloud.get("pushed"):
            return "pending"
        return "synced" if cloud.get("pushed_status") == manifest.get("status") else "stale"

    # ---------- 推送 ----------

    def push_one(self, store: ArtifactStore, artifact_type: str, camera_role: str, run_id: str) -> dict[str, Any]:
        if not self.settings.configured:
            raise CloudError("还没有配置云端地址和 token")
        target = store.artifact_dir(artifact_type, camera_role, run_id)
        manifest_path = target / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CloudError(f"读取 {manifest_path} 失败: {exc}") from exc
        files = [target / f["name"] for f in manifest.get("files") or [] if f.get("name")]
        missing = [p.name for p in files if not p.is_file()]
        if missing:
            raise CloudError(f"产物目录缺少文件 {missing}，无法推送")
        # 上传时把云端字段清成"未推送"，由云端重新写；本地保留上一次的 remote_id 以便对照
        payload = dict(manifest)
        payload.pop("path", None)
        result = self.client.upload(payload, files)
        remote_id = result.get("id")
        manifest.setdefault("cloud", {})
        manifest["cloud"].update({
            "pushed": True, "pushed_at": _now(), "remote_id": remote_id,
            "pushed_status": manifest.get("status"), "url": self.settings.url,
        })
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"type": artifact_type, "camera_role": camera_role, "run_id": run_id,
                "remote_id": remote_id, "created": result.get("created")}

    def push_pending(self, store: ArtifactStore, *, force: bool = False) -> dict[str, Any]:
        """推送该机器人所有待同步的产物。一次失败不影响其余项；结果记入 self.last。"""
        with self.lock:
            started = _now()
            pushed: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            skipped = 0
            if not self.settings.configured:
                self.last = {"at": started, "ok": False, "error": "还没有配置云端地址和 token",
                             "pushed": [], "failed": [], "skipped": 0}
                return self.last
            for manifest in store.list():
                if not force and not self.needs_push(manifest):
                    skipped += 1
                    continue
                key = {"type": manifest["type"], "camera_role": manifest["camera_role"], "run_id": manifest["run_id"]}
                try:
                    pushed.append(self.push_one(store, key["type"], key["camera_role"], key["run_id"]))
                except CloudError as exc:
                    failed.append({**key, "error": str(exc)})
            self.last = {"at": started, "ok": not failed, "pushed": pushed, "failed": failed, "skipped": skipped,
                         "error": failed[0]["error"] if failed else None}
            return self.last

    def push_pending_async(self, store: ArtifactStore) -> None:
        threading.Thread(target=self.push_pending, args=(store,), daemon=True, name="cloud-push").start()

    def delete_remote(self, unit_code: str, manifest: dict[str, Any] | None) -> str | None:
        """本地删除产物后同步删云端。返回错误文本（None = 成功或无需删除）。"""
        cloud = (manifest or {}).get("cloud") or {}
        if not cloud.get("remote_id") or not self.settings.configured:
            return None
        try:
            self.client.delete(unit_code, int(cloud["remote_id"]))
            return None
        except (CloudError, ValueError) as exc:
            return str(exc)

    def summary(self, store: ArtifactStore | None) -> dict[str, Any]:
        counts = {"synced": 0, "stale": 0, "pending": 0}
        if store is not None:
            for manifest in store.list():
                counts[self.sync_state(manifest)] += 1
        return {"settings": self.settings.public(), "counts": counts, "last": self.last}
