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
