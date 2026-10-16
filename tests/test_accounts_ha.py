"""Tests for the Tuya cloud account store and its services."""

import json

import pytest
from homeassistant.helpers.storage import Store

from custom_components.tuya_local import accounts, cloud
from custom_components.tuya_local.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _auth(user_code="user-one", endpoint="https://openapi.tuyaeu.com"):
    return {
        "user_code": user_code,
        "terminal_id": "terminal",
        "endpoint": endpoint,
        "token_info": {
            "t": "t",
            "uid": "uid",
            "expire_time": 1,
            "access_token": "secret-access",
            "refresh_token": "secret-refresh",
        },
    }


async def test_first_login_becomes_the_active_account(hass):
    account = await accounts.async_store_login(hass, _auth())

    assert account["label"] == "account 1"
    data = await accounts.async_list(hass)
    assert data["count"] == 1
    assert data["active"] == account["id"]
    assert data["accounts"][0]["active"] is True


async def test_logins_with_different_user_codes_are_separate_accounts(hass):
    first = await accounts.async_store_login(hass, _auth("user-one"))
    second = await accounts.async_store_login(hass, _auth("user-two"))

    assert first["id"] != second["id"]
    data = await accounts.async_list(hass)
    assert data["count"] == 2
    # The newest login is the one in use.
    assert data["active"] == second["id"]


async def test_reauthenticating_an_account_updates_it(hass):
    first = await accounts.async_store_login(hass, _auth("user-one"))
    fresh = _auth("user-one")
    fresh["terminal_id"] = "rotated-terminal"
    again = await accounts.async_store_login(hass, fresh)

    assert again["id"] == first["id"]
    assert (await accounts.async_list(hass))["count"] == 1


async def test_tokens_never_appear_in_the_account_listing(hass):
    await accounts.async_store_login(hass, _auth())
    listing = json.dumps(await accounts.async_list(hass))

    assert "secret-access" not in listing
    assert "secret-refresh" not in listing


async def test_the_stored_auth_is_shared_with_cloud_instances(hass):
    await accounts.async_store_login(hass, _auth())
    bound = await accounts.async_cloud_for(hass)

    assert bound is not None
    assert bound.is_authenticated
    # Same dict, so a token refresh on one instance is seen by the others.
    assert bound.authentication is (await accounts.async_resolve(hass))["auth"]


async def test_rename_set_active_and_remove(hass):
    first = await accounts.async_store_login(hass, _auth("user-one"))
    second = await accounts.async_store_login(hass, _auth("user-two"))

    assert await accounts.async_rename(hass, first["id"], "nhà chính") is True
    assert await accounts.async_set_active(hass, first["id"]) is True
    data = await accounts.async_list(hass)
    assert data["active"] == first["id"]
    assert data["accounts"][0]["label"] == "nhà chính"

    assert await accounts.async_remove(hass, first["id"]) is True
    data = await accounts.async_list(hass)
    assert data["count"] == 1
    # Removing the active account falls back to the one left.
    assert data["active"] == second["id"]

    assert await accounts.async_remove(hass, "no-such-account") is False


async def test_legacy_single_login_is_imported_once(hass):
    await Store(hass, 1, accounts.LEGACY_STORAGE_KEY, private=True).async_save(
        _auth("legacy")
    )

    data = await accounts.async_list(hass)

    assert data["count"] == 1
    assert data["accounts"][0]["id"] == "default"
    assert data["accounts"][0]["source"] == "legacy"
    assert data["accounts"][0]["user_code"] == "legacy"
    assert data["active"] == "default"


async def test_restore_auth_uses_the_active_account(hass):
    await accounts.async_store_login(hass, _auth("user-one"))
    second = await accounts.async_store_login(hass, _auth("user-two"))
    await accounts.async_set_active(hass, second["id"])
    hass.data.setdefault(DOMAIN, {})["auth_restored"] = False
    hass.data[DOMAIN].pop("auth_cache", None)

    await cloud.async_restore_auth(hass)

    assert hass.data[DOMAIN]["auth_cache"]["user_code"] == "user-two"


async def test_save_auth_stores_an_account(hass):
    cloud.async_save_auth(hass, _auth("from-cloud"))
    await hass.async_block_till_done()

    data = await accounts.async_list(hass)
    assert data["count"] == 1
    assert data["accounts"][0]["user_code"] == "from-cloud"


async def test_save_auth_with_none_does_not_remove_an_account(hass):
    await accounts.async_store_login(hass, _auth())

    cloud.async_save_auth(hass, None)
    await hass.async_block_till_done()

    # A failed attempt clears the in-memory login only.
    assert hass.data[DOMAIN]["auth_cache"] is None
    assert (await accounts.async_list(hass))["count"] == 1


async def test_find_device_searches_every_account(hass, mocker):
    """The key fix: a device owned by a second account is still found."""
    first = await accounts.async_store_login(hass, _auth("user-one"))
    second = await accounts.async_store_login(hass, _auth("user-two"))
    await accounts.async_set_active(hass, first["id"])

    devices = {
        first["id"]: {"eb111": {"id": "eb111", "name": "đèn phòng khách"}},
        second["id"]: {"eb222": {"id": "eb222", "name": "công tắc mới"}},
    }
    mocker.patch.object(
        accounts,
        "async_account_devices",
        side_effect=lambda hass, aid=None, **kw: devices.get(aid, {}),
    )

    found, account_id = await accounts.async_find_device(hass, "eb222")
    assert found is not None and found["name"] == "công tắc mới"
    assert account_id == second["id"]

    missing, account_id = await accounts.async_find_device(hass, "eb999")
    assert missing is None and account_id is None


async def test_all_device_names_merges_accounts(hass, mocker):
    first = await accounts.async_store_login(hass, _auth("user-one"))
    second = await accounts.async_store_login(hass, _auth("user-two"))
    devices = {
        first["id"]: {"eb111": {"id": "eb111", "name": "one"}},
        second["id"]: {"eb222": {"id": "eb222", "name": "two"}},
    }
    mocker.patch.object(
        accounts,
        "async_account_devices",
        side_effect=lambda hass, aid=None, **kw: devices.get(aid, {}),
    )

    names = await accounts.async_all_device_names(hass)

    assert names == {"eb111": "one", "eb222": "two"}


async def test_account_services_are_registered_and_list(hass):
    await accounts.async_store_login(hass, _auth("user-one"))
    await accounts.async_setup_account_services(hass)

    for service in (
        accounts.SERVICE_ACCOUNT_LIST,
        accounts.SERVICE_ACCOUNT_LOGIN_START,
        accounts.SERVICE_ACCOUNT_LOGIN_FINISH,
        accounts.SERVICE_ACCOUNT_RENAME,
        accounts.SERVICE_ACCOUNT_SET_ACTIVE,
        accounts.SERVICE_ACCOUNT_REMOVE,
    ):
        assert hass.services.has_service(DOMAIN, service)

    # Registering twice must not raise (setup runs once per entry).
    await accounts.async_setup_account_services(hass)

    result = await hass.services.async_call(
        DOMAIN, accounts.SERVICE_ACCOUNT_LIST, {}, blocking=True, return_response=True
    )
    assert result["count"] == 1
    assert result["accounts"][0]["user_code"] == "user-one"


async def test_login_finish_without_a_pending_login_reports_it(hass):
    await accounts.async_setup_account_services(hass)

    result = await hass.services.async_call(
        DOMAIN,
        accounts.SERVICE_ACCOUNT_LOGIN_FINISH,
        {"account_id": "nope"},
        blocking=True,
        return_response=True,
    )

    assert result["success"] is False
    assert "no login in progress" in result["error"]
