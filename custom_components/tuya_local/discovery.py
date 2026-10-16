"""
Active Tuya LAN discovery.

When a router hands out new DHCP leases (e.g. after a reboot) a Tuya device
can change IP. The integration then keeps trying the stale ``host`` stored in
the config entry, the device goes unavailable, and the user has to reconfigure
it by hand.

Tuya devices do not all announce themselves unprompted -- in particular
protocol 3.4/3.5 devices stay silent until they receive a discovery request
broadcast to UDP port 7000, at which point they reply with their id (``gwId``),
current IP and ``productKey``. ``tinytuya``'s scanner sends exactly that request,
so ``tinytuya.find_device``/``tinytuya.deviceScan`` locate devices regardless of
how their IP changed. This is the same mechanism the config flow already uses via
``scan_for_device`` and the one ``localtuya`` uses to find devices in seconds.

This module runs two active-scan tasks:

- a fast sweep (every ``SWEEP_INTERVAL``) that relocates *unreachable* configured
  devices: the current address is looked up and confirmed by MAC address (see
  ``macmap``), the config entry's host is updated and the existing update-listener
  reload reconnects the device on the new address -- no manual reconfiguration, no
  cloud round-trip, history preserved.  Reachable devices are never scanned, so
  there is no traffic while the network is healthy.
- a slower full scan (every ``SCAN_INTERVAL``) that, from a single
  ``deviceScan``: (a) warns, once per device per HA start, when a *configured*
  device reports a ``productKey`` its config file does not list under
  ``products`` (so the config can be improved); and (b) raises an
  ``integration_discovery`` flow for each *unconfigured* device found, so it
  surfaces in Home Assistant for one-click setup (with the built-in ignore).

References:
- tinytuya scanner discovery request (port 7000 for v3.5 devices):
  https://github.com/jasonacox/tinytuya/blob/master/tinytuya/scanner.py
- the integration's own config-flow scan: config_flow.scan_for_device
"""

import asyncio
import logging
from datetime import timedelta

import tinytuya
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DEVICE_CID, CONF_DEVICE_ID, CONF_TYPE, DATA_DISCOVERY, DOMAIN
from .helpers.device_config import get_config
from .macmap import async_relocate_devices

_LOGGER = logging.getLogger(__name__)

# How often to look for unreachable devices. Reachable devices are skipped, so
# a healthy system generates no scan traffic; an unreachable device is normally
# relocated on the first sweep after it drops.
SWEEP_INTERVAL = timedelta(seconds=60)

# How often to run the full network scan (product-id check + new-device
# discovery). Infrequent, since neither action is time critical.
SCAN_INTERVAL = timedelta(minutes=10)

# Delay before the extra scan that runs shortly after startup, so devices show
# up under "Discovered" without waiting a full SCAN_INTERVAL after a restart.
STARTUP_SCAN_DELAY = 45


def _scan_all():
    """Scan the LAN for all Tuya devices (blocking; run in executor).

    Returns tinytuya's dict keyed by IP, each value carrying ``gwId``,
    ``productKey`` and ``version``; an empty dict on any socket error.
    """
    try:
        return tinytuya.deviceScan(verbose=False, poll=False, maxretry=2)
    except OSError:
        return {}


class TuyaLANRediscovery:
    """Active LAN discovery for Tuya devices."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._unsub_sweep = None
        self._unsub_scan = None
        self._scanning = False
        # device ids already warned about an unmatched product id this run.
        self._warned_products = set()
        # gwIds an integration_discovery flow has already been raised for.
        self._discovered = set()
        # {device_id: app name} from the cloud, when available.
        self._names: dict[str, str] = {}

    @callback
    def async_start(self) -> None:
        """Begin periodic discovery tasks."""
        # The sweep is re-enabled (upstream disabled it in #5713 because an
        # unreachable device could be moved to a wrong address): the relocation
        # is now confirmed against the device's MAC and device id before the
        # stored host is rewritten, so a device that is merely unreachable is
        # left where it is.
        if self._unsub_sweep is None:
            self._unsub_sweep = async_track_time_interval(
                self._hass, self._async_sweep, SWEEP_INTERVAL
            )
        if self._unsub_scan is None:
            self._unsub_scan = async_track_time_interval(
                self._hass, self._async_discovery_scan, SCAN_INTERVAL
            )
        # One extra scan shortly after startup: a Home Assistant restart clears
        # in-progress config flows, so without this the Discovered cards would
        # not come back until the first periodic scan, up to SCAN_INTERVAL later.
        self._hass.async_create_task(self._async_startup_scan())

    async def _async_startup_scan(self) -> None:
        """Run one discovery scan shortly after startup."""
        await asyncio.sleep(STARTUP_SCAN_DELAY)
        try:
            await self._async_discovery_scan()
        except Exception as err:  # noqa: BLE001 - never break setup
            _LOGGER.warning("Startup discovery scan failed: %s", err)

    @callback
    def async_stop(self, event=None) -> None:
        """Stop periodic discovery tasks."""
        for attr in ("_unsub_sweep", "_unsub_scan"):
            unsub = getattr(self, attr)
            if unsub is not None:
                unsub()
                setattr(self, attr, None)

    async def _async_sweep(self, now=None) -> None:
        """Relocate any unreachable device onto its current address.

        Only devices that stopped returning state are looked at, so a healthy
        network costs one ARP table read per sweep and no scan traffic at all.
        """
        if self._scanning:
            return
        self._scanning = True
        try:
            result = await async_relocate_devices(self._hass)
            if result.get("relocated"):
                _LOGGER.info("IP relocation sweep: %s", result["relocated"])
        except Exception as err:  # noqa: BLE001 - a sweep must never break setup
            _LOGGER.warning("IP relocation sweep failed: %s", err)
        finally:
            self._scanning = False

    async def _async_discovery_scan(self, now=None) -> None:
        """Full LAN scan: product-id check for known devices, discover new ones."""
        if self._scanning:
            return
        self._scanning = True
        try:
            from .refresh import async_get_names

            self._names = await async_get_names(self._hass)
            found = await self._hass.async_add_executor_job(_scan_all)
            if not found:
                return

            by_gwid = {}
            for info in found.values():
                gwid = info.get("gwId")
                if gwid:
                    by_gwid[gwid] = info

            # A device that is still only "discovered" (has an in-progress
            # discovery flow) must not block the user from adding it by hand:
            # HA raises already_in_progress for a second flow with the same
            # unique id, which is confusing.  Duplicate *entries* are still
            # prevented by _abort_if_unique_id_configured().
            configured = {}
            for entry in self._hass.config_entries.async_entries(DOMAIN):
                device_id = entry.data.get(CONF_DEVICE_ID)
                if device_id:
                    configured[device_id] = entry

            # product id -> a config type already in use for that product, so a
            # new device of the same product can be labelled like its siblings
            # (the product id alone can match several device configs).
            product_types = {}
            for gwid, info in by_gwid.items():
                entry = configured.get(gwid)
                product_id = info.get("productKey")
                config_type = entry.data.get(CONF_TYPE) if entry else None
                if product_id and config_type:
                    product_types.setdefault(product_id, config_type)

            for gwid, info in by_gwid.items():
                entry = configured.get(gwid)
                if entry is not None:
                    # Skip sub-devices for now, the WiFi reported product id is for the hub
                    if not entry.data.get(CONF_DEVICE_CID):
                        await self._check_product(entry, info.get("productKey"))
                else:
                    self._discover_new(
                        gwid, info, product_types.get(info.get("productKey"))
                    )
        finally:
            self._scanning = False

    async def _check_product(self, entry, product_id) -> None:
        """Warn once per run when a configured device's product id is unlisted."""
        device_id = entry.data.get(CONF_DEVICE_ID)
        config_type = entry.data.get(CONF_TYPE)
        if not product_id or not config_type or device_id in self._warned_products:
            return
        config = await self._hass.async_add_executor_job(get_config, config_type)
        if config is None or config.matches_product(product_id):
            return
        # WARNING so it is visible under HA's default log level; once per device
        # per run to avoid noise.
        self._warned_products.add(device_id)
        _LOGGER.warning(
            "%s: device product id %s is not listed in its config (%s); "
            "if your device is an exact match for the config please report Product Details it so support can be improved",
            entry.title,
            product_id,
            config_type,
        )

    def _flow_in_progress(self, device_id) -> bool:
        """Whether a config flow for this device id is already in progress.

        Re-raising a discovery flow for a device that already has one makes HA
        abort with ``already_in_progress``, which surfaces as a confusing error
        in the UI, so check first.
        """
        for flow in self._hass.config_entries.flow.async_progress():
            if flow.get("handler") != DOMAIN:
                continue
            if flow.get("context", {}).get("unique_id") == device_id:
                return True
        return False

    @callback
    def _discover_new(self, gwid, info, preferred_type=None) -> None:
        """Raise an integration_discovery flow for a not-yet-configured device."""
        if gwid in self._discovered:
            return
        if self._flow_in_progress(gwid):
            return
        self._discovered.add(gwid)
        self._hass.async_create_task(
            self._hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_DEVICE_ID: gwid,
                    CONF_HOST: info.get("ip"),
                    "product_id": info.get("productKey"),
                    "version": info.get("version"),
                    # The name set in the Tuya/SmartLife app, when we have it.
                    "name": self._names.get(gwid),
                    # Config type already used for the same product id, if any.
                    "preferred_type": preferred_type,
                },
            )
        )

    async def async_scan_now(self) -> None:
        """Run the full discovery scan immediately (on-demand refresh)."""
        self._discovered.clear()
        await self._async_discovery_scan()


async def async_start_discovery(hass: HomeAssistant) -> None:
    """Start the shared LAN discovery service if not already running."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_DISCOVERY) is not None:
        return

    rediscovery = TuyaLANRediscovery(hass)
    domain_data[DATA_DISCOVERY] = rediscovery
    rediscovery.async_start()


@callback
def async_stop_discovery(hass: HomeAssistant) -> None:
    """Stop the shared LAN discovery service if running."""
    domain_data = hass.data.get(DOMAIN, {})
    rediscovery = domain_data.pop(DATA_DISCOVERY, None)
    if rediscovery is not None:
        rediscovery.async_stop()
