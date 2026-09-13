import io

import pytest
import yaml
from fastapi.testclient import TestClient

from calib_workstation.app import create_app
from calib_workstation.config import load_config
from calib_workstation.calib3d import app as engine, hand_hold


@pytest.mark.parametrize("configured, expected", [
    (None, "http://127.0.0.1:18089"),
    ("http://127.0.0.1:18089/", "http://127.0.0.1:18089"),
    ("https://127.0.0.1:18089/", "https://127.0.0.1:18089"),
])
def test_hand_service_url_reaches_controller_without_hardware(tmp_path, monkeypatch, configured, expected):
    raw = {"cameras": {"head": {}}, "data_root": str(tmp_path / "data"),
           "hand_connections": {"brainco_revo2": {"left": {"slave_id": 42}}}}
    if configured is not None:
        raw["services"] = {"hand_web": configured}
    path = tmp_path / "workstation.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path, mock=True)
    assert config.hand_service_url == expected
    assert config.to_public_dict()["services"]["hand_web"] == expected

    calls = []
    def fake_urlopen(request, **kwargs):
        calls.append((request.full_url, request.get_method(), kwargs.get("context")))
        return io.BytesIO(b'{"ok":true}')
    monkeypatch.setattr(hand_hold.urllib.request, "urlopen", fake_urlopen)
    with TestClient(create_app(config)):
        assert engine.hand_service_url == expected
        assert engine.hand_hold._connection_request("brainco_revo2", "left")["options"]["slave_id"] == 42
        # Exercise URL construction and transport without connecting or commanding a hand.
        assert hand_hold._request_json(engine.hand_hold._url("/api/status"))["ok"]
    assert calls[0][:2] == (expected + "/api/status", "GET")
    assert (calls[0][2] is not None) == expected.startswith("https://")
