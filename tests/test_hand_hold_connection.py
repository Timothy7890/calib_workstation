import threading

import pytest

from calib_workstation.calib3d import hand_hold


def connected(side="left", address=126, **overrides):
    return {"connected": True, "device_id": "brainco_revo2", "transport": "modbus",
            "slave_id": address, "hands": {side: {"online": True}}, "control_owner": None,
            "port": "/dev/ttyUSB1", **overrides}


class NoMotionThread:
    def __init__(self, **kwargs):
        self.alive = False
    def start(self):
        self.alive = True
    def is_alive(self):
        return self.alive
    def join(self, **kwargs):
        self.alive = False


def install_fake(monkeypatch, states):
    calls = []
    def request(url, payload=None, **kwargs):
        calls.append((url.rsplit("/", 1)[-1], payload))
        if url.endswith("/status"):
            return states.pop(0)
        if url.endswith("/connect"):
            return {"ok": True}
        raise AssertionError(f"Unexpected request: {url}")
    monkeypatch.setattr(hand_hold, "_request_json", request)
    monkeypatch.setattr(hand_hold.threading, "Thread", NoMotionThread)
    return calls


@pytest.mark.parametrize("side,address", [("left", 126), ("right", 127)])
def test_automatic_connection_has_correct_side_and_address(monkeypatch, side, address):
    calls = install_fake(monkeypatch, [{"connected": False}, connected(side, address)])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089")
    result = hold.start("brainco_revo2", side)
    assert result["running"]
    assert calls == [("status", None), ("connect", {
        "device_id": "brainco_revo2", "transport": "modbus",
        "options": {"side": side, "slave_id": address}}), ("status", None)]
    hold.stop()


def test_matching_manual_connection_is_reused_without_reconnecting(monkeypatch):
    calls = install_fake(monkeypatch, [connected()])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089")
    assert hold.start("brainco_revo2", "left")["running"]
    assert calls == [("status", None)]
    hold.stop()


@pytest.mark.parametrize("status", [
    connected("right", 127), connected(address=127),
    connected(device_id="inspire_dfx"), connected(transport="dds"),
    connected(control_owner="vision"), connected(error="read failed"),
    connected(hands={"left": {"online": False}}),
])
def test_wrong_or_busy_connection_never_sends_or_disconnects(monkeypatch, status):
    calls = install_fake(monkeypatch, [status])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089")
    with pytest.raises(hand_hold.HandHoldError):
        hold.start("brainco_revo2", "left")
    assert calls == [("status", None)]
    assert not hold.status()["running"]


def test_recheck_after_connect_catches_foreign_service_wrong_reuse(monkeypatch):
    calls = install_fake(monkeypatch, [{"connected": False}, connected("right", 127)])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089")
    with pytest.raises(hand_hold.HandHoldError, match="不一致"):
        hold.start("brainco_revo2", "left")
    assert [name for name, _ in calls] == ["status", "connect", "status"]


def test_custom_address_in_our_config(monkeypatch):
    calls = install_fake(monkeypatch, [{"connected": False}, connected(address=42)])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089",
        lambda: {"brainco_revo2": {"left": {"slave_id": 42}}})
    hold.start("brainco_revo2", "left")
    assert calls[1][1]["options"]["slave_id"] == 42
    hold.stop()


def test_dds_override_does_not_send_modbus_address():
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089",
        lambda: {"brainco_revo2": {"transport": "dds", "left": {"network_interface": "enp86s0"}}})
    assert hold._connection_request("brainco_revo2", "left") == {
        "device_id": "brainco_revo2", "transport": "dds",
        "options": {"sides": "left", "network_interface": "enp86s0"}}


def test_connection_changed_during_hold_stops_without_command(monkeypatch):
    calls = install_fake(monkeypatch, [connected("right", 127)])
    hold = hand_hold.HandHoldController(lambda: "http://localhost:18089")
    hold._state.update(running=True, error_count=0)
    stopped = threading.Event()
    hold._run("left", stopped, hold._connection_request("brainco_revo2", "left"))
    assert stopped.is_set()
    assert not hold.status()["running"]
    assert "不一致" in hold.status()["last_error"]
    assert calls == [("status", None)]
