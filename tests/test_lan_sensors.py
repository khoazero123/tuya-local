"""Tests for the LAN IP / MAC diagnostic sensors (fork, PATCHES.md #21)."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import macmap
from custom_components.tuya_local import sensor as sensor_module
from custom_components.tuya_local.const import CONF_DEVICE_ID, CONF_TYPE, DOMAIN
from custom_components.tuya_local.sensor import (
    TuyaLocalIpAddressSensor,
    TuyaLocalMacAddressSensor,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _device():
    device = Mock()
    device.unique_id = "dev123"
    device.device_info = {"identifiers": {(DOMAIN, "dev123")}, "name": "Bếp"}
    return device


def _entry(hass, host="192.168.1.50"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=24,
        title="Bếp",
        data={CONF_DEVICE_ID: "dev123", CONF_TYPE: "zitech_ztbox", CONF_HOST: host},
    )
    entry.add_to_hass(hass)
    return entry


async def test_ip_sensor_follows_the_entry_host(hass):
    entry = _entry(hass)
    sensor = TuyaLocalIpAddressSensor(_device(), entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == "192.168.1.50"
    assert sensor.name == "IP address"
    assert sensor.unique_id == "dev123_lan_ip"
    assert sensor.icon == "mdi:ip-network"

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: "192.168.1.77"}
    )
    await hass.async_block_till_done()
    await sensor.async_update()
    assert sensor.native_value == "192.168.1.77"


async def test_mac_sensor_prefers_the_learned_mac(hass):
    entry = _entry(hass)
    hass.data.setdefault(DOMAIN, {})[macmap.MACS_CACHE] = {
        "dev123": "AA:BB:CC:DD:EE:FF"
    }
    sensor = TuyaLocalMacAddressSensor(_device(), entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == "AA:BB:CC:DD:EE:FF"
    assert sensor.unique_id == "dev123_mac_address"
    assert sensor.icon == "mdi:identifier"


async def test_mac_sensor_falls_back_to_the_arp_table(hass, monkeypatch):
    entry = _entry(hass)
    hass.data.setdefault(DOMAIN, {})[macmap.MACS_CACHE] = {}
    monkeypatch.setattr(
        sensor_module,
        "async_arp_table",
        AsyncMock(return_value={"192.168.1.50": "11:22:33:44:55:66"}),
    )
    sensor = TuyaLocalMacAddressSensor(_device(), entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == "11:22:33:44:55:66"


async def test_mac_sensor_is_empty_when_nothing_is_known(hass, monkeypatch):
    entry = _entry(hass)
    hass.data.setdefault(DOMAIN, {})[macmap.MACS_CACHE] = {}
    monkeypatch.setattr(sensor_module, "async_arp_table", AsyncMock(return_value={}))
    sensor = TuyaLocalMacAddressSensor(_device(), entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value is None


# --- group A diagnostics (PATCHES.md #22) ---------------------------------


def _dev(**kwargs):
    device = _device()
    for key, value in kwargs.items():
        setattr(device, key, value)
    return device


async def test_last_seen_sensor_shows_a_timestamp(hass):
    from datetime import UTC, datetime

    entry = _entry(hass)
    device = _dev(last_seen=1_760_000_000.0)
    sensor = sensor_module.TuyaLocalLastSeenSensor(device, entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == datetime.fromtimestamp(1_760_000_000.0, UTC)
    assert sensor.unique_id == "dev123_last_seen"


async def test_response_time_sensor(hass):
    entry = _entry(hass)
    sensor = sensor_module.TuyaLocalResponseTimeSensor(
        _dev(response_time_ms=12.345), entry.entry_id, "dev123"
    )
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == 12.3
    assert sensor.native_unit_of_measurement == "ms"


async def test_protocol_and_datapoint_sensors(hass):
    entry = _entry(hass)
    device = _dev(protocol_version="3.3", dp_count=7)

    proto = sensor_module.TuyaLocalProtocolVersionSensor(
        device, entry.entry_id, "dev123"
    )
    count = sensor_module.TuyaLocalDatapointCountSensor(
        device, entry.entry_id, "dev123"
    )
    for sensor in (proto, count):
        sensor.hass = hass
        await sensor.async_update()

    assert proto.native_value == "3.3"
    assert count.native_value == 7


async def test_last_error_sensor_reports_none_and_errors(hass):
    entry = _entry(hass)
    device = _dev(last_error=None)
    sensor = sensor_module.TuyaLocalLastErrorSensor(device, entry.entry_id, "dev123")
    sensor.hass = hass

    await sensor.async_update()
    assert sensor.native_value == "none"

    device.last_error = {
        "code": "914",
        "message": "Check device config",
        "at": 1_760_000_000.0,
    }
    await sensor.async_update()
    assert sensor.native_value == "914"
    assert sensor.extra_state_attributes["message"] == "Check device config"


async def test_parent_hub_and_config_type_sensors(hass):
    entry = _entry(hass)
    device = _dev(parent_id="hub123", node_id="node456")

    parent = sensor_module.TuyaLocalParentHubSensor(device, entry.entry_id, "dev123")
    kind = sensor_module.TuyaLocalConfigTypeSensor(
        device, entry.entry_id, "dev123", "zitech_ztbox"
    )
    for sensor in (parent, kind):
        sensor.hass = hass
        await sensor.async_update()

    assert parent.native_value == "hub123"
    assert parent.extra_state_attributes["node_id"] == "node456"
    assert kind.native_value == "zitech_ztbox"


async def test_connected_binary_sensor(hass):
    from custom_components.tuya_local.binary_sensor import (
        TuyaLocalConnectedBinarySensor,
    )

    entry = _entry(hass)
    device = _dev(has_returned_state=True)
    sensor = TuyaLocalConnectedBinarySensor(device, entry.entry_id, "dev123")
    sensor.hass = hass

    assert sensor.is_on is True
    assert sensor.unique_id == "dev123_connected"

    device.has_returned_state = False
    assert sensor.is_on is False

    device.has_returned_state = True
    assert sensor.is_on is True
