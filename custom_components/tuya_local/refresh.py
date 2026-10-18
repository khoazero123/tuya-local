"""On-demand helpers for the Tuya Local integration.

Backs the ``tuya_local.refresh_devices`` and ``tuya_local.relocate_devices``
services and the matching "Refresh device list" button.  Three jobs:

1. re-locate configured devices on the LAN and update the stored host when a DHCP
   change moved them (the entry reloads itself).  The relocation itself lives in
   ``macmap``: it is confirmed by MAC address, so a device that is only
   temporarily unreachable is never moved onto a wrong address,
2. refresh the device names from the Tuya cloud, so discovered devices can be
   labelled with the name set in the Tuya/SmartLife app,
3. re-run the LAN discovery scan so newly seen devices surface again.

Everything here is best effort: failures are logged and never raise out of the
service.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.storage import Store

from . import macmap
from .const import DATA_DISCOVERY, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.cloud_names"
STORAGE_VERSION = 1
NAMES_CACHE = "cloud_names"
NAMES_STORE = "cloud_names_store"

SERVICE_REFRESH_DEVICES = "refresh_devices"
SERVICE_RELOCATE_DEVICES = "relocate_devices"
SERVICE_LOGOUT = "logout_cloud"


def _store(hass: HomeAssistant) -> Store:
    """Return the persistent store used for the cloud device-name cache."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(NAMES_STORE)
    if store is None:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        domain_data[NAMES_STORE] = store
    return store


async def async_get_names(hass: HomeAssistant) -> dict[str, str]:
    """Return cached {device_id: name}, loading from disk once per session."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(NAMES_CACHE)
    if cached is not None:
        return cached
    names: dict[str, str] = {}
    try:
        stored = await _store(hass).async_load()
        if isinstance(stored, dict):
            names = {str(k): str(v) for k, v in stored.items() if v}
    except Exception as err:  # noqa: BLE001 - cache must never break setup
        _LOGGER.warning("Could not load cached Tuya device names: %s", err)
    domain_data[NAMES_CACHE] = names
    return names


async def async_save_names(hass: HomeAssistant, names: dict[str, str]) -> None:
    """Store a name map that was fetched elsewhere (one cloud call, many uses)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[NAMES_CACHE] = names
    try:
        await _store(hass).async_save(names)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save Tuya device names: %s", err)


async def async_refresh_names(hass: HomeAssistant) -> dict[str, str] | None:
    """Fetch device names from the Tuya cloud, when a login is available."""
    from .cloud import Cloud

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("auth_cache"):
        _LOGGER.info(
            "No saved Tuya cloud login; skipping device name refresh "
            "(use the cloud-assisted setup once to enable it)"
        )
        return None
    try:
        names = await Cloud(hass).async_get_device_names()
    except Exception as err:  # noqa: BLE001 - cloud is optional
        _LOGGER.warning("Could not refresh device names from Tuya cloud: %s", err)
        return None
    domain_data[NAMES_CACHE] = names
    try:
        await _store(hass).async_save(names)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save Tuya device names: %s", err)
    _LOGGER.info("Refreshed %d device names from the Tuya cloud", len(names))
    return names


async def async_relocate_devices(hass: HomeAssistant) -> dict[str, Any]:
    """Update the stored host of any configured device that changed IP.

    Delegates to ``macmap``, where each candidate address is confirmed by MAC
    address before the stored host is rewritten.
    """
    # Manual use: do not let the throttle hold back the handshake probe.
    return await macmap.async_relocate_devices(hass, force_protocol=True)


async def async_clear_names(hass: HomeAssistant) -> None:
    """Drop the cached cloud device names: they belong to the login."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[NAMES_CACHE] = {}
    try:
        await _store(hass).async_remove()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not remove the cached Tuya device names: %s", err)


async def async_handle_logout(call: ServiceCall) -> dict[str, Any]:
    """Forget the Tuya cloud login.

    Device control is local, so every configured device keeps working; what goes
    away is the cloud side: the account that labels discovered devices and
    supplies the local keys offered when adding one.  The next device added
    through the cloud path asks for a login again, which is how you switch
    accounts.
    """
    from .cloud import async_forget_auth, async_restore_auth

    hass = call.hass
    # Make sure the saved login has been loaded before deciding what is there.
    await async_restore_auth(hass)
    had_login = await async_forget_auth(hass)
    await async_clear_names(hass)
    if had_login:
        _LOGGER.warning("Tuya cloud login removed; local device control is unchanged")
    else:
        _LOGGER.info("Tuya cloud logout requested but no login was saved")
    return {"success": True, "was_authenticated": had_login}


async def async_handle_relocate_devices(call: ServiceCall) -> dict[str, Any]:
    """Service handler: relocate devices and report what changed."""
    from .cloud import async_restore_auth

    await async_restore_auth(call.hass)
    return await async_relocate_devices(call.hass)


async def async_handle_refresh_devices(call: ServiceCall) -> dict[str, Any]:
    """Service handler: relocate devices, refresh cloud names, rescan."""
    from .cloud import async_restore_auth

    hass = call.hass

    # Load a saved cloud login from disk first: it may not have been restored
    # yet in this Home Assistant session.
    await async_restore_auth(hass)

    relocated = await async_relocate_devices(hass)
    names = await async_refresh_names(hass)

    domain_data = hass.data.get(DOMAIN, {})
    rediscovery = domain_data.get(DATA_DISCOVERY)
    rescanned = False
    if rediscovery is not None:
        try:
            await rediscovery.async_scan_now()
            rescanned = True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("LAN discovery rescan failed: %s", err)

    result = {
        "relocated": relocated["relocated"],
        "unverified": relocated["unverified"],
        "not_found": relocated["not_found"],
        "cloud_names": len(names) if names else 0,
        "rescanned": rescanned,
    }
    _LOGGER.info("Refresh device list: %s", result)
    return result


async def async_setup_refresh_services(hass: HomeAssistant) -> None:
    """Register the integration level services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_DEVICES):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DEVICES,
        async_handle_refresh_devices,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RELOCATE_DEVICES,
        async_handle_relocate_devices,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOGOUT,
        async_handle_logout,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL,
    )
