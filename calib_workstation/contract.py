"""Unified calibration artifact contract shared by workstation consumers.

Version 2 separates the thing being calibrated (``subject``) from the storage
location and records stable identities/dependencies.  Version 1 manifests are
still accepted and can be normalised in memory; existing archives do not need
to be rewritten.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

SCHEMA_V1 = "calib-manifest/1"
SCHEMA_V2 = "calib-manifest/2"
MANIFEST_SCHEMA = SCHEMA_V2

CAMERA_ARTIFACT_TYPES = ("extrinsic", "intrinsic", "camera_transform")
HAND_ARTIFACT_TYPES = ("hand_mount", "tcp_profile")
ARTIFACT_TYPES = (*CAMERA_ARTIFACT_TYPES, *HAND_ARTIFACT_TYPES)
STATUSES = ("draft", "active", "superseded")

_ID_NAMESPACE = uuid.UUID("9d985bb7-46ad-4bb2-9105-8e9a621d2c47")
_SAFE_COMPONENT = re.compile(r"^[^/\\\x00]+$")


class ContractError(ValueError):
    """A manifest cannot be represented by the unified contract."""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"manifest.{field} 不能为空")
    if not _SAFE_COMPONENT.match(text) or text in (".", ".."):
        raise ContractError(f"manifest.{field} 非法")
    return text


def canonical_subject(
    artifact_type: str,
    *,
    unit_code: str,
    camera_role: str | None = None,
    camera_serial: str | None = None,
    arm: str | None = None,
    hand_id: str | None = None,
    hand_serial: str | None = None,
    tool_id: str | None = None,
) -> dict[str, Any]:
    """Build the identity of the physical object described by an artifact."""
    unit = _required_text(unit_code, "unit_code")
    if artifact_type in CAMERA_ARTIFACT_TYPES:
        return {
            "kind": "camera",
            "unit_code": unit,
            "camera_role": _required_text(camera_role, "subject.camera_role"),
            "camera_serial": str(camera_serial or "").strip() or None,
        }
    if artifact_type in HAND_ARTIFACT_TYPES:
        if tool_id and artifact_type == "tcp_profile":
            return {"kind": "tool", "unit_code": unit,
                    "arm": _required_text(arm, "subject.arm"),
                    "tool_id": _required_text(tool_id, "subject.tool_id")}
        return {
            "kind": "hand",
            "unit_code": unit,
            "arm": _required_text(arm, "subject.arm"),
            "hand_id": _required_text(hand_id, "subject.hand_id"),
            "hand_serial": str(hand_serial or "").strip() or None,
        }
    raise ContractError(f"未知产物类型 {artifact_type!r}")


def subject_key(artifact_type: str, subject: dict[str, Any]) -> str:
    """Return the stable local/cloud partition key for a subject."""
    if artifact_type in CAMERA_ARTIFACT_TYPES:
        return _required_text(subject.get("camera_role"), "subject.camera_role")
    if artifact_type in HAND_ARTIFACT_TYPES:
        arm = _required_text(subject.get("arm"), "subject.arm")
        if subject.get("kind") == "tool" and artifact_type == "tcp_profile":
            return f"{arm}__tool__{_required_text(subject.get('tool_id'), 'subject.tool_id')}"
        hand_id = _required_text(subject.get("hand_id"), "subject.hand_id")
        return f"{arm}__{hand_id}"
    raise ContractError(f"未知产物类型 {artifact_type!r}")


def stable_artifact_id(unit_code: str, artifact_type: str, subject: dict[str, Any], run_id: str) -> str:
    """Create an id that is stable across upload retries and local re-archives."""
    identity = "/".join((
        _required_text(unit_code, "unit_code"),
        _required_text(artifact_type, "type"),
        subject_key(artifact_type, subject),
        _required_text(run_id, "run_id"),
    ))
    return str(uuid.uuid5(_ID_NAMESPACE, identity))


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a v2 view of a v1/v2 manifest without mutating the input."""
    out = dict(manifest)
    artifact_type = _required_text(out.get("type"), "type")
    if artifact_type not in ARTIFACT_TYPES:
        raise ContractError(f"未知产物类型 {artifact_type!r}")
    unit_code = _required_text(out.get("unit_code"), "unit_code")
    run_id = _required_text(out.get("run_id"), "run_id")
    subject = out.get("subject")
    if not isinstance(subject, dict):
        subject = canonical_subject(
            artifact_type,
            unit_code=unit_code,
            camera_role=out.get("camera_role"),
            camera_serial=out.get("camera_serial"),
            arm=out.get("arm"),
            hand_id=out.get("hand_id"),
            hand_serial=out.get("hand_serial"),
        )
    else:
        subject = canonical_subject(
            artifact_type,
            unit_code=unit_code,
            camera_role=subject.get("camera_role"),
            camera_serial=subject.get("camera_serial"),
            arm=subject.get("arm"),
            hand_id=subject.get("hand_id"),
            hand_serial=subject.get("hand_serial"),
            tool_id=subject.get("tool_id") if subject.get("kind") == "tool" else None,
        )
    out["schema"] = SCHEMA_V2
    out["subject"] = subject
    out["subject_key"] = subject_key(artifact_type, subject)
    out["artifact_id"] = str(out.get("artifact_id") or stable_artifact_id(
        unit_code, artifact_type, subject, run_id))
    out["dependencies"] = list(out.get("dependencies") or [])
    out["compatibility"] = dict(out.get("compatibility") or {})
    return out


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a manifest at a local/cloud boundary."""
    out = normalize_manifest(manifest)
    if out.get("status", "draft") not in STATUSES:
        raise ContractError(f"manifest.status 只能是 {STATUSES}")
    files = out.get("files")
    if not isinstance(files, list):
        raise ContractError("manifest.files 必须是数组")
    for entry in files:
        if not isinstance(entry, dict):
            raise ContractError("manifest.files[] 必须是对象")
        _required_text(entry.get("name"), "files[].name")
    for dependency in out["dependencies"]:
        if not isinstance(dependency, dict):
            raise ContractError("manifest.dependencies[] 必须是对象")
        _required_text(dependency.get("artifact_id"), "dependencies[].artifact_id")
        _required_text(dependency.get("relation"), "dependencies[].relation")
    return out
