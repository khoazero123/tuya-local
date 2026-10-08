"""On-demand refresh helpers for the Tuya Local integration.

Backs the ``tuya_local.refresh_devices`` service and the matching
"Refresh device list" button.  Three jobs:

1. re-locate configured devices on the LAN by device id and update the stored
   host when a DHCP change moved them (the entry reloads itself),
2. refresh the device names from the Tuya cloud, so discovered devices can be
   labelled with the name set in the Tuya/SmartLife app,
3. re-run the LAN discovery scan so newly seen devices surface again.

Everything here is best effort: failures are logged and never raise out of the
service.
"""

from __future__ import annotations

import logging
from typing import Any

import tinytuya
import voluptuous as vol
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICE_ID, DATA_DISCOVERY, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.cloud_names"
STORAGE_VERSION = 1
NAMES_CACHE = "cloud_names"
NAMES_STORE = "cloud_names_store"

SERVICE_REFRESH_DEVICES = "refresh_devices"


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


def _scan_lan() -> dict[str, str]:
    """Blocking LAN scan; returns {device_id: ip}."""
    try:
        found = tinytuya.deviceScan(verbose=False, poll=False, maxretry=2)
    except OSError as err:
        _LOGGER.warning("LAN scan failed: %s", err)
        return {}
    result: dict[str, str] = {}
    for info in found.values():
        gwid = info.get("gwId")
        ip = info.get("ip")
        if gwid and ip:
            result[gwid] = ip
    return result


async def async_relocate_devices(hass: HomeAssistant) -> dict[str, Any]:
    """Update the stored host of any configured device that changed IP."""
    found = await hass.async_add_executor_job(_scan_lan)
    moved: list[str] = []
    unchanged: list[str] = []
    not_found: list[str] = []

    for entry in hass.config_entries.async_entries(DOMAIN):
        device_id = entry.data.get(CONF_DEVICE_ID)
        if not device_id:
            continue
        ip = found.get(device_id)
        current = {**entry.data, **entry.options}.get(CONF_HOST)
        if not ip:
            not_found.append(entry.title)
            continue
        if ip == current:
            unchanged.append(entry.title)
            continue
        _LOGGER.warning(
            "%s: LAN IP changed to %s (was %s); updating configuration",
            entry.title,
            ip,
            current,
        )
        new_options = entry.options
        if CONF_HOST in entry.options:
            new_options = {**entry.options, CONF_HOST: ip}
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_HOST: ip},
            options=new_options,
        )
        moved.append(f"{entry.title}: {current} -> {ip}")

    return {"moved": moved, "unchanged": unchanged, "not_found": not_found}


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
        "relocated": relocated["moved"],
        "unchanged": len(relocated["unchanged"]),
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
