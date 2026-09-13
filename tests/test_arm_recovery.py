import json

import pytest
from fastapi.testclient import TestClient

import calib_workstation.app as module
from calib_workstation.config import load_config


@pytest.fixture
def recovery_client(tmp_path, monkeypatch):
    state = {"state": "completed", "arm": {"engaged": True}}
    calls = []
    class FakeClient:
        def __init__(self, *args):
            pass
        def get(self, path, **kwargs):
            assert path == "/api/status"
            return state
        def post(self, path, *args, **kwargs):
            calls.append(path)
            return {"ok": True}
    monkeypatch.setattr(module, "HttpClient", FakeClient)
    config = load_config(mock=True)
    config.data_root = tmp_path
    config.state_path.write_text(json.dumps({"unit_code": "H2-TEST"}))
    job_path = tmp_path / "H2-TEST/state/current_job.json"
    job_path.parent.mkdir(parents=True)
    run = tmp_path / "capture"
    (run / "episode_0000").mkdir(parents=True)
    (run / "episode_0000/data.json").write_text("{}")
    job_path.write_text(json.dumps({"calibration_kind": "3d", "step": "running",
        "plan_id": "test", "run_dir": str(run), "arm": "left"}))
    async def switch(body):
        return {"ok": True}
    monkeypatch.setattr(module.calib3d_app, "api_offline_switch_task", switch)
    with TestClient(module.create_app(config)) as client:
        calls.clear()  # Startup registers the robot with the mocked capability service.
        yield client, state, calls, job_path


@pytest.mark.parametrize("state_name", ["completed", "stopped", "fault", "armed"])
@pytest.mark.parametrize("action", ["guide", "catch", "disarm"])
def test_recovery_controls_available_after_run(recovery_client, state_name, action):
    client, state, calls, _ = recovery_client
    state["state"] = state_name
    response = client.post(f"/api/calibration/{action}")
    assert response.status_code == 200, response.text
    assert calls == [f"/api/control/{action}"]


@pytest.mark.parametrize("state_name", ["preflight", "moving", "settling", "capturing", "returning", "paused", "unknown"])
@pytest.mark.parametrize("action", ["guide", "catch", "disarm"])
def test_cannot_change_control_during_run(recovery_client, state_name, action):
    client, state, calls, _ = recovery_client
    state["state"] = state_name
    assert client.post(f"/api/calibration/{action}").status_code == 409
    assert not calls


def test_enter_annotation_does_not_implicitly_release_arm(recovery_client):
    client, state, calls, job_path = recovery_client
    response = client.post("/api/calibration/mark-captured")
    assert response.status_code == 200, response.text
    assert response.json()["job"]["step"] == "annotating"
    assert "/api/control/disarm" not in calls
    # Ending control after capture must not reset or discard annotation state.
    assert client.post("/api/calibration/disarm").status_code == 200
    assert json.loads(job_path.read_text())["step"] == "annotating"


def test_not_engaged_cannot_drag(recovery_client):
    client, state, calls, _ = recovery_client
    state["arm"]["engaged"] = False
    assert client.post("/api/calibration/guide").status_code == 409
    assert not calls
