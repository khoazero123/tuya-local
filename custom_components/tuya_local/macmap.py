"""MAC <-> IP mapping for the Tuya Local integration.

A configured device is stored by IP address (``CONF_HOST``).  When DHCP hands a
device a different address -- typically after a router reboot -- the stored host
goes stale: the integration keeps dialling an address nothing answers on, the
entry lands in ``setup_retry`` and the user has to fix the address by hand.

This module keeps the **MAC address** of every configured device and uses it to
put the device back on its new address automatically.

Why MAC:

- the L2 address survives a DHCP change, so it identifies the device rather than
  the lease,
- it is visible in the local ARP table, which the Home Assistant host maintains
  on its own, so no cloud round trip and no dependence on the device answering
  anything besides ordinary traffic,
- a subnet sweep for the Tuya TCP port plus the ARP table yields the complete
  ``{mac: ip}`` map of the network, so a device can be found again *without*
  knowing its old address -- the one thing the Tuya discovery broadcast cannot
  do when a device ignores the broadcast.

Every candidate address is confirmed before the stored host is rewritten:

- the device id from the Tuya broadcast scan, and
- the MAC address in the ARP table,

must agree with what is on record for that entry, and the candidate must not be
an address another configured device is answering on.  A device that is simply
powered off therefore produces no match and is left alone, instead of being
moved onto whatever address the scan happened to answer from.

Everything here is best effort: on a Home Assistant install where the ARP table
is not visible (for example the container is not on the host network) the MAC
half disables itself with one log line and the integration keeps working as
before.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import socket
import subprocess  # noqa: S404 - fixed argv, no shell
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.device_macs"
STORAGE_VERSION = 1
MACS_CACHE = "device_macs"
MACS_STORE = "device_macs_store"

# The Tuya local protocol listens on this port; it is also the one port that
# every Tuya LAN device answers on, which makes it a cheap way to enumerate
# them across a subnet.
TUYA_PORT = 6668

# Caps so a misconfigured network cannot turn a sweep into a port scan of
# thousands of addresses.
MAX_HOSTS = 1024
PROBE_TIMEOUT = 0.4
PROBE_WORKERS = 128

# Identity probe: talk the Tuya protocol to the devices answering on the LAN and
# see which id answers. Needs neither a MAC record nor a discovery broadcast,
# but it is the slowest check, so it runs at most this often per Home Assistant.
PROTOCOL_PROBE_TIMEOUT = 2.0
PROTOCOL_SCAN_INTERVAL = 600
PROTOCOL_SCAN_AT = "macmap_last_protocol_scan"

# Home Assistant's own container networks; never a place where a Tuya device
# lives, and sweeping them only wastes time.
_INTERNAL_NETWORKS = ("172.17.0.0/16", "172.30.0.0/16", "172.31.0.0/16")

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_ARP_FAILED = "00:00:00:00:00:00"

# The kernel's neighbour table.  Overridable so the parsing can be tested.
ARP_FILE = "/proc/net/arp"  # noqa: S105 - a path, not a secret


def _normalise_mac(mac: str | None) -> str | None:
    """Return a lower case ``aa:bb:cc:dd:ee:ff`` MAC, or None."""
    if not mac:
        return None
    mac = mac.strip().lower().replace("-", ":")
    if _MAC_RE.match(mac) and mac != _ARP_FAILED:
        return mac
    return None


async def async_arp_table(hass: HomeAssistant) -> dict[str, str]:
    """Return the local ``{ip: mac}`` view of the LAN (blocking work off loop)."""
    return await hass.async_add_executor_job(read_arp_table)


def read_arp_table() -> dict[str, str]:
    """Read ``{ip: mac}`` from the kernel, then fall back to ``ip neigh``.

    ``/proc/net/arp`` is the kernel's own table and is what the host network
    namespace exposes to a container running on it.  Where it is missing (a
    container on a bridge network, a non-Linux host) the neighbour table is
    tried instead.
    """
    table: dict[str, str] = {}

    try:
        with open(ARP_FILE, encoding="utf-8") as handle:  # noqa: PTH123
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) < 4:
                    continue
                ip, mac = fields[0], _normalise_mac(fields[3])
                if mac:
                    table[ip] = mac
    except OSError:
        pass

    if table:
        return table

    if shutil.which("ip"):
        try:
            out = subprocess.run(  # noqa: S603
                ["ip", "-4", "neigh", "show"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            for line in out.splitlines():
                fields = line.split()
                if "lladdr" not in fields or len(fields) < 5:
                    continue
                index = fields.index("lladdr")
                mac = _normalise_mac(fields[index + 1])
                if mac:
                    table[fields[0]] = mac
        except (OSError, subprocess.SubprocessError):  # fmt: skip
            pass

    return table


def arp_available() -> bool:
    """Whether this install can see the LAN ARP table at all."""
    return bool(read_arp_table())


async def async_local_networks(hass: HomeAssistant) -> list[str]:
    """IPv4 networks worth sweeping: the host's own LAN(s) and the entry subnets.

    Portability matters here: the integration is installed on many boxes, so the
    subnet is discovered from Home Assistant's network adapters rather than
    configured, and the subnet each already configured device sits on is added
    as a safety net.
    """
    networks: list[str] = []

    try:
        from homeassistant.components.network import async_get_adapters

        adapters = await async_get_adapters(hass)
    except Exception as err:  # noqa: BLE001 - network API is optional here
        _LOGGER.debug("Could not read network adapters: %s", err)
        adapters = []

    for adapter in adapters:
        for addr in adapter.get("ipv4") or []:
            network = _network_for(addr.get("address"), addr.get("network_prefix"))
            if network is not None:
                networks.append(network)

    for entry in hass.config_entries.async_entries(DOMAIN):
        host = {**entry.data, **entry.options}.get(CONF_HOST)
        network = _network_for(host, None)
        if network is not None:
            networks.append(network)

    unique: list[str] = []
    for network in networks:
        if network not in unique:
            unique.append(network)
    return unique


def _network_for(address: Any, prefix: Any) -> str | None:
    """Return the IPv4 network to sweep for this address, or None."""
    if not address or not isinstance(address, str):
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return None

    try:
        width = int(prefix) if prefix is not None else 24
    except (TypeError, ValueError):  # fmt: skip
        width = 24
    # A device is never more than a /24 away in practice, and sweeping a wider
    # range could mean thousands of probes.  Narrow to the address's own /24.
    if width < 24:
        width = 24
    if width > 32:
        width = 32

    try:
        network = ipaddress.ip_network(f"{ip}/{width}", strict=False)
    except ValueError:
        return None
    if network.num_addresses < 4:
        return None
    if any(
        network.subnet_of(ipaddress.ip_network(other)) for other in _INTERNAL_NETWORKS
    ):
        return None
    return str(network)


def _hosts(networks: list[str]) -> list[str]:
    """Expand networks to a bounded list of host addresses."""
    hosts: list[str] = []
    for network in networks:
        try:
            net = ipaddress.ip_network(network, strict=False)
        except ValueError:
            continue
        for ip in net.hosts():
            hosts.append(str(ip))
            if len(hosts) >= MAX_HOSTS:
                return hosts
    return hosts


def probe(ip: str, port: int = TUYA_PORT, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether something accepts a TCP connection on ``ip:port``."""
    with socket.socket() as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((ip, port))
        except OSError:
            return False
    return True


def _sweep(hosts: list[str], port: int = TUYA_PORT) -> list[str]:
    """Return the addresses that answered, probing concurrently."""
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        results = pool.map(lambda ip: (ip, probe(ip, port)), hosts)
        return [ip for ip, ok in results if ok]


async def async_tuya_addresses(hass: HomeAssistant) -> list[str]:
    """Return the LAN addresses that accept a connection on the Tuya port."""
    hosts = _hosts(await async_local_networks(hass))
    return await hass.async_add_executor_job(_sweep, hosts)


async def async_scan_by_mac(hass: HomeAssistant) -> dict[str, str]:
    """Return ``{mac: ip}`` for the Tuya devices answering on the LAN."""
    responders = await async_tuya_addresses(hass)
    if not responders:
        return {}
    arp = await async_arp_table(hass)
    return {arp[ip]: ip for ip in responders if ip in arp}


def probe_identity(
    candidates: list[str], wanted: dict[str, tuple[str, Any]]
) -> dict[str, str]:
    """Return ``{device_id: ip}`` for the devices that answer as themselves.

    Blocking; run in an executor.  The local key is already known, so a device
    that answers *this* id with state is that device: the handshake proves the
    identity, which is what a bare TCP answer does not.  Versions are tried
    cheapest-first and the scan stops as soon as nothing is left to find.
    """
    import tinytuya

    found: dict[str, str] = {}
    pending = dict(wanted)
    for ip in candidates:
        if not pending:
            break
        for device_id, (local_key, version) in list(pending.items()):
            if not local_key:
                pending.pop(device_id, None)
                continue
            versions = (
                [version] if isinstance(version, (int, float)) else [3.3, 3.5, 3.4]
            )
            for protocol in versions:
                try:
                    device = tinytuya.Device(device_id, ip, local_key, version=protocol)
                    device.set_socketTimeout(PROTOCOL_PROBE_TIMEOUT)
                    device.set_socketPersistent(False)
                    status = device.status()
                except Exception as err:  # noqa: BLE001 - wrong device or no answer
                    _LOGGER.debug(
                        "Identity probe %s @ %s v%s: %s", device_id, ip, protocol, err
                    )
                    continue
                if isinstance(status, dict) and status.get("dps") is not None:
                    found[device_id] = ip
                    pending.pop(device_id, None)
                    _LOGGER.info(
                        "Identified %s at %s by a Tuya handshake (protocol %s)",
                        device_id,
                        ip,
                        protocol,
                    )
                    break
    return found


def _protocol_scan_due(hass: HomeAssistant) -> bool:
    """Throttle the protocol probe: it is the most expensive check."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    last = domain_data.get(PROTOCOL_SCAN_AT) or 0.0
    if (time.monotonic() - last) < PROTOCOL_SCAN_INTERVAL:
        return False
    domain_data[PROTOCOL_SCAN_AT] = time.monotonic()
    return True


async def async_identify_devices(
    hass: HomeAssistant, entries: list, *, force: bool = False
) -> dict[str, str]:
    """Identify configured devices by talking the Tuya protocol to the LAN."""
    wanted: dict[str, tuple[str, Any]] = {}
    for entry in entries:
        device_id = _entry_device_id(entry)
        local_key = entry.data.get(CONF_LOCAL_KEY)
        if device_id and local_key:
            wanted[device_id] = (local_key, entry.data.get(CONF_PROTOCOL_VERSION))
    if not wanted or not (force or _protocol_scan_due(hass)):
        return {}

    candidates = await async_tuya_addresses(hass)
    if not candidates:
        return {}
    return await hass.async_add_executor_job(probe_identity, candidates, wanted)


def _store(hass: HomeAssistant) -> Store:
    """Return the persistent store holding ``{device_id: mac}``."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(MACS_STORE)
    if store is None:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        domain_data[MACS_STORE] = store
    return store


async def async_load_macs(hass: HomeAssistant) -> dict[str, str]:
    """Return ``{device_id: mac}``, loading from disk once per session."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get(MACS_CACHE)
    if cached is not None:
        return cached
    macs: dict[str, str] = {}
    try:
        stored = await _store(hass).async_load()
        if isinstance(stored, dict):
            for key, value in stored.items():
                mac = _normalise_mac(value if isinstance(value, str) else None)
                if mac:
                    macs[str(key)] = mac
    except Exception as err:  # noqa: BLE001 - the cache must never break setup
        _LOGGER.warning("Could not load remembered device MACs: %s", err)
    domain_data[MACS_CACHE] = macs
    return macs


async def async_save_macs(hass: HomeAssistant, macs: dict[str, str]) -> None:
    """Persist ``{device_id: mac}``."""
    hass.data.setdefault(DOMAIN, {})[MACS_CACHE] = macs
    try:
        await _store(hass).async_save(macs)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not save remembered device MACs: %s", err)


def _entry_host(entry) -> str | None:
    """Return the host the entry currently dials."""
    return {**entry.data, **entry.options}.get(CONF_HOST)


def _entry_device_id(entry) -> str | None:
    """Return the entry's device id."""
    return entry.data.get(CONF_DEVICE_ID)


def _write_host(hass: HomeAssistant, entry, ip: str) -> None:
    """Rewrite the stored host, wherever it currently takes effect."""
    options = entry.options
    if CONF_HOST in entry.options:
        options = {**entry.options, CONF_HOST: ip}
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: ip}, options=options
    )


async def async_learn_macs(hass: HomeAssistant) -> int:
    """Remember the MAC of every device currently answering on its stored host.

    Called while devices are healthy, so that a later DHCP change can be
    corrected by MAC.  Cheap: one ARP table read, no probing.
    """
    arp = await async_arp_table(hass)
    if not arp:
        return 0

    macs = dict(await async_load_macs(hass))
    learned = 0
    domain_data = hass.data.get(DOMAIN, {})
    for entry in hass.config_entries.async_entries(DOMAIN):
        device_id = _entry_device_id(entry)
        host = _entry_host(entry)
        if not device_id or not host:
            continue
        # Only a device that is answering its own protocol proves which MAC
        # belongs to it.  A stale address now held by another device would
        # otherwise be recorded as this device's MAC, and every later lookup
        # would follow the wrong device.
        bucket = domain_data.get(device_id)
        device = bucket.get("device") if isinstance(bucket, dict) else None
        if device is None or not device.has_returned_state:
            continue
        mac = arp.get(host)
        if mac and macs.get(device_id) != mac:
            macs[device_id] = mac
            learned += 1
            _LOGGER.debug("%s: learned MAC %s at %s", entry.title, mac, host)

    if learned:
        await async_save_macs(hass, macs)
    return learned


def _reachable_hosts(hass: HomeAssistant) -> set[str]:
    """Hosts of entries whose device object is reporting state."""
    domain_data = hass.data.get(DOMAIN, {})
    live: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        device_id = _entry_device_id(entry)
        bucket = domain_data.get(device_id) if device_id else None
        device = bucket.get("device") if isinstance(bucket, dict) else None
        host = _entry_host(entry)
        if device is not None and device.has_returned_state and host:
            live.add(host)
    return live


async def async_unreachable_entries(
    hass: HomeAssistant,
    macs: dict[str, str] | None = None,
    arp: dict[str, str] | None = None,
) -> list:
    """Return entries whose device is not answering on its stored address.

    Entries that have a device object are judged by whether it is still
    reporting state.  Entries that do not (not set up yet, or setup failed) are
    probed on the stored host -- but a TCP answer alone is not proof the device
    is there: the address may have been reassigned to a *different* device, so
    where a MAC is on record the answering host must carry that MAC, otherwise
    the entry is treated as unreachable and gets relocated.
    """
    macs = macs or {}
    if arp is None:
        arp = await async_arp_table(hass)

    domain_data = hass.data.get(DOMAIN, {})
    unreachable = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        device_id = _entry_device_id(entry)
        host = _entry_host(entry)
        if entry.disabled_by or not device_id or not host:
            continue
        bucket = domain_data.get(device_id)
        device = bucket.get("device") if isinstance(bucket, dict) else None
        if device is not None:
            if not device.has_returned_state:
                unreachable.append(entry)
            continue
        # No device object: the entry is between setups, or its setup failed.
        # A TCP answer on the stored address proves nothing here - after a DHCP
        # change that address is often held by a *different* device - so only a
        # recorded MAC that matches the ARP entry counts as "it is still there".
        # Without a record the entry is handed to the verified relocation below,
        # which does nothing at all when the device is where it should be.
        mac = macs.get(device_id)
        if mac is None:
            unreachable.append(entry)
            continue
        answers = await hass.async_add_executor_job(probe, host)
        if not answers or arp.get(host) != mac:
            unreachable.append(entry)
    return unreachable


async def async_broadcast_lookup(
    hass: HomeAssistant, device_ids: list[str]
) -> dict[str, str]:
    """Return ``{device_id: ip}`` from the Tuya discovery broadcast.

    One scan answers for every missing device at once.  The scan is left to the
    networks tinytuya detects itself -- the same call the periodic discovery
    scan already relies on.  ``forcescan`` is deliberately not used: tinytuya
    disables it, and prints to stdout, unless a keyfile is present, which this
    integration never writes.
    """
    if not device_ids:
        return {}
    wanted = set(device_ids)

    def _scan() -> dict[str, str]:
        import tinytuya

        try:
            found = tinytuya.deviceScan(
                verbose=False,
                poll=False,
                maxretry=2,
                byID=True,
            )
        except OSError as err:
            _LOGGER.debug("Tuya broadcast scan failed: %s", err)
            return {}
        result = {}
        for gwid, info in found.items():
            if gwid in wanted and info.get("ip"):
                result[gwid] = info["ip"]
        return result

    return await hass.async_add_executor_job(_scan)


def choose_candidate(
    *,
    mac: str | None,
    device_id: str,
    by_mac: dict[str, str],
    broadcast: dict[str, str],
    identified: dict[str, str] | None = None,
    arp: dict[str, str] | None = None,
    live_hosts: Any = (),
) -> tuple[str | None, str | None, str | None]:
    """Pick the address a device moved to, and say how it was confirmed.

    Returns ``(ip, confirmed_by, problem)``: ``ip`` is None when there is no
    address that is safe to use, and ``problem`` then explains why (``None`` for
    "no candidate at all", a string for "candidate rejected").

    Both identities must agree, which is what keeps an unreachable device (its
    Tuya broadcast may be cached or stale) from being moved onto a wrong
    address: a MAC match must also be the address the device id claimed, and a
    broadcast result must sit on the recorded MAC.
    """
    arp = arp or {}
    mac_ip = by_mac.get(mac) if mac else None
    broadcast_ip = broadcast.get(device_id)
    identified_ip = (identified or {}).get(device_id)

    if identified_ip:
        # Strongest evidence: the device answered a full Tuya handshake for its
        # own id and local key.  Nothing else is needed.
        if identified_ip in live_hosts:
            return (
                None,
                "protocol",
                f"candidate address {identified_ip} is in use by another device",
            )
        return identified_ip, "protocol", None

    if mac_ip:
        if broadcast_ip and broadcast_ip != mac_ip:
            return (
                None,
                "mac",
                f"MAC {mac} is at {mac_ip} but the device id answers from "
                f"{broadcast_ip}",
            )
        ip, confirmed_by = mac_ip, "mac"
    elif broadcast_ip:
        if mac and arp.get(broadcast_ip) != mac:
            return (
                None,
                "broadcast",
                f"device id found at {broadcast_ip} but its MAC is "
                f"{arp.get(broadcast_ip)}, expected {mac}",
            )
        ip, confirmed_by = broadcast_ip, "broadcast"
    else:
        return None, None, None

    if ip in live_hosts:
        return None, confirmed_by, f"candidate address {ip} is in use by another device"

    return ip, confirmed_by, None


async def async_relocate_devices(
    hass: HomeAssistant, *, force_protocol: bool = False
) -> dict[str, Any]:
    """Put unreachable devices back on their current address.

    A device is placed on three pieces of evidence, strongest first: a full Tuya
    handshake for its own id and key, the MAC recorded at the candidate address,
    or the address the device id answers the discovery broadcast from.  Returns a
    summary suitable for a service response or a log line.

    ``force_protocol`` skips the throttle on the handshake probe, for the manual
    service (the periodic sweep keeps it, as the probe is the slow one).
    """
    relocated: list[str] = []
    unverified: list[str] = []
    not_found: list[str] = []

    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if not entry.disabled_by and _entry_device_id(entry) and _entry_host(entry)
    ]
    if not entries:
        return {
            "relocated": relocated,
            "unverified": unverified,
            "not_found": not_found,
        }

    # Keep the MAC record current while the devices are still healthy.
    learned = await async_learn_macs(hass)
    macs = await async_load_macs(hass)
    arp = await async_arp_table(hass)

    unreachable = await async_unreachable_entries(hass, macs, arp)
    if not unreachable:
        return {
            "relocated": relocated,
            "unverified": unverified,
            "not_found": not_found,
            "learned": learned,
        }

    live_hosts = _reachable_hosts(hass)
    # Re-read after the sweep below has spoken to the devices: the verification
    # of a broadcast result depends on a fresh ARP entry.
    arp = await async_arp_table(hass)
    by_mac = await async_scan_by_mac(hass) if arp else {}
    broadcast = await async_broadcast_lookup(
        hass, [_entry_device_id(entry) for entry in unreachable]
    )

    # Devices the two cheap checks could not place are identified by talking the
    # Tuya protocol to each device on the LAN: that needs no MAC record (a fresh
    # install has none) and no answer to the discovery broadcast.
    identified: dict[str, str] = {}
    unresolved = [
        entry
        for entry in unreachable
        if macs.get(_entry_device_id(entry)) not in by_mac
        and _entry_device_id(entry) not in broadcast
    ]
    if unresolved:
        identified = await async_identify_devices(
            hass, unreachable, force=force_protocol
        )

    macs_dirty = False
    for entry in unreachable:
        device_id = _entry_device_id(entry)
        current = _entry_host(entry)
        mac = macs.get(device_id)

        candidate, confirmed_by, problem = choose_candidate(
            mac=mac,
            device_id=device_id,
            by_mac=by_mac,
            broadcast=broadcast,
            identified=identified,
            arp=arp,
            live_hosts=live_hosts,
        )
        if candidate is None:
            if problem is None:
                not_found.append(entry.title)
                continue
            _LOGGER.warning("%s: %s; not relocating", entry.title, problem)
            unverified.append(entry.title)
            continue
        if candidate == current:
            continue
        if not await hass.async_add_executor_job(probe, candidate):
            _LOGGER.warning(
                "%s: %s does not accept connections; not relocating",
                entry.title,
                candidate,
            )
            unverified.append(entry.title)
            continue

        # WARNING, not INFO: an IP change is a notable operational event and
        # config entries commonly run at log level WARNING.
        _LOGGER.warning(
            "%s: LAN IP changed to %s (was %s), confirmed by %s; updating configuration",
            entry.title,
            candidate,
            current,
            confirmed_by,
        )
        _write_host(hass, entry, candidate)
        relocated.append(f"{entry.title}: {current} -> {candidate}")
        live_hosts.add(candidate)

        # The device is on a new address now; re-read its MAC there so a later
        # change is matched on a fresh record.
        arp = await async_arp_table(hass)
        new_mac = arp.get(candidate)
        if new_mac and macs.get(device_id) != new_mac:
            macs[device_id] = new_mac
            macs_dirty = True

    if macs_dirty:
        await async_save_macs(hass, macs)

    return {
        "relocated": relocated,
        "unverified": unverified,
        "not_found": not_found,
        "learned": learned,
    }
