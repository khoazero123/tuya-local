"""Tuya cloud accounts: store several logins, and manage them over services.

One Tuya app account only ever sees the devices paired to *that* account, and a
device's local key can only be read from the cloud list of the account that owns
it.  A home that mixes devices from two accounts (a second app, a vendor app, a
second data centre) therefore needs both logins available: this module keeps
them in one private store and hands the rest of the integration the account it
asks for.

Accounts are created either by the Home Assistant QR login (``account_login_*``
services, also driven from the config flow) or by a session pushed in from the
mobile app (``sync_cloud_session``).  The store is private
(``/config/.storage/tuya_local.cloud_accounts``); tokens never leave it, and no
service response contains one.

An existing single login from before this module existed is imported as the
first account on first load, so nothing has to be re-authenticated.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.cloud_accounts"
STORAGE_VERSION = 1
ACCOUNTS_CACHE = "cloud_accounts"
ACCOUNTS_STORE = "cloud_accounts_store"

# The single-login store from before multi-account support; read once to seed
# the first account, then left alone so a rollback still finds a login.
LEGACY_STORAGE_KEY = f"{DOMAIN}.cloud_auth"

PENDING_LOGINS = "cloud_pending_logins"
DEVICE_CACHE = "cloud_account_devices"
DEVICE_CACHE_TTL = 600

SERVICE_ACCOUNT_LIST = "account_list"
SERVICE_ACCOUNT_LOGIN_START = "account_login_start"
SERVICE_ACCOUNT_LOGIN_FINISH = "account_login_finish"
SERVICE_ACCOUNT_RENAME = "account_rename"
SERVICE_ACCOUNT_SET_ACTIVE = "account_set_active"
SERVICE_ACCOUNT_REMOVE = "account_remove"

# The Tuya app scans this payload to authorise a login.
QR_PAYLOAD = "tuyaSmart--qrLogin?token={}"


def _store(hass: HomeAssistant) -> Store:
    """Return the persistent store for the cloud accounts."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(ACCOUNTS_STORE)
    if store is None:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        domain_data[ACCOUNTS_STORE] = store
    return store


def is_auth(auth: Any) -> bool:
    """Whether a payload looks like a tuya_sharing login we can use."""
    return (
        isinstance(auth, dict)
        and isinstance(auth.get("token_info"), dict)
        and bool(auth.get("endpoint"))
        and bool(auth.get("user_code"))
    )


def sanitize(account: dict[str, Any], active: str | None = None) -> dict[str, Any]:
    """Return an account without its tokens, for a service response or a log."""
    auth = account.get("auth") or {}
    return {
        "id": account.get("id"),
        "label": account.get("label"),
        "source": account.get("source"),
        "user_code": auth.get("user_code"),
        "endpoint": auth.get("endpoint"),
        "created": account.get("created"),
        "last_used": account.get("last_used"),
        "active": account.get("id") == active,
        "has_token": bool((auth.get("token_info") or {}).get("access_token")),
    }


async def async_load(hass: HomeAssistant) -> dict[str, Any]:
    """Return the stored accounts document, migrating the old login if needed."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(ACCOUNTS_CACHE)
    if cached is not None:
        return cached

    data: dict[str, Any] = {"accounts": [], "active": None}
    try:
        stored = await _store(hass).async_load()
        if isinstance(stored, dict):
            accounts = stored.get("accounts")
            if isinstance(accounts, list):
                data["accounts"] = [a for a in accounts if isinstance(a, dict)]
            active = stored.get("active")
            if isinstance(active, str):
                data["active"] = active
    except Exception as err:  # noqa: BLE001 - never block setup on the store
        _LOGGER.warning("Could not load stored Tuya cloud accounts: %s", err)

    if not data["accounts"]:
        migrated = await _async_migrate_legacy(hass)
        if migrated is not None:
            data["accounts"] = [migrated]
            data["active"] = migrated["id"]
            await async_save(hass, data)

    domain_data[ACCOUNTS_CACHE] = data
    return data


async def _async_migrate_legacy(hass: HomeAssistant) -> dict[str, Any] | None:
    """Import the single pre-multi-account login as the first account."""
    try:
        legacy = await Store(
            hass, STORAGE_VERSION, LEGACY_STORAGE_KEY, private=True
        ).async_load()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("No legacy Tuya cloud login to import: %s", err)
        return None
    if not is_auth(legacy):
        return None
    _LOGGER.info(
        "Imported the existing Tuya cloud login as account 'default' (user code %s)",
        legacy.get("user_code"),
    )
    return {
        "id": "default",
        "label": "default",
        "source": "legacy",
        "created": time.time(),
        "last_used": time.time(),
        "auth": legacy,
    }


async def async_save(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Persist the accounts document (and keep the in-memory copy current)."""
    hass.data.setdefault(DOMAIN, {})[ACCOUNTS_CACHE] = data
    try:
        await _store(hass).async_save(data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save Tuya cloud accounts: %s", err)


async def async_resolve(
    hass: HomeAssistant, account_id: str | None = None
) -> dict[str, Any] | None:
    """Return one account: the requested one, else the active one."""
    data = await async_load(hass)
    accounts = data["accounts"]
    if not accounts:
        return None
    if account_id:
        for account in accounts:
            if account.get("id") == account_id:
                return account
        return None
    active = data.get("active")
    for account in accounts:
        if account.get("id") == active:
            return account
    return accounts[0]


async def async_store_login(
    hass: HomeAssistant,
    auth: dict[str, Any] | None,
    *,
    label: str | None = None,
    account_id: str | None = None,
    source: str = "qr",
    make_active: bool = True,
) -> dict[str, Any] | None:
    """Create or update the account this login belongs to.

    A login is matched to an account by ``user_code``, so re-authenticating an
    account refreshes it instead of piling up duplicates.  Returns a sanitized
    account, or None when there is nothing to store.
    """
    if not is_auth(auth):
        return None

    data = await async_load(hass)
    accounts = data["accounts"]
    account = None
    if account_id:
        for candidate in accounts:
            if candidate.get("id") == account_id:
                account = candidate
                break
    if account is None:
        for candidate in accounts:
            existing = (candidate.get("auth") or {}).get("user_code")
            if existing and existing == auth.get("user_code"):
                account = candidate
                break

    if account is None:
        account = {
            "id": account_id or uuid4().hex[:12],
            "label": label or f"account {len(accounts) + 1}",
            "source": source,
            "created": time.time(),
        }
        accounts.append(account)
        _LOGGER.info(
            "Stored Tuya cloud account '%s' (user code %s)",
            account["label"],
            auth.get("user_code"),
        )
    else:
        if label:
            account["label"] = label
        if source:
            account["source"] = source

    # Mutate the stored dict in place: every Cloud bound to this account shares
    # it, so a token refresh is seen by the others.
    account["auth"] = auth
    account["last_used"] = time.time()

    if make_active or not data.get("active"):
        data["active"] = account["id"]

    await async_save(hass, data)
    return sanitize(account, data.get("active"))


def async_store_login_nowait(
    hass: HomeAssistant, auth: dict[str, Any] | None, **kwargs: Any
) -> None:
    """Schedule ``async_store_login``; for sync callers such as Cloud.async_login."""
    hass.async_create_task(async_store_login(hass, auth, **kwargs))


async def async_remove(hass: HomeAssistant, account_id: str) -> bool:
    """Delete an account. The active one falls back to the first remaining."""
    data = await async_load(hass)
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a.get("id") != account_id]
    if len(data["accounts"]) == before:
        return False
    if data.get("active") == account_id:
        data["active"] = data["accounts"][0]["id"] if data["accounts"] else None
    await async_save(hass, data)
    _LOGGER.info("Removed Tuya cloud account %s", account_id)
    return True


async def async_rename(hass: HomeAssistant, account_id: str, label: str) -> bool:
    """Give an account a human readable label."""
    data = await async_load(hass)
    for account in data["accounts"]:
        if account.get("id") == account_id:
            account["label"] = label
            await async_save(hass, data)
            return True
    return False


async def async_set_active(hass: HomeAssistant, account_id: str) -> bool:
    """Choose the account used by default (config flow, device list, names)."""
    data = await async_load(hass)
    for account in data["accounts"]:
        if account.get("id") == account_id:
            data["active"] = account_id
            await async_save(hass, data)
            _LOGGER.info("Active Tuya cloud account is now '%s'", account.get("label"))
            return True
    return False


async def async_list(hass: HomeAssistant) -> dict[str, Any]:
    """Sanitized accounts plus which one is active."""
    data = await async_load(hass)
    active = data.get("active")
    return {
        "active": active,
        "count": len(data["accounts"]),
        "accounts": [sanitize(a, active) for a in data["accounts"]],
    }


async def async_cloud_for(hass: HomeAssistant, account_id: str | None = None):
    """Return a Cloud bound to one account, or None when there is none."""
    from .cloud import Cloud  # local import: cloud imports this module too

    account = await async_resolve(hass, account_id)
    if account is None:
        return None
    auth = account.get("auth")
    if not is_auth(auth):
        return None
    # Pass the stored dict itself so token refreshes are shared.
    return Cloud(hass, auth)


async def async_account_devices(
    hass: HomeAssistant, account_id: str | None = None, *, force: bool = False
) -> dict[str, Any]:
    """Return ``{index: device}`` for an account, cached to spare the cloud."""
    account = await async_resolve(hass, account_id)
    if account is None:
        return {}
    key = account.get("id")
    cache = hass.data.setdefault(DOMAIN, {}).setdefault(DEVICE_CACHE, {})
    hit = cache.get(key)
    if hit and not force and (time.monotonic() - hit[0]) < DEVICE_CACHE_TTL:
        return hit[1]

    cloud = await async_cloud_for(hass, key)
    if cloud is None:
        return {}
    try:
        devices = await cloud.async_get_devices()
    except Exception as err:  # noqa: BLE001 - a dead account must not break lookups
        _LOGGER.warning("Could not read devices for account %s: %s", key, err)
        return {}
    cache[key] = (time.monotonic(), devices)
    return devices


async def async_find_device(
    hass: HomeAssistant, device_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Find a device id in any connected account; active account first.

    This is what makes a discovered device usable when its local key belongs to
    a *different* account than the one that happens to be active.
    """
    data = await async_load(hass)
    accounts = list(data["accounts"])
    active = data.get("active")
    accounts.sort(key=lambda a: a.get("id") != active)

    for account in accounts:
        devices = await async_account_devices(hass, account.get("id"))
        for index, device in devices.items():
            if device.get("id") != device_id and not str(index).startswith(
                f"{device_id}/"
            ):
                continue
            return device, account.get("id")
    return None, None


async def async_all_device_names(
    hass: HomeAssistant, *, force: bool = False
) -> dict[str, str]:
    """Merge ``{device_id: name}`` over every account (first match wins)."""
    data = await async_load(hass)
    accounts = list(data["accounts"])
    active = data.get("active")
    accounts.sort(key=lambda a: a.get("id") != active)

    names: dict[str, str] = {}
    for account in accounts:
        devices = await async_account_devices(hass, account.get("id"), force=force)
        for device in devices.values():
            device_id = device.get("id")
            name = device.get("name")
            if device_id and name:
                names.setdefault(device_id, name)
    return names


async def _async_login_start(call: ServiceCall) -> dict[str, Any]:
    """Begin a QR login: return the payload the Tuya app has to scan."""
    from .cloud import Cloud

    hass = call.hass
    user_code = call.data["user_code"]
    account_id = call.data.get("account_id") or uuid4().hex[:12]

    cloud = Cloud(hass)
    qr_code = await cloud.async_get_qr_code(user_code)
    if not qr_code:
        return {
            "success": False,
            "account_id": account_id,
            "error": cloud.last_error or "could not start the login",
        }

    hass.data.setdefault(DOMAIN, {}).setdefault(PENDING_LOGINS, {})[account_id] = cloud
    _LOGGER.info(
        "Started Tuya QR login for account '%s'", call.data.get("label") or account_id
    )
    return {
        "success": True,
        "account_id": account_id,
        "user_code": user_code,
        "qr": qr_code,
        # Render this as a QR code and scan it with the Tuya/SmartLife app.
        "qr_payload": QR_PAYLOAD.format(qr_code),
    }


async def _async_login_finish(call: ServiceCall) -> dict[str, Any]:
    """Complete a QR login started with ``account_login_start``."""
    hass = call.hass
    account_id = call.data["account_id"]
    pending = hass.data.setdefault(DOMAIN, {}).setdefault(PENDING_LOGINS, {})
    cloud = pending.get(account_id)
    if cloud is None:
        return {
            "success": False,
            "account_id": account_id,
            "error": "no login in progress for this account_id",
        }

    if not await cloud.async_login():
        return {
            "success": False,
            "account_id": account_id,
            "error": cloud.last_error or "login not confirmed yet",
            "retry": True,
        }

    pending.pop(account_id, None)
    account = await async_store_login(
        hass,
        cloud.authentication,
        label=call.data.get("label"),
        account_id=account_id,
        source="qr",
    )
    _LOGGER.info("Tuya cloud account '%s' connected", (account or {}).get("label"))
    return {"success": True, "account": account}


async def _async_account_list(call: ServiceCall) -> dict[str, Any]:
    return await async_list(call.hass)


async def _async_account_rename(call: ServiceCall) -> dict[str, Any]:
    ok = await async_rename(call.hass, call.data["account_id"], call.data["label"])
    return {"success": ok}


async def _async_account_set_active(call: ServiceCall) -> dict[str, Any]:
    ok = await async_set_active(call.hass, call.data["account_id"])
    return {"success": ok}


async def _async_account_remove(call: ServiceCall) -> dict[str, Any]:
    ok = await async_remove(call.hass, call.data["account_id"])
    return {"success": ok}


async def async_setup_account_services(hass: HomeAssistant) -> None:
    """Register the account services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_ACCOUNT_LIST):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_LIST,
        _async_account_list,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_LOGIN_START,
        _async_login_start,
        schema=vol.Schema(
            {
                vol.Required("user_code"): str,
                vol.Optional("label"): str,
                vol.Optional("account_id"): str,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_LOGIN_FINISH,
        _async_login_finish,
        schema=vol.Schema(
            {
                vol.Required("account_id"): str,
                vol.Optional("label"): str,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_RENAME,
        _async_account_rename,
        schema=vol.Schema(
            {
                vol.Required("account_id"): str,
                vol.Required("label"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_SET_ACTIVE,
        _async_account_set_active,
        schema=vol.Schema({vol.Required("account_id"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACCOUNT_REMOVE,
        _async_account_remove,
        schema=vol.Schema({vol.Required("account_id"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
