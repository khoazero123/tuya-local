"""Add every device of a Tuya cloud account in one go.

The cloud login already returns the whole device list, including each device's
local key, so there is no reason to pick devices one at a time after signing in.
This module plans the whole list - what can be added, what cannot and why - and
then creates the config entries in one pass.

A device is skipped, with a reason, when it is already configured, is a hub (its
sub-devices are reached through it), has no local key, is marked cloud-only by
the cloud, has no known LAN address, or when no device config lists its product
id (without a config the entity set cannot be derived, and guessing one would
create an entry that never loads).

The plan is cheap: one LAN scan for the addresses the cloud did not provide, one
pass over the device config files to map product ids to config types, and a TCP
check on the Tuya port so the report can say which devices are answering now.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_ADD_ALL = "add_all_devices"

SKIP_ALREADY = "already_added"
SKIP_HUB = "hub"
SKIP_NO_KEY = "no_local_key"
SKIP_CLOUD_ONLY = "cloud_only"
SKIP_NO_HOST = "no_host"
SKIP_NO_CONFIG = "no_config"
SKIP_UNREACHABLE = "unreachable"
SKIP_TYPE_UNKNOWN = "type_unknown"

ADD_ALL_SCHEMA = vol.Schema(
    {
        vol.Optional("dry_run", default=True): bool,
        vol.Optional("include_unreachable", default=True): bool,
    }
)


def _is_lan_ip(value: str) -> bool:
    """Whether an address from the cloud may be a LAN address at all.

    The cloud reports the address the device was last seen from, which for a
    domestic account is the router's **WAN** address, not the device's LAN
    address.  Using it would point an entry at an arbitrary host on the internet,
    so only private addresses are considered - the LAN scan is the real source.
    """
    import ipaddress

    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback and not address.is_link_local


async def _cloud_devices(hass: HomeAssistant) -> dict[str, Any]:
    """Return the device list of the connected Tuya cloud account."""
    from .cloud import Cloud

    cloud = Cloud(hass)
    if not cloud.is_authenticated:
        _LOGGER.info("No Tuya cloud login; nothing to add automatically")
        return {}
    try:
        return await cloud.async_get_devices()
    except Exception as err:  # noqa: BLE001 - the cloud is optional at runtime
        _LOGGER.warning("Could not read the Tuya cloud device list: %s", err)
        return {}


def _lan_addresses() -> dict[str, str]:
    """One LAN scan: ``{device_id: ip}`` for the Tuya devices that answer."""
    import tinytuya

    try:
        found = tinytuya.deviceScan(verbose=False, poll=False, maxretry=2)
    except Exception as err:  # noqa: BLE001 - a scan failure just means no addresses
        _LOGGER.debug("LAN scan failed: %s", err)
        return {}
    return {
        info["gwId"]: info["ip"]
        for info in found.values()
        if info.get("gwId") and info.get("ip")
    }


def _probe_hosts(hosts: list[str]) -> dict[str, bool]:
    """TCP check on the Tuya port, concurrently (report only)."""
    from .macmap import probe

    if not hosts:
        return {}

    def _safe(host: str) -> bool:
        try:
            return probe(host)
        except Exception as err:  # noqa: BLE001 - a probe failure is "no answer"
            _LOGGER.debug("Probe of %s failed: %s", host, err)
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        return dict(zip(hosts, pool.map(_safe, hosts), strict=False))


def _type_index(product_ids: set[str]) -> dict[str, list[str]]:
    """``{product_id: [config types listing it]}`` in a single pass."""
    from .helpers.device_config import TuyaDeviceConfig, available_configs

    index: dict[str, list[str]] = {pid: [] for pid in product_ids if pid}
    if not index:
        return index
    for fname in available_configs():
        try:
            config = TuyaDeviceConfig(fname)
        except TypeError:
            _LOGGER.debug("Parse error in %s", fname)
            continue
        for product_id in list(index):
            try:
                if config.matches_product(product_id):
                    index[product_id].append(config.config_type)
            except Exception as err:  # noqa: BLE001 - one bad config, not the plan
                _LOGGER.debug("Could not check %s for %s: %s", fname, product_id, err)
                continue
    return index


async def async_plan_devices(
    hass: HomeAssistant, *, include_unreachable: bool = True
) -> dict[str, Any]:
    """Work out what can be added from the connected account, and what cannot."""
    devices = await _cloud_devices(hass)
    configured = {
        entry.data.get(CONF_DEVICE_ID)
        for entry in hass.config_entries.async_entries(DOMAIN)
    }
    lan = await hass.async_add_executor_job(_lan_addresses) if devices else {}

    candidates: list[dict[str, Any]] = []
    for device in devices.values():
        device_id = device.get("id")
        if not device_id:
            continue
        cloud_ip = device.get("ip") or ""
        item: dict[str, Any] = {
            "device_id": device_id,
            "name": device.get("name") or f"Tuya {device_id[-6:]}",
            "product_id": device.get("product_id") or "",
            "local_key": device.get(CONF_LOCAL_KEY) or "",
            "protocol_version": device.get("version") or "auto",
            "host": lan.get(device_id) or (cloud_ip if _is_lan_ip(cloud_ip) else ""),
            "action": "add",
            "reason": None,
        }
        if device_id in configured:
            item.update(action="skip", reason=SKIP_ALREADY)
        elif device.get("is_hub"):
            item.update(action="skip", reason=SKIP_HUB)
        elif not item["local_key"]:
            item.update(action="skip", reason=SKIP_NO_KEY)
        elif device.get("support_local") is False:
            item.update(action="skip", reason=SKIP_CLOUD_ONLY)
        elif not item["host"]:
            item.update(action="skip", reason=SKIP_NO_HOST)
        candidates.append(item)

    products = {i["product_id"] for i in candidates if i["action"] == "add"}
    types = await hass.async_add_executor_job(_type_index, products) if products else {}
    for item in candidates:
        if item["action"] != "add":
            continue
        matches = sorted(types.get(item["product_id"], []))
        if not matches:
            # The product id is listed nowhere; the type can still be inferred by
            # asking the device for its DPs (the path the mobile app uses), which
            # only works while it answers.
            item["needs_probe"] = True
            continue
        # Several configs can list one product; take the first and report the
        # rest so a wrong pick is visible in the plan (and fixable per entry).
        item["type"] = matches[0]
        if len(matches) > 1:
            item["alternatives"] = matches[1:]

    hosts = [i["host"] for i in candidates if i["action"] == "add"]
    reachable = await hass.async_add_executor_job(_probe_hosts, hosts) if hosts else {}
    for item in candidates:
        if item["action"] != "add":
            continue
        item["reachable"] = reachable.get(item["host"], False)
        if not item["reachable"] and not include_unreachable:
            item.update(action="skip", reason=SKIP_UNREACHABLE)
        elif not item["reachable"] and item.get("needs_probe"):
            # No config to take the type from, and no device to ask.
            item.update(action="skip", reason=SKIP_TYPE_UNKNOWN)

    plan_add = [i for i in candidates if i["action"] == "add"]
    plan_skip = [i for i in candidates if i["action"] != "add"]
    _LOGGER.info(
        "Tuya cloud account: %d device(s), %d to add, %d skipped",
        len(candidates),
        len(plan_add),
        len(plan_skip),
    )
    return {
        "found": len(candidates),
        "add": plan_add,
        "skip": plan_skip,
        "devices": candidates,
    }


async def _infer_type(hass: HomeAssistant, item: dict[str, Any]) -> str | None:
    """Ask the device which config fits it (DP-based, like the mobile app)."""
    from .config_flow import async_test_connection

    config = {
        CONF_DEVICE_ID: item["device_id"],
        CONF_HOST: item["host"],
        CONF_LOCAL_KEY: item["local_key"],
        CONF_PROTOCOL_VERSION: item.get("protocol_version") or "auto",
        CONF_POLL_ONLY: False,
        CONF_NAME: item["name"],
    }
    device = await async_test_connection(config, hass)
    if device is None:
        return None
    try:
        if item.get("product_id"):
            try:
                device.set_detected_product_id(item["product_id"])
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("No product hint for %s: %s", item["name"], err)
        device_type = await device.async_inferred_type()
        protocol = getattr(device, "_protocol_configured", None)
        if protocol and protocol != "auto":
            item["protocol_version"] = protocol
        return device_type
    finally:
        try:
            device._api.set_socketPersistent(False)
            if device._api.parent:
                device._api.parent.set_socketPersistent(False)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not release the test socket: %s", err)


async def async_add_planned(
    hass: HomeAssistant, plan: dict[str, Any]
) -> dict[str, Any]:
    """Create the planned config entries (the same path the mobile app uses)."""
    added: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in plan.get("add", []):
        if not item.get("type"):
            if not item.get("reachable"):
                failed.append({**item, "error": SKIP_TYPE_UNKNOWN})
                continue
            item["type"] = await _infer_type(hass, item)
            if not item["type"]:
                failed.append({**item, "error": SKIP_TYPE_UNKNOWN})
                continue
        data = {
            CONF_DEVICE_ID: item["device_id"],
            CONF_HOST: item["host"],
            CONF_LOCAL_KEY: item["local_key"],
            CONF_PROTOCOL_VERSION: item["protocol_version"],
            CONF_POLL_ONLY: False,
            CONF_TYPE: item["type"],
            CONF_NAME: item["name"],
        }
        try:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "mobile"}, data=data
            )
        except Exception as err:  # noqa: BLE001 - report, do not abort the batch
            _LOGGER.warning("Could not add %s: %s", item["name"], err)
            failed.append({**item, "error": str(err)})
            continue
        if result.get("type") != "create_entry":
            _LOGGER.warning(
                "Could not add %s: flow returned %s", item["name"], result.get("type")
            )
            failed.append({**item, "error": f"flow_{result.get('type')}"})
            continue
        _LOGGER.info(
            "Added %s (%s) as %s at %s",
            item["name"],
            item["device_id"],
            item["type"],
            item["host"],
        )
        added.append({**item, "entry_id": result.get("result")})

    return {"added": added, "failed": failed}


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Counts plus a skip breakdown, for a service response or a log line."""
    reasons: dict[str, int] = {}
    for item in plan["skip"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {
        "found": plan["found"],
        "to_add": len(plan["add"]),
        "needs_probe": sum(1 for i in plan["add"] if not i.get("type")),
        "skipped": len(plan["skip"]),
        "skip_reasons": reasons,
    }


def _public_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan as returned to a caller: never the local keys."""
    return [
        {key: value for key, value in item.items() if key != "local_key"}
        for item in plan["devices"]
    ]


async def async_handle_add_all(call: ServiceCall) -> dict[str, Any]:
    """Service: plan, and unless ``dry_run``, add every addable device."""
    hass = call.hass
    dry_run = call.data.get("dry_run", True)
    plan = await async_plan_devices(
        hass, include_unreachable=call.data.get("include_unreachable", True)
    )
    summary = _summary(plan)
    if dry_run:
        return {**summary, "dry_run": True, "devices": _public_plan(plan)}

    result = await async_add_planned(hass, plan)
    return {
        **summary,
        "dry_run": False,
        "added": len(result["added"]),
        "failed": result["failed"],
        "added_devices": [
            {key: value for key, value in item.items() if key != "local_key"}
            for item in result["added"]
        ],
    }


async def async_setup_bulk_services(hass: HomeAssistant) -> None:
    """Register the bulk services (once)."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_ALL):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_ALL,
        async_handle_add_all,
        schema=ADD_ALL_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
