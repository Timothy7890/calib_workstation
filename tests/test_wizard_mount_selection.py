"""Wizard requests must preserve the embedded solver's saved sticker selection."""
from copy import deepcopy

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import calib_workstation.app as workstation_app
from calib_workstation.calib3d import mount_api
from calib_workstation.config import load_config


EXCLUDED = [f"back-green-{i:02d}" for i in range(5, 9)] + [
    f"palm-red-{i:02d}" for i in range(5, 9)
] + ["side-yellow-01", "side-yellow-02", "side-pink-01", "side-pink-02"]
SAVED = {"excluded_point_ids": EXCLUDED, "point_count": 8, "num_samples": 52,
         "hand_id": "qiangnao-revo2-left", "arm": "left"}


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    config = load_config(mock=True)
    config.data_root = tmp_path / "data"
    workspace = workstation_app.Workspace(config)
    workspace.select("H2-TEST")
    workspace.job.update(calibration_kind="3d", step="annotated", arm="left",
                         hand_id="qiangnao-revo2-left", model_id="qiangnao-revo2-left",
                         object_mode="hand", camera_role="head", extrinsic_artifact_id="camera-1")
    monkeypatch.setattr(workstation_app, "Workspace", lambda _: workspace)
    monkeypatch.setattr(workspace.store, "active", lambda *_: {
        "artifact_id": "camera-1", "run_id": "camera-1", "primary_file": "handeye_result_left.json",
    })

    class FakeHttp:
        def __init__(self, *_):
            pass

        def get(self, path, **_):
            assert path == "/api/capability/registry"
            return {"registry": {"active": {"arm": "left_arm", "hand_id": "qiangnao-revo2-left"}}}

        def post(self, path, *_, **__):
            assert path == "/api/capability/robot"
            return {"ok": True}

    monkeypatch.setattr(workstation_app, "HttpClient", FakeHttp)
    state = {"result": deepcopy(SAVED), "stale": False, "calls": []}

    async def saved_result():
        if state.get("read_error"):
            return JSONResponse({"ok": False, "error": "安装标定结果无法读取"}, status_code=500)
        return {"result": state["result"], "stale": state["stale"]}

    async def solve(body):
        # No actual solver, camera, robot or existing calibration file is touched.
        state["calls"].append(deepcopy(body))
        return {"ok": True, "excluded_point_ids": body["exclude_point_ids"]}

    monkeypatch.setattr(mount_api, "api_mount_result", saved_result)
    monkeypatch.setattr(mount_api, "api_mount_solve", solve)
    with TestClient(workstation_app.create_app(config)) as client:
        yield client, state, workspace.job


@pytest.mark.parametrize("stale", [False, True])
def test_wizard_forwards_saved_twelve_exclusions(wizard, stale):
    client, state, job = wizard
    state["stale"] = stale  # Editing observations requires re-solving, not resetting selection.
    original = deepcopy(state["result"])
    response = client.post("/api/hand-calibration/solve", json={"camera_role": "head"})
    assert response.status_code == 200, response.text
    assert state["calls"][0]["exclude_point_ids"] == EXCLUDED
    assert state["calls"][0]["calib_path"].endswith("handeye_result_left.json")
    assert state["result"] == original
    assert job.snapshot()["step"] == "solved"
    # Read the latest saved selection on EVERY request, including an explicit all-selected result.
    state["result"]["excluded_point_ids"] = []
    assert client.post("/api/hand-calibration/solve", json={}).status_code == 200
    assert state["calls"][-1]["exclude_point_ids"] == []


@pytest.mark.parametrize("saved", [None, {}, {"excluded_point_ids": []}])
def test_first_or_legacy_solve_without_exclusions(wizard, saved):
    client, state, _ = wizard
    state["result"] = saved
    assert client.post("/api/hand-calibration/solve", json={}).status_code == 200
    assert state["calls"][0]["exclude_point_ids"] == []


@pytest.mark.parametrize("change", [
    {"excluded_point_ids": None}, {"excluded_point_ids": "red1"},
    {"excluded_point_ids": [1]}, {"arm": "right"}, {"hand_id": "yinshi-1-left"},
])
def test_invalid_or_other_object_selection_must_not_fall_back_to_all(wizard, change):
    client, state, job = wizard
    state["result"].update(change)
    response = client.post("/api/hand-calibration/solve", json={})
    assert response.status_code == 409, response.text
    assert state["calls"] == []
    assert job.snapshot()["step"] == "annotated"


def test_unreadable_saved_result_aborts_without_solving(wizard):
    client, state, job = wizard
    state["read_error"] = True
    assert client.post("/api/hand-calibration/solve", json={}).status_code == 500
    assert state["calls"] == []
    assert job.snapshot()["step"] == "annotated"
