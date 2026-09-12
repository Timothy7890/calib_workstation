from calib_workstation.contract import (
    SCHEMA_V2,
    canonical_subject,
    normalize_manifest,
    stable_artifact_id,
    subject_key,
)
from calib_workstation.manifest import ArtifactStore


def test_v1_camera_manifest_normalizes_without_rewrite():
    old = {
        "schema": "calib-manifest/1",
        "unit_code": "H2-1336",
        "type": "extrinsic",
        "camera_role": "head",
        "camera_serial": "CAM-1",
        "arm": "right_arm",
        "run_id": "run-1",
        "status": "active",
        "files": [],
    }

    normalized = normalize_manifest(old)

    assert old["schema"] == "calib-manifest/1"
    assert normalized["schema"] == SCHEMA_V2
    assert normalized["subject"] == {
        "kind": "camera",
        "unit_code": "H2-1336",
        "camera_role": "head",
        "camera_serial": "CAM-1",
    }
    assert normalized["subject_key"] == "head"
    assert normalized["artifact_id"]


def test_hand_subject_has_independent_partition_and_stable_id():
    subject = canonical_subject(
        "hand_mount",
        unit_code="H2-1336",
        arm="right_arm",
        hand_id="qiangnao-1-right",
        hand_serial="HAND-7",
    )

    assert subject_key("hand_mount", subject) == "right_arm__qiangnao-1-right"
    first = stable_artifact_id("H2-1336", "hand_mount", subject, "mount-1")
    second = stable_artifact_id("H2-1336", "hand_mount", subject, "mount-1")
    assert first == second


def test_tcp_profile_can_depend_on_hand_mount():
    manifest = normalize_manifest({
        "schema": SCHEMA_V2,
        "unit_code": "H2-1336",
        "type": "tcp_profile",
        "arm": "right_arm",
        "hand_id": "qiangnao-1-right",
        "run_id": "tcp-1",
        "status": "draft",
        "files": [],
        "dependencies": [{
            "relation": "derived_from",
            "artifact_id": "mount-artifact-id",
        }],
    })

    assert manifest["subject"]["kind"] == "hand"
    assert manifest["dependencies"][0]["artifact_id"] == "mount-artifact-id"


def test_2d_archive_emits_v2_pair_with_dependency(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "handeye_result_left.json").write_text(
        '{"base_link":"torso_link","tip_link":"right_wrist_yaw_link",'
        '"T_cam2base":[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}',
        encoding="utf-8",
    )
    (run_dir / "camera_intrinsics.json").write_text(
        '{"serial":"CAM-1","width":1920,"height":1080,"source":"orbbec_sdk"}',
        encoding="utf-8",
    )
    store = ArtifactStore(
        tmp_path / "calibrations", unit_code="H2-1336", vendor="BWT", model="H2")

    artifacts = store.finalize_2d_run(
        run_dir=run_dir,
        camera_role="head",
        run_id="run-1",
        arm="right_arm",
    )

    extrinsic = artifacts["extrinsic"]
    intrinsic = artifacts["intrinsic"]
    assert extrinsic["schema"] == SCHEMA_V2
    assert extrinsic["dependencies"] == [{
        "relation": "solved_with",
        "artifact_id": intrinsic["artifact_id"],
        "type": "intrinsic",
    }]
    assert extrinsic["compatibility"]["camera_serial"] == "CAM-1"
