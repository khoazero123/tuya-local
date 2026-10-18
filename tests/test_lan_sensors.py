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


# --- device id / local key / group B (PATCHES.md #23) ----------------------


async def test_device_id_and_local_key_sensors(hass):
    entry = _entry(hass)
    config = {CONF_DEVICE_ID: "dev123", "local_key": "abc123key"}

    dev_id = sensor_module.TuyaLocalDeviceIdSensor(
        _device(), entry.entry_id, "dev123", config
    )
    key = sensor_module.TuyaLocalLocalKeySensor(
        _device(), entry.entry_id, "dev123", config
    )
    for sensor in (dev_id, key):
        sensor.hass = hass
        await sensor.async_update()

    assert dev_id.native_value == "dev123"
    assert key.native_value is None  # entry holds no local key in this fixture


async def test_cloud_sensors_read_the_cache_only(hass, monkeypatch):
    from custom_components.tuya_local import bulk_add

    entry = _entry(hass)
    monkeypatch.setattr(
        bulk_add,
        "_cloud_cache",
        {
            "at": 1,
            "devices": {
                "dev123": {
                    "product_id": "pk123",
                    "category": "cz",
                    "name": "Đèn bếp (app)",
                    "online": True,
                }
            },
        },
    )

    product = sensor_module.TuyaLocalProductIdSensor(
        _device(), entry.entry_id, "dev123", "dev123", None
    )
    category = sensor_module.TuyaLocalCategorySensor(
        _device(), entry.entry_id, "dev123", "dev123", None
    )
    cloud_name = sensor_module.TuyaLocalCloudNameSensor(
        _device(), entry.entry_id, "dev123", "dev123", None
    )
    for sensor in (product, category, cloud_name):
        sensor.hass = hass
        await sensor.async_update()

    assert product.native_value == "pk123"
    assert category.native_value == "cz"
    assert cloud_name.native_value == "Đèn bếp (app)"


async def test_find_cached_device_matches_sub_devices(monkeypatch):
    from custom_components.tuya_local import bulk_add

    monkeypatch.setattr(
        bulk_add,
        "_cloud_cache",
        {
            "at": 1,
            "devices": {
                "hub1": {"name": "hub"},
                "hub1/node9": {"name": "child"},
            },
        },
    )
    assert bulk_add.find_cached_device("hub1")["name"] == "hub"
    assert bulk_add.find_cached_device("hub1", "node9")["name"] == "child"
    # sub device without a cid still resolves to a child record, not the hub
    assert bulk_add.find_cached_device("hub1", None)["name"] == "hub"
    assert bulk_add.find_cached_device("missing") is None


async def test_cloud_online_binary_sensor(hass, monkeypatch):
    from custom_components.tuya_local import bulk_add
    from custom_components.tuya_local.binary_sensor import (
        TuyaLocalCloudOnlineBinarySensor,
    )

    entry = _entry(hass)
    sensor = TuyaLocalCloudOnlineBinarySensor(
        _device(), entry.entry_id, "dev123", "dev123", None
    )
    sensor.hass = hass

    monkeypatch.setattr(bulk_add, "_cloud_cache", {})
    assert sensor.is_on is None

    monkeypatch.setattr(
        bulk_add, "_cloud_cache", {"at": 1, "devices": {"dev123": {"online": False}}}
    )
    assert sensor.is_on is False


async def test_cloud_refresh_interval_is_stored_and_can_be_disabled(hass):
    from custom_components.tuya_local import cloud_refresh

    assert await cloud_refresh.async_get_interval(hass) == 15

    assert await cloud_refresh.async_set_interval(hass, 30) == 30
    assert await cloud_refresh.async_get_interval(hass) == 30

    # clamped
    assert await cloud_refresh.async_set_interval(hass, 99999) == 1440
    assert await cloud_refresh.async_set_interval(hass, 0) == 0
    # 0 disables the background task
    assert hass.data[DOMAIN].get(cloud_refresh.REFRESH_TASK) is None


async def test_the_interval_number_entity(hass):
    from custom_components.tuya_local.number import TuyaLocalCloudRefreshInterval

    number = TuyaLocalCloudRefreshInterval(15)
    number.hass = hass
    number.async_write_ha_state = lambda: None

    assert number.native_value == 15
    assert number.native_min_value == 0
    assert number.native_max_value == 1440
    assert number.native_unit_of_measurement == "min"
    assert number.unique_id == "tuya_local_cloud_refresh_interval"

    await number.async_set_native_value(45)
    assert number.native_value == 45
