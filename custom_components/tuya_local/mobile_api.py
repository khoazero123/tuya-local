"""Mobile app services for pairing hand-off from the Smarthome Flutter app.

Services:
- ``tuya_local.add_device`` — create/update a config entry from App SDK local_key
- ``tuya_local.list_configured`` — list configured device ids/hosts
- ``tuya_local.sync_cloud_session`` — store optional App/OpenAPI session for later refresh
"""

from __future__ import annotations

import logging
from typing import Any

import tinytuya
import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .cloud import async_save_auth
from .config_flow import async_test_connection, scan_for_device
from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_SERVICE_ENTRY,
    CONF_TYPE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_ADD_DEVICE = "add_device"
SERVICE_LIST_CONFIGURED = "list_configured"
SERVICE_SYNC_CLOUD_SESSION = "sync_cloud_session"

APP_SESSION_CACHE = "app_cloud_session"
APP_SESSION_STORE = "app_cloud_session_store"
APP_SESSION_STORAGE_KEY = f"{DOMAIN}.app_cloud_session"
APP_SESSION_STORAGE_VERSION = 1

ADD_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("local_key"): cv.string,
        vol.Optional("host"): cv.string,
        vol.Optional("product_id"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("protocol_version", default="auto"): vol.Any(
            "auto", float, int, cv.string
        ),
        vol.Optional("poll_only", default=False): cv.boolean,
    }
)

SYNC_SESSION_SCHEMA = vol.Schema(
    {
        vol.Required("payload"): dict,
    }
)


def _app_session_store(hass: HomeAssistant) -> Store:
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(APP_SESSION_STORE)
    if store is None:
        store = Store(
            hass, APP_SESSION_STORAGE_VERSION, APP_SESSION_STORAGE_KEY, private=True
        )
        domain_data[APP_SESSION_STORE] = store
    return store


def _find_entry_by_device_id(hass: HomeAssistant, device_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_DEVICE_ID) == device_id:
            return entry
    return None


async def _resolve_host(
    hass: HomeAssistant, device_id: str, host: str | None
) -> str | None:
    if host:
        return host
    try:
        found = await hass.async_add_executor_job(scan_for_device, device_id)
        if isinstance(found, dict):
            return found.get("ip")
        if isinstance(found, str):
            return found
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("find_device failed for %s: %s", device_id, err)

    def _scan() -> str | None:
        try:
            devices = tinytuya.deviceScan(verbose=False, poll=False, maxretry=2)
        except OSError:
            return None
        for info in devices.values():
            if info.get("gwId") == device_id:
                return info.get("ip")
        return None

    return await hass.async_add_executor_job(_scan)


async def async_handle_add_device(call: ServiceCall) -> dict[str, Any]:
    """Create or update a tuya_local config entry from mobile pairing data."""
    hass = call.hass
    device_id = call.data["device_id"].strip()
    local_key = call.data["local_key"].strip()
    product_id = call.data.get("product_id")
    name = call.data.get("name") or f"Tuya {device_id[-6:]}"
    protocol = call.data.get("protocol_version", "auto")
    poll_only = call.data.get("poll_only", False)

    if protocol not in (None, "auto"):
        try:
            protocol = float(protocol)
        except (TypeError, ValueError):  # fmt: skip
            protocol = "auto"

    host = await _resolve_host(hass, device_id, call.data.get("host"))
    if not host:
        _LOGGER.error(
            "add_device: could not resolve host for %s; pass host from the app",
            device_id,
        )
        return {
            "success": False,
            "error": "host_not_found",
            "device_id": device_id,
        }

    base_config = {
        CONF_DEVICE_ID: device_id,
        CONF_HOST: host,
        CONF_LOCAL_KEY: local_key,
        CONF_PROTOCOL_VERSION: protocol,
        CONF_POLL_ONLY: poll_only,
        CONF_NAME: name,
    }

    device = await async_test_connection(base_config, hass)
    device_type = None
    if device is None:
        _LOGGER.warning(
            "add_device: connection test failed for %s @ %s — still creating entry",
            device_id,
            host,
        )
        saved_protocol = protocol if protocol != "auto" else 3.3
    else:
        if product_id:
            try:
                device.set_detected_product_id(product_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "add_device: set_detected_product_id failed for %s: %s",
                    product_id,
                    err,
                )
        device_type = await device.async_inferred_type()
        # Prefer the protocol the test actually used
        saved_protocol = getattr(device, "_protocol_configured", protocol)
        if saved_protocol == "auto":
            saved_protocol = 3.3
        # Clean up test sockets
        try:
            device._api.set_socketPersistent(False)
            if device._api.parent:
                device._api.parent.set_socketPersistent(False)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("add_device: could not release the test socket: %s", err)

    if not device_type and product_id:
        from .helpers.device_config import possible_matches

        matches = await hass.async_add_executor_job(possible_matches, {}, [product_id])
        if matches:
            device_type = matches[0].config_type

    if not device_type:
        # Last-resort generic single-switch profile so the entry can load.
        device_type = "aubess_1gang_switch"

    entry_data = {
        CONF_DEVICE_ID: device_id,
        CONF_HOST: host,
        CONF_LOCAL_KEY: local_key,
        CONF_PROTOCOL_VERSION: saved_protocol,
        CONF_POLL_ONLY: poll_only,
        CONF_TYPE: device_type,
    }

    existing = _find_entry_by_device_id(hass, device_id)
    if existing:
        hass.config_entries.async_update_entry(
            existing,
            title=name,
            data={**existing.data, **entry_data},
        )
        await hass.config_entries.async_reload(existing.entry_id)
        _LOGGER.info("add_device: updated %s (%s)", name, device_id)
        return {
            "success": True,
            "updated": True,
            "entry_id": existing.entry_id,
            "device_id": device_id,
            "host": host,
            "type": device_type,
        }

    # Programmatic config flow (source=mobile → async_step_mobile)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "mobile"},
        data={
            **entry_data,
            CONF_NAME: name,
            "product_id": product_id,
        },
    )
    if result.get("type") != "create_entry":
        _LOGGER.error("add_device: flow failed: %s", result)
        return {
            "success": False,
            "error": "flow_failed",
            "flow_result": str(result.get("type")),
            "device_id": device_id,
        }

    _LOGGER.info("add_device: created %s (%s) type=%s", name, device_id, device_type)
    return {
        "success": True,
        "updated": False,
        "entry_id": result.get("result"),
        "device_id": device_id,
        "host": host,
        "type": device_type,
    }


async def async_handle_list_configured(call: ServiceCall) -> dict[str, Any]:
    """Return configured tuya_local devices for the mobile sync UI."""
    hass = call.hass
    devices = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_SERVICE_ENTRY):
            continue
        data = {**entry.data, **entry.options}
        devices.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "device_id": data.get(CONF_DEVICE_ID),
                "host": data.get(CONF_HOST),
                "type": data.get(CONF_TYPE),
                "protocol_version": data.get(CONF_PROTOCOL_VERSION),
            }
        )
    return {"devices": devices, "count": len(devices)}


async def async_handle_sync_cloud_session(call: ServiceCall) -> dict[str, Any]:
    """Persist an optional cloud session pushed by the mobile app.

    Payload may be either a legacy tuya_sharing auth dict (compatible with
    ``auth_cache``) or an App/OpenAPI credential set for future refresh.
    """
    hass = call.hass
    payload = dict(call.data["payload"])
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[APP_SESSION_CACHE] = payload

    # If the payload looks like the existing sharing auth cache, reuse it.
    if "token_info" in payload and "endpoint" in payload:
        async_save_auth(hass, payload)
        _LOGGER.info("sync_cloud_session: stored sharing-compatible auth cache")
    else:
        try:
            await _app_session_store(hass).async_save(payload)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not persist app cloud session: %s", err)
            return {"success": False, "error": str(err)}
        _LOGGER.info("sync_cloud_session: stored app OpenAPI session")

    return {"success": True, "keys": list(payload.keys())}


async def async_setup_mobile_services(hass: HomeAssistant) -> None:
    """Register mobile hand-off services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_DEVICE):
        return

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_DEVICE,
        async_handle_add_device,
        schema=ADD_DEVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_CONFIGURED,
        async_handle_list_configured,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_CLOUD_SESSION,
        async_handle_sync_cloud_session,
        schema=SYNC_SESSION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
