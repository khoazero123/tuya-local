"""Tests for forgetting the Tuya cloud login (service + integration button)."""

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import cloud, refresh
from custom_components.tuya_local.button import TuyaLocalCloudLogoutButton
from custom_components.tuya_local.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)

AUTH = {
    "user_code": "user-one",
    "terminal_id": "terminal",
    "endpoint": "https://openapi.tuyaeu.com",
    "token_info": {
        "t": "t",
        "uid": "uid",
        "expire_time": 1,
        "access_token": "***",
        "refresh_token": "***",
    },
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _save_login(hass) -> None:
    cloud.async_save_auth(hass, AUTH)
    await hass.async_block_till_done()


async def test_service_forgets_the_login_in_memory_and_on_disk(hass):
    await refresh.async_setup_refresh_services(hass)
    await _save_login(hass)
    assert cloud.Cloud(hass).is_authenticated is True

    response = await hass.services.async_call(
        DOMAIN, refresh.SERVICE_LOGOUT, {}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()

    assert response == {"success": True, "was_authenticated": True}
    assert cloud.Cloud(hass).is_authenticated is False
    stored = await Store(
        hass, cloud.STORAGE_VERSION, cloud.STORAGE_KEY, private=True
    ).async_load()
    assert stored is None


async def test_logout_without_a_login_says_so(hass):
    await refresh.async_setup_refresh_services(hass)

    response = await hass.services.async_call(
        DOMAIN, refresh.SERVICE_LOGOUT, {}, blocking=True, return_response=True
    )

    assert response == {"success": True, "was_authenticated": False}


async def test_configured_devices_are_untouched_by_a_logout(hass):
    """Device control is local: logging out must not touch the entries."""
    await refresh.async_setup_refresh_services(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=24,
        title="switch",
        data={
            CONF_DEVICE_ID: "dev1",
            CONF_HOST: "192.168.1.50",
            CONF_LOCAL_KEY: "***",
            CONF_PROTOCOL_VERSION: 3.3,
            CONF_TYPE: "zitech_ztbox",
        },
    )
    entry.add_to_hass(hass)
    await _save_login(hass)

    await hass.services.async_call(DOMAIN, refresh.SERVICE_LOGOUT, {}, blocking=True)
    await hass.async_block_till_done()

    assert [e.entry_id for e in hass.config_entries.async_entries(DOMAIN)] == [
        entry.entry_id
    ]


async def test_logout_clears_the_cached_device_names(hass):
    await refresh.async_setup_refresh_services(hass)
    hass.data.setdefault(DOMAIN, {})[refresh.NAMES_CACHE] = {"dev1": "Đèn bếp"}
    await refresh._store(hass).async_save({"dev1": "Đèn bếp"})

    await hass.services.async_call(DOMAIN, refresh.SERVICE_LOGOUT, {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.data[DOMAIN][refresh.NAMES_CACHE] == {}
    assert await refresh._store(hass).async_load() is None


async def test_the_button_logs_out(hass):
    await refresh.async_setup_refresh_services(hass)
    await _save_login(hass)

    button = TuyaLocalCloudLogoutButton()
    button.hass = hass

    assert button.name == "Logout Tuya account"
    assert button.unique_id == "tuya_local_logout_cloud"
    assert button.icon == "mdi:logout"

    await button.async_press()
    await hass.async_block_till_done()

    assert cloud.Cloud(hass).is_authenticated is False
