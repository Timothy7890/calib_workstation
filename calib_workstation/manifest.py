"""标定产物包：<calibrations_root>/<type>/<camera_role>/<run_id>/manifest.json + 文件。

与云端平台（Camera-Tools-for-Robot）的三个栏目一一对应：
  extrinsic        外参：T_cam2base（camera → torso_link），来自 8131 handeye_result_left.json
  intrinsic        内参：Orbbec SDK 读出的 camera_intrinsics.json
  camera_transform 内部相机转换（RGB-D depth→color），2D 流程不产出
manifest.json 是机器人侧与云端的唯一契约；字段只加不改。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import (
    CAMERA_ARTIFACT_TYPES,
    MANIFEST_SCHEMA,
    canonical_subject,
    normalize_manifest,
    stable_artifact_id,
    subject_key,
)

# The current workstation UI manages camera artifacts.  Hand artifacts use the
# same v2 contract and are enabled by the 3D integration step.
ARTIFACT_TYPES = CAMERA_ARTIFACT_TYPES


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_describe(project: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ArtifactStore:
    def __init__(self, calibrations_root: Path, *, unit_code: str, vendor: str, model: str,
                 tool_projects: dict[str, Path] | None = None):
        self.root = calibrations_root
        self.unit_code = unit_code
        self.vendor = vendor
        self.model = model
        self.tool_projects = tool_projects or {}

    # ---------- 路径 ----------

    def artifact_dir(self, artifact_type: str, camera_role: str, run_id: str) -> Path:
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"unknown artifact type {artifact_type!r}")
        return self.root / artifact_type / camera_role / run_id

    def active_pointer(self, artifact_type: str, camera_role: str) -> Path:
        return self.root / artifact_type / camera_role / "active.json"

    # ---------- 写 ----------

    def _write_artifact(
        self,
        *,
        artifact_type: str,
        camera_role: str,
        run_id: str,
        files: list[Path],
        arm: str,
        camera_serial: str | None,
        tool: str,
        source_run_dir: Path,
        quality: dict[str, Any],
        extra: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        target = self.artifact_dir(artifact_type, camera_role, run_id)
        if target.exists() and not overwrite:
            raise FileExistsError(f"产物已存在: {target}")
        target.mkdir(parents=True, exist_ok=True)
        file_entries = []
        for src in files:
            if not src.is_file():
                raise FileNotFoundError(f"缺少产物文件: {src}")
            dst = target / src.name
            shutil.copy2(src, dst)
            file_entries.append({
                "name": src.name,
                "bytes": dst.stat().st_size,
                "sha256": _sha256(dst),
                "source": str(src),
            })
        subject = canonical_subject(
            artifact_type,
            unit_code=self.unit_code,
            camera_role=camera_role,
            camera_serial=camera_serial,
            arm=arm,
        )
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "artifact_id": stable_artifact_id(
                self.unit_code, artifact_type, subject, run_id),
            "unit_code": self.unit_code,
            "vendor": self.vendor,
            "robot_model": self.model,
            "type": artifact_type,
            "camera_role": camera_role,
            "camera_serial": camera_serial,
            "arm": arm,
            "subject": subject,
            "subject_key": subject_key(artifact_type, subject),
            "run_id": run_id,
            "tool": tool,
            "tool_version": _git_describe(self.tool_projects.get(tool, Path("."))),
            "created_at": _now(),
            "source_run_dir": str(source_run_dir),
            "quality": quality,
            "dependencies": [],
            "compatibility": {},
            "files": file_entries,
            "status": "draft",
            "cloud": {"pushed": False, "pushed_at": None, "remote_id": None},
        }
        if extra:
            manifest.update(extra)
        (target / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return manifest

    def finalize_2d_run(
        self,
        *,
        run_dir: Path,
        camera_role: str,
        run_id: str,
        arm: str,
        overwrite: bool = False,
        camera_label: str | None = None,
    ) -> dict[str, Any]:
        """把一次 2D 运行目录里的求解结果打成 extrinsic + intrinsic 两个产物包。"""
        result_path = run_dir / "handeye_result_left.json"
        result = _read_json(result_path)
        if result is None:
            raise FileNotFoundError(f"{run_dir} 里没有 handeye_result_left.json，请先求解")
        intrinsics_path = run_dir / "camera_intrinsics.json"
        intrinsics = _read_json(intrinsics_path) or {}
        meta = _read_json(run_dir / "session_meta.json") or {}
        camera_serial = (
            (meta.get("camera") or {}).get("serial")
            or intrinsics.get("serial")
        )
        intrinsic_subject = canonical_subject(
            "intrinsic",
            unit_code=self.unit_code,
            camera_role=camera_role,
            camera_serial=camera_serial,
            arm=arm,
        )
        intrinsic_artifact_id = stable_artifact_id(
            self.unit_code, "intrinsic", intrinsic_subject, run_id)
        camera_compatibility = {
            "camera_serial": camera_serial,
            "width": intrinsics.get("width"),
            "height": intrinsics.get("height"),
        }
        n_samples = len(list((run_dir / "joints").glob("*.json"))) if (run_dir / "joints").is_dir() else 0

        extrinsic_quality = {
            "num_samples": int(result.get("num_samples") or n_samples),
            "num_inliers": result.get("num_inliers"),
            "residual_translation_mm": result.get("residual_translation_mm"),
            "residual_rotation_deg": result.get("residual_rotation_deg"),
            "method": result.get("method"),
            "square_size_mm": result.get("square_size_mm"),
            "board_size": result.get("board_size"),
        }
        extra_files = [p for p in (run_dir / "run.json", run_dir / "session_meta.json") if p.is_file()]
        extrinsic = self._write_artifact(
            artifact_type="extrinsic",
            camera_role=camera_role,
            run_id=run_id,
            files=[result_path, *extra_files],
            arm=arm,
            camera_serial=camera_serial,
            tool="hand_eye_2D",
            source_run_dir=run_dir,
            quality=extrinsic_quality,
            extra={
                "frames": {"parent": result.get("base_link"), "child": "camera_rgb",
                           "tip_link": result.get("tip_link")},
                "primary_file": result_path.name,
                "T_cam2base": result.get("T_cam2base"),
                "camera_label": camera_label,
                "source_method": "checkerboard_2d",
                "dependencies": ([{
                    "relation": "solved_with",
                    "artifact_id": intrinsic_artifact_id,
                    "type": "intrinsic",
                }] if intrinsics_path.is_file() else []),
                "compatibility": camera_compatibility,
            },
            overwrite=overwrite,
        )
        artifacts = {"extrinsic": extrinsic}
        if intrinsics_path.is_file():
            artifacts["intrinsic"] = self._write_artifact(
                artifact_type="intrinsic",
                camera_role=camera_role,
                run_id=run_id,
                files=[intrinsics_path],
                arm=arm,
                camera_serial=camera_serial,
                tool="hand_eye_2D",
                source_run_dir=run_dir,
                quality={
                    "width": intrinsics.get("width"),
                    "height": intrinsics.get("height"),
                    "source": intrinsics.get("source"),
                },
                extra={
                    "primary_file": intrinsics_path.name,
                    "camera_label": camera_label,
                    "source_method": str(intrinsics.get("source") or "camera_sdk"),
                    "compatibility": camera_compatibility,
                },
                overwrite=overwrite,
            )
        return artifacts

    # ---------- 生效 / 列表 ----------

    def set_active(self, artifact_type: str, camera_role: str, run_id: str) -> dict[str, Any]:
        target = self.artifact_dir(artifact_type, camera_role, run_id)
        manifest_path = target / "manifest.json"
        manifest = _read_json(manifest_path)
        if manifest is None:
            raise FileNotFoundError(f"产物不存在: {target}")
        previous = _read_json(self.active_pointer(artifact_type, camera_role)) or {}
        if previous.get("run_id") and previous["run_id"] != run_id:
            prev_manifest_path = self.artifact_dir(artifact_type, camera_role, previous["run_id"]) / "manifest.json"
            prev_manifest = _read_json(prev_manifest_path)
            if prev_manifest is not None:
                prev_manifest["status"] = "superseded"
                prev_manifest_path.write_text(
                    json.dumps(prev_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest["status"] = "active"
        manifest["activated_at"] = _now()
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        pointer = {"run_id": run_id, "activated_at": manifest["activated_at"], "path": str(target)}
        self.active_pointer(artifact_type, camera_role).write_text(
            json.dumps(pointer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return manifest

    def delete(self, artifact_type: str, camera_role: str, run_id: str) -> dict[str, Any]:
        """删除一个产物包目录。若它正是当前生效项，则清掉 active.json（该位置回到"未标定"）。

        只删 calibrations/ 下的归档副本，原始采集目录（18004 runs/）不受影响。
        """
        target = self.artifact_dir(artifact_type, camera_role, run_id)
        if not (target / "manifest.json").is_file():
            raise FileNotFoundError(f"产物不存在: {target}")
        was_active = False
        pointer_path = self.active_pointer(artifact_type, camera_role)
        pointer = _read_json(pointer_path) or {}
        if pointer.get("run_id") == run_id:
            pointer_path.unlink(missing_ok=True)
            was_active = True
        shutil.rmtree(target)
        return {"type": artifact_type, "camera_role": camera_role, "run_id": run_id, "was_active": was_active}

    def roles(self) -> list[str]:
        """已归档过的所有相机位置 id（含自定义），按目录名去重。"""
        found: set[str] = set()
        for artifact_type in ARTIFACT_TYPES:
            base = self.root / artifact_type
            if base.is_dir():
                found.update(p.name for p in base.iterdir() if p.is_dir())
        return sorted(found)

    def active(self, artifact_type: str, camera_role: str) -> dict[str, Any] | None:
        pointer = _read_json(self.active_pointer(artifact_type, camera_role))
        if not pointer or not pointer.get("run_id"):
            return None
        manifest = _read_json(
            self.artifact_dir(artifact_type, camera_role, pointer["run_id"]) / "manifest.json")
        if manifest is None:
            return None
        try:
            return normalize_manifest(manifest)
        except ValueError:
            return None

    def list(self, artifact_type: str | None = None, camera_role: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for manifest_path in self.root.glob("*/*/*/manifest.json"):
            manifest = _read_json(manifest_path)
            if manifest is None:
                continue
            try:
                manifest = normalize_manifest(manifest)
            except ValueError:
                continue
            if artifact_type and manifest.get("type") != artifact_type:
                continue
            if camera_role and manifest.get("camera_role") != camera_role:
                continue
            manifest["path"] = str(manifest_path.parent)
            items.append(manifest)
        items.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return items

    def get(self, artifact_type: str, camera_role: str, run_id: str) -> dict[str, Any] | None:
        manifest = _read_json(self.artifact_dir(artifact_type, camera_role, run_id) / "manifest.json")
        if manifest is not None:
            try:
                manifest = normalize_manifest(manifest)
            except ValueError:
                return None
            manifest["path"] = str(self.artifact_dir(artifact_type, camera_role, run_id))
        return manifest
