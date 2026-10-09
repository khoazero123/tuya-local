"""Test RF support cho ``ir2mqtt_bridge`` (thuần Python, KHÔNG cần Home Assistant).

Chạy trực tiếp::

    python3 tests/test_ir2mqtt_bridge_rf.py

Script tự stub các module ``homeassistant`` cần thiết khi máy không cài HA, và
``patch`` ``mqtt.async_publish`` để bắt các message publish (hoạt động cả khi có
HA thật, không để lại side-effect). Nếu chạy bằng ``pytest`` các hàm ``test_*``
cũng được collect (mỗi test là hàm sync, tự gọi ``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "custom_components" / "tuya_local" / "ir2mqtt_bridge.py"


# --------------------------------------------------------------- HA stubs (?)
def _ensure_homeassistant() -> None:
    """Cài stub tối thiểu nếu máy chưa có Home Assistant."""
    try:
        import homeassistant.components.mqtt  # noqa: F401
        import homeassistant.core  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("homeassistant.core")

    def callback(func):
        return func

    class HomeAssistant:  # noqa: D401 - stub
        pass

    core.callback = callback
    core.HomeAssistant = HomeAssistant

    components = types.ModuleType("homeassistant.components")
    components.__path__ = []  # type: ignore[attr-defined]
    mqtt = types.ModuleType("homeassistant.components.mqtt")

    async def async_publish(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("mqtt.async_publish chưa được patch trong test")

    async def async_subscribe(*args, **kwargs):  # noqa: ANN002, ANN003
        return lambda: None

    mqtt.async_publish = async_publish
    mqtt.async_subscribe = async_subscribe

    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.mqtt", mqtt)


_ensure_homeassistant()

_spec = importlib.util.spec_from_file_location("ir2mqtt_bridge_undertest", BRIDGE_PATH)
bridge_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge_mod)  # type: ignore[union-attr]


# ------------------------------------------------------------------- fakes
class FakeDevice:
    _address = "10.0.0.9"


class FakeEntity:
    def __init__(self) -> None:
        self._device = FakeDevice()
        self.sent: list[tuple[list[str], dict]] = []

    async def async_send_command(self, command, **kwargs) -> None:
        self.sent.append((list(command), kwargs))


class FakeHass:
    loop = asyncio.new_event_loop()

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro, loop=self.loop)


def _make_bridge():
    entity = FakeEntity()
    bridge = bridge_mod.IR2MQTTBridge(FakeHass(), entity, "testbridge")
    return bridge, entity


@contextmanager
def _capture(out: list):
    async def fake_publish(hass, topic, payload, qos=0, retain=False):
        out.append({"topic": topic, "payload": json.loads(payload), "retain": retain})

    with patch.object(bridge_mod.mqtt, "async_publish", fake_publish):
        yield out


def _send(bridge, code_json: dict) -> list:
    out: list = []
    payload = json.dumps({"command": "send", "request_id": "r1", "code": code_json})
    with _capture(out):
        asyncio.run(bridge._async_handle_command(payload))
    return out


def _received(bridge, code: str) -> list:
    out: list = []
    with _capture(out):
        asyncio.run(bridge.async_publish_received(code))
    return out


def _b64(pulses: list[int]) -> str:
    return bridge_mod.to_b64(list(pulses))


def _response(out: list) -> dict:
    return next(m["payload"] for m in out if m["topic"].endswith("/response"))


# --------------------------------------------------------------------- tests
def test_is_rf_payload_detection():
    """(a) Nhận diện RF qua payload.rf (và receiver_id dự phòng)."""
    assert bridge_mod.is_rf_payload({"timings": [1, 2], "rf": True}) is True
    assert (
        bridge_mod.is_rf_payload({"timings": [1], "rf": True, "receiver_id": "rf"})
        is True
    )
    assert bridge_mod.is_rf_payload({"timings": [1], "receiver_id": "rf"}) is True
    # IR không có key rf
    assert bridge_mod.is_rf_payload({"timings": [1]}) is False
    assert bridge_mod.is_rf_payload({"timings": [1], "rf": False}) is False
    assert bridge_mod.is_rf_payload({"timings": [1], "receiver_id": "rx1"}) is False
    assert bridge_mod.is_rf_payload(None) is False  # type: ignore[arg-type]


def test_rf_send_uses_rf_prefix_roundtrip():
    """(b) Gửi RF -> lệnh `rf:<b64>`; b64 round-trip đúng timings."""
    bridge, entity = _make_bridge()
    timings = [9000, -4500, 560, -560, 560, -1690, 560]
    expected = [abs(t) for t in timings]
    chunk_b64, _delay = bridge_mod.split_for_tuya(expected)[0]
    out = _send(
        bridge,
        {
            "protocol": "raw",
            "payload": {"timings": timings, "rf": True, "receiver_id": "rf"},
        },
    )

    assert len(entity.sent) == 1, entity.sent
    commands, kwargs = entity.sent[0]
    assert commands == [bridge_mod.PREFIX_RF + chunk_b64], commands
    assert kwargs == {"num_repeats": 1, "delay_secs": 0.5}
    # round-trip: giải mã lại chuỗi đã gửi (split_for_tuya pad phần tử lẻ bằng 5000)
    assert bridge_mod.b64_to_pulses(commands[0][3:]) == expected + [5000]
    assert _response(out) == {"request_id": "r1", "success": True, "message": ""}


def test_rf_send_chunking_keeps_rf_prefix():
    """Chuỗi RF có gap > 50ms -> tách nhiều chunk, mọi chunk đều prefix rf:."""
    bridge, entity = _make_bridge()
    timings = [9000, -4500, 560, -60000, 560, -1690, 560]
    _send(
        bridge,
        {
            "protocol": "raw",
            "payload": {"timings": timings, "rf": True, "receiver_id": "rf"},
        },
    )
    assert len(entity.sent) == 2, entity.sent
    assert all(c[0][0].startswith(bridge_mod.PREFIX_RF) for c in entity.sent)
    assert all(bridge_mod.b64_to_pulses(c[0][0][3:]) for c in entity.sent)


def test_rf_send_without_rf_key_is_ir():
    """Không có rf=true -> vẫn đi đường IR (b64:)."""
    bridge, entity = _make_bridge()
    out = _send(
        bridge,
        {"protocol": "raw", "payload": {"timings": [300, -300, 300, -300]}},
    )
    commands, _ = entity.sent[0]
    assert commands == [bridge_mod.PREFIX_IR + _b64([300, 300, 300, 300])], commands
    assert _response(out)["success"] is True


def test_ir_send_unchanged_nec():
    """(c) IR (nec) không bị ảnh hưởng: b64: + encode NEC, không có rf:."""
    bridge, entity = _make_bridge()
    out = _send(
        bridge,
        {
            "protocol": "nec",
            "payload": {"address": "0x00FF", "command": "0x1CE3"},
        },
    )
    commands, _ = entity.sent[0]
    assert len(commands) == 1
    assert commands[0].startswith(bridge_mod.PREFIX_IR)
    assert not commands[0].startswith(bridge_mod.PREFIX_RF)
    nec = bridge_mod.nec_pulses(0x00FF, 0x1CE3, 0)
    assert commands[0][4:] == bridge_mod.split_for_tuya(nec)[0][0]
    assert _response(out)["success"] is True


def test_send_error_branch_responds_false():
    """Nhánh lỗi vẫn publish response success=false, không gửi gì."""
    bridge, entity = _make_bridge()
    out = _send(bridge, {"protocol": "khong-ton-tai", "payload": {}})
    assert entity.sent == []
    resp = _response(out)
    assert resp["success"] is False
    assert "chưa hỗ trợ" in resp["message"]


def test_ir_received_unchanged():
    """(c) Receive IR giữ nguyên hành vi cũ."""
    bridge, _ = _make_bridge()
    # (1) timings không khớp NEC/Samsung -> payload chỉ có timings, receiver_id cũ
    pulses = [300, 300, 600, 600, 300, 300]
    out = _received(bridge, _b64(pulses))
    assert len(out) == 1 and out[0]["topic"] == "ir2mqtt/bridge/testbridge/received"
    p = out[0]["payload"]
    assert p["type"] == "received" and p["protocol"] == "raw"
    assert p["receiver_id"] == bridge.receiver_id == "rx1"
    assert p["payload"] == {
        "timings": [v if i % 2 == 0 else -v for i, v in enumerate(pulses)]
    }
    # (2) mã nhận diện được (Samsung) -> giữ nguyên hành vi: protocol/payload đổi,
    #     KHÔNG có key rf
    samsung = bridge_mod.samsung_pulses(0xE0E040BF, 32)
    p2 = _received(bridge, _b64(samsung))[0]["payload"]
    assert p2["protocol"] == "samsung"
    assert p2["payload"] == {"data": "0xE0E040BF", "nbits": 32}
    assert "rf" not in p2["payload"] and p2["receiver_id"] == "rx1"


def test_rf_received_from_prefixed_code():
    """(b) Code học RF ('rf:' + b64) -> payload RF đúng định dạng."""
    bridge, _ = _make_bridge()
    # cố tình dùng timings giống header Samsung để chứng minh RF KHÔNG bị
    # nhận diện lại thành samsung/nec.
    pulses = bridge_mod.samsung_pulses(0xE0E040BF, 32)
    out = _received(bridge, bridge_mod.PREFIX_RF + _b64(pulses))
    assert len(out) == 1
    p = out[0]["payload"]
    assert p["type"] == "received"
    assert p["protocol"] == "raw"
    assert p["receiver_id"] == "rf"
    assert p["payload"]["rf"] is True
    assert p["payload"]["receiver_id"] == "rf"
    assert p["payload"]["timings"] == [
        v if i % 2 == 0 else -v for i, v in enumerate(pulses)
    ]


def test_rf_received_via_explicit_flag():
    """Đường DP không có prefix: caller có thể truyền is_rf=True."""
    bridge, _ = _make_bridge()
    out: list = []
    with _capture(out):
        asyncio.run(
            bridge.async_publish_received(_b64([300, 300, 600, 600]), is_rf=True)
        )
    p = out[0]["payload"]
    assert p["payload"]["rf"] is True and p["protocol"] == "raw"


def test_invalid_code_publishes_nothing():
    bridge, _ = _make_bridge()
    assert _received(bridge, "rf:####") == []
    assert _received(bridge, "$$$$") == []


def test_config_and_state_unchanged():
    """config/state topics + enabled_protocols giữ nguyên."""
    bridge, _ = _make_bridge()
    cfg = json.loads(bridge._config_payload())
    assert cfg["enabled_protocols"] == ["nec", "samsung", "sony", "raw"]
    assert cfg["capabilities"] == ["nec", "samsung", "sony", "raw"]
    assert cfg["receivers"] == [{"id": "rx1"}]
    assert cfg["transmitters"] == [{"id": "tx1"}]
    state = json.loads(bridge._state_payload())
    assert state["enabled_protocols"] == ["nec", "samsung", "sony", "raw"]
    assert state["online"] is True


TESTS = [
    test_is_rf_payload_detection,
    test_rf_send_uses_rf_prefix_roundtrip,
    test_rf_send_chunking_keeps_rf_prefix,
    test_rf_send_without_rf_key_is_ir,
    test_ir_send_unchanged_nec,
    test_send_error_branch_responds_false,
    test_ir_received_unchanged,
    test_rf_received_from_prefixed_code,
    test_rf_received_via_explicit_flag,
    test_invalid_code_publishes_nothing,
    test_config_and_state_unchanged,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"PASS {test.__name__}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} test PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
