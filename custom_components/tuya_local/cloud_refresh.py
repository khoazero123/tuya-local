"""Background refresh of the Tuya cloud device list.

The per device cloud diagnostics (product id, category, cloud name, cloud online)
read the account's device list out of the cache in ``bulk_add``.  Nothing in the
entity path ever calls the cloud: the list is refreshed here, in the background, on
an interval the user controls with the "Cloud refresh interval" number entity.

The same fetched list also feeds the device name cache, so one cloud call serves
both (a fetch costs ~20 s on a far away endpoint).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import bulk_add
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.cloud_refresh"
STORAGE_VERSION = 1

INTERVAL_CACHE = "cloud_refresh_interval"
INTERVAL_STORE = "cloud_refresh_store"
REFRESH_TASK = "cloud_refresh_task"

DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL_MINUTES = 0
MAX_INTERVAL_MINUTES = 1440


def _store(hass: HomeAssistant) -> Store:
    """Return the store holding the refresh interval."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(INTERVAL_STORE)
    if store is None:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        domain_data[INTERVAL_STORE] = store
    return store


async def async_get_interval(hass: HomeAssistant) -> int:
    """Return the configured refresh interval in minutes (0 disables it)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(INTERVAL_CACHE)
    if cached is not None:
        return cached
    interval = DEFAULT_INTERVAL_MINUTES
    try:
        stored = await _store(hass).async_load()
        if isinstance(stored, dict):
            value = stored.get("interval_minutes")
            if isinstance(value, (int, float)):
                interval = int(value)
    except Exception as err:  # noqa: BLE001 - a bad store must not break setup
        _LOGGER.warning("Could not load the cloud refresh interval: %s", err)
    interval = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval))
    domain_data[INTERVAL_CACHE] = interval
    return interval


async def async_set_interval(hass: HomeAssistant, minutes: int) -> int:
    """Store a new interval and restart the background refresh."""
    minutes = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(minutes)))
    hass.data.setdefault(DOMAIN, {})[INTERVAL_CACHE] = minutes
    try:
        await _store(hass).async_save({"interval_minutes": minutes})
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save the cloud refresh interval: %s", err)
    await async_stop_cloud_refresh(hass)
    if minutes > 0:
        await async_start_cloud_refresh(hass)
    return minutes


async def async_refresh_cloud(hass: HomeAssistant) -> dict:
    """Fetch the account device list once and refresh the names from it."""
    devices = await bulk_add.async_cached_devices(hass, force=True)
    if devices:
        names = {
            key.split("/")[0] if "/" in key else key: value.get("name")
            for key, value in devices.items()
            if value.get("name")
        }
        if names:
            from .refresh import async_save_names

            await async_save_names(hass, names)
        _LOGGER.debug("Refreshed %d devices from the Tuya cloud", len(devices))
    return devices


async def _async_worker(hass: HomeAssistant) -> None:
    """Refresh the device list every interval, forever, until cancelled."""
    while True:
        minutes = await async_get_interval(hass)
        if minutes <= 0:
            return
        await asyncio.sleep(minutes * 60)
        try:
            await async_refresh_cloud(hass)
        except Exception as err:  # noqa: BLE001 - background work must not die
            _LOGGER.warning("Scheduled Tuya cloud refresh failed: %s", err)


async def async_start_cloud_refresh(hass: HomeAssistant) -> None:
    """Start the background refresh if it is enabled and not already running."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(REFRESH_TASK) is not None:
        return
    if await async_get_interval(hass) <= 0:
        return
    domain_data[REFRESH_TASK] = hass.async_create_background_task(
        _async_worker(hass), name="tuya_local cloud refresh", eager_start=True
    )


async def async_stop_cloud_refresh(hass: HomeAssistant) -> None:
    """Cancel the background refresh, if it is running."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    task = domain_data.pop(REFRESH_TASK, None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Cloud refresh task ended with: %s", err)
