"""Tests for adding every device of a Tuya cloud account in one go."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_HOST

from custom_components.tuya_local import bulk_add
from custom_components.tuya_local.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Listed under `products` in devices/zitech_ztbox.yaml.
PRODUCT_LISTED = "bolrg9mcadqiejd0"
PRODUCT_UNLISTED = "keyfxuj3d5wg53xx"
# `matches_product` compares literal ids; keep the test product obviously absent.


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _device(device_id: str, **kwargs):
    device = {
        "id": device_id,
        "name": kwargs.pop("name", device_id),
        "local_key": kwargs.pop("local_key", "key-1234567890123"),
        "product_id": kwargs.pop("product_id", PRODUCT_LISTED),
        "ip": kwargs.pop("ip", "192.168.1.50"),
        "support_local": kwargs.pop("support_local", True),
        "is_hub": kwargs.pop("is_hub", False),
        "version": None,
    }
    device.update(kwargs)
    return device


def _add_entry(hass, device_id: str):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=24,
        title=device_id,
        data={
            CONF_DEVICE_ID: device_id,
            CONF_HOST: "192.168.1.10",
            CONF_LOCAL_KEY: "key-1234567890123",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "zitech_ztbox",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _patch_lookups(mocker, devices, *, lan=None, reachable=None):
    mocker.patch.object(bulk_add, "_cloud_devices", AsyncMock(return_value=devices))
    mocker.patch.object(bulk_add, "_lan_addresses", return_value=lan or {})
    mocker.patch.object(
        bulk_add,
        "_probe_hosts",
        return_value=reachable if reachable is not None else {"192.168.1.50": True},
    )


async def test_plan_classifies_every_device(hass, mocker):
    devices = {
        "a": _device("dev_already", name="already"),
        "b": _device("dev_hub", is_hub=True),
        "c": _device("dev_nokey", local_key=""),
        "d": _device("dev_cloudonly", support_local=False),
        "e": _device("dev_nohost", ip=""),
        "f": _device("dev_noconfig", product_id=PRODUCT_UNLISTED),
        "g": _device("dev_ok", name="ok"),
    }
    _patch_lookups(mocker, devices)
    _add_entry(hass, "dev_already")

    plan = await bulk_add.async_plan_devices(hass)

    assert plan["found"] == 7
    assert {i["device_id"]: i["reason"] for i in plan["skip"]} == {
        "dev_already": bulk_add.SKIP_ALREADY,
        "dev_hub": bulk_add.SKIP_HUB,
        "dev_nokey": bulk_add.SKIP_NO_KEY,
        "dev_cloudonly": bulk_add.SKIP_CLOUD_ONLY,
        "dev_nohost": bulk_add.SKIP_NO_HOST,
    }
    assert [i["device_id"] for i in plan["add"]] == ["dev_noconfig", "dev_ok"]
    by_id = {i["device_id"]: i for i in plan["add"]}
    assert by_id["dev_ok"]["type"] == "zitech_ztbox"
    # An unlisted product id is not a dead end: the type is asked from the
    # device when the entries are actually created.
    assert by_id["dev_noconfig"]["needs_probe"] is True
    assert "type" not in by_id["dev_noconfig"]


async def test_plan_takes_the_address_from_the_lan_scan(hass, mocker):
    """The cloud often reports no address; the LAN scan fills it in."""
    _patch_lookups(
        mocker,
        {"a": _device("dev_lan", ip="")},
        lan={"dev_lan": "192.168.1.77"},
        reachable={"192.168.1.77": True},
    )

    plan = await bulk_add.async_plan_devices(hass)

    assert plan["add"][0]["host"] == "192.168.1.77"
    assert plan["add"][0]["reachable"] is True


async def test_plan_can_leave_out_devices_that_are_not_answering(hass, mocker):
    _patch_lookups(
        mocker,
        {"a": _device("dev_off")},
        reachable={"192.168.1.50": False},
    )

    plan = await bulk_add.async_plan_devices(hass, include_unreachable=False)

    assert plan["add"] == []
    assert plan["skip"][0]["reason"] == bulk_add.SKIP_UNREACHABLE


async def test_a_public_cloud_address_is_never_used_as_host(hass, mocker):
    """The cloud reports the WAN address; using it would point outside the LAN."""
    _patch_lookups(
        mocker,
        {"a": _device("dev_wan", ip="171.242.82.77")},
        lan={},
        reachable={},
    )

    plan = await bulk_add.async_plan_devices(hass)

    assert plan["add"] == []
    assert plan["skip"][0]["reason"] == bulk_add.SKIP_NO_HOST


async def test_unknown_type_needs_the_device_to_answer(hass, mocker):
    _patch_lookups(
        mocker,
        {"a": _device("dev_quiet", product_id=PRODUCT_UNLISTED)},
        reachable={"192.168.1.50": False},
    )

    plan = await bulk_add.async_plan_devices(hass)

    assert plan["add"] == []
    assert plan["skip"][0]["reason"] == bulk_add.SKIP_TYPE_UNKNOWN


async def test_the_type_is_inferred_from_the_device_when_needed(hass, mocker):
    mocker.patch("custom_components.tuya_local.async_setup_entry", return_value=True)
    _patch_lookups(
        mocker,
        {"a": _device("dev_guess", name="Ổ cắm", product_id=PRODUCT_UNLISTED)},
        reachable={"192.168.1.50": True},
    )
    mocker.patch.object(
        bulk_add, "_infer_type", AsyncMock(return_value="smartplugv2_energy")
    )
    await bulk_add.async_setup_bulk_services(hass)

    response = await hass.services.async_call(
        DOMAIN,
        bulk_add.SERVICE_ADD_ALL,
        {"dry_run": False},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response["added"] == 1
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_TYPE] == "smartplugv2_energy"


async def test_the_plan_never_returns_local_keys(hass, mocker):
    _patch_lookups(mocker, {"a": _device("dev_ok")})
    await bulk_add.async_setup_bulk_services(hass)

    response = await hass.services.async_call(
        DOMAIN, bulk_add.SERVICE_ADD_ALL, {}, blocking=True, return_response=True
    )

    assert response["devices"]
    assert all("local_key" not in item for item in response["devices"])


async def test_dry_run_creates_nothing(hass, mocker):
    _patch_lookups(mocker, {"a": _device("dev_ok")})
    await bulk_add.async_setup_bulk_services(hass)

    response = await hass.services.async_call(
        DOMAIN, bulk_add.SERVICE_ADD_ALL, {}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()

    assert response["dry_run"] is True
    assert response["to_add"] == 1
    assert response["added"] == 0 if "added" in response else True
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_add_all_creates_the_entries(hass, mocker):
    mocker.patch("custom_components.tuya_local.async_setup_entry", return_value=True)
    _patch_lookups(
        mocker,
        {
            "a": _device("dev_one", name="Đèn phòng khách"),
            "b": _device("dev_two", name="Ổ cắm tivi", ip="192.168.1.51"),
        },
        reachable={"192.168.1.50": True, "192.168.1.51": True},
    )
    await bulk_add.async_setup_bulk_services(hass)

    response = await hass.services.async_call(
        DOMAIN,
        bulk_add.SERVICE_ADD_ALL,
        {"dry_run": False},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response["added"] == 2
    assert response["failed"] == []
    entries = {
        e.data[CONF_DEVICE_ID]: e for e in hass.config_entries.async_entries(DOMAIN)
    }
    assert set(entries) == {"dev_one", "dev_two"}
    assert entries["dev_two"].data[CONF_HOST] == "192.168.1.51"
    assert entries["dev_two"].data[CONF_TYPE] == "zitech_ztbox"
    assert entries["dev_two"].title == "Ổ cắm tivi"


async def test_a_device_that_fails_to_create_is_reported_not_fatal(hass, mocker):
    mocker.patch("custom_components.tuya_local.async_setup_entry", return_value=True)
    _patch_lookups(
        mocker,
        {"a": _device("dev_one"), "b": _device("dev_two", ip="192.168.1.51")},
        reachable={"192.168.1.50": True, "192.168.1.51": True},
    )
    real_init = hass.config_entries.flow.async_init
    calls = {"n": 0}

    async def _init(handler, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await real_init(handler, **kwargs)

    mocker.patch.object(hass.config_entries.flow, "async_init", side_effect=_init)
    await bulk_add.async_setup_bulk_services(hass)

    response = await hass.services.async_call(
        DOMAIN,
        bulk_add.SERVICE_ADD_ALL,
        {"dry_run": False},
        blocking=True,
        return_response=True,
    )

    assert response["added"] == 1
    assert len(response["failed"]) == 1
    assert "boom" in response["failed"][0]["error"]
