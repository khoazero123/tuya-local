"""Tests for MAC verified IP relocation (``macmap``) against a real hass.

The pure decision rules live in ``tests/test_macmap.py`` (no Home Assistant
needed); these tests cover the integration side: which entries are looked at,
that the stored host is rewritten where it takes effect, and that a rejected
candidate leaves the entry alone.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local import macmap
from custom_components.tuya_local.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)

TESTKEY = ")<jO<@)'P1|kR$Kd"
DEVID = "bf1234567890abcdef"
OTHER_MAC = "bc:35:1e:4d:0a:f3"
MAC = "d0:82:eb:84:87:59"
OLD_HOST = "192.168.1.10"
NEW_HOST = "192.168.1.77"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _make_entry(hass, host=OLD_HOST, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=13,
        minor_version=20,
        title="thermostat",
        data={
            CONF_DEVICE_ID: DEVID,
            CONF_HOST: host,
            CONF_LOCAL_KEY: TESTKEY,
            CONF_POLL_ONLY: False,
            CONF_PROTOCOL_VERSION: "auto",
            CONF_TYPE: "polytherm_polyalpha_thermostat",
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


def _set_device(hass, returned_state, device_id=DEVID):
    """Register a fake device object in hass.data under the device id."""
    device = type("Dev", (), {"has_returned_state": returned_state})()
    hass.data.setdefault(DOMAIN, {})[device_id] = {"device": device}
    return device


def _patch_scan(mocker, *, by_mac, broadcast, arp, macs, probe=True):
    """Patch the LAN/MAC lookups and report what was called."""
    calls = {}
    calls["scan"] = mocker.patch.object(
        macmap, "async_scan_by_mac", AsyncMock(return_value=by_mac)
    )
    calls["broadcast"] = mocker.patch.object(
        macmap, "async_broadcast_lookup", AsyncMock(return_value=broadcast)
    )
    calls["arp"] = mocker.patch.object(
        macmap, "async_arp_table", AsyncMock(return_value=arp)
    )
    calls["load"] = mocker.patch.object(
        macmap, "async_load_macs", AsyncMock(return_value=macs)
    )
    calls["learn"] = mocker.patch.object(
        macmap, "async_learn_macs", AsyncMock(return_value=0)
    )
    calls["save"] = mocker.patch.object(macmap, "async_save_macs", AsyncMock())
    calls["probe"] = mocker.patch.object(macmap, "probe", MagicMock(return_value=probe))
    return calls


@pytest.mark.asyncio
async def test_relocate_updates_host_of_unreachable_device(hass, mocker):
    """A device that stopped answering is moved to the address its MAC is on."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=False)
    _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: NEW_HOST},
        arp={NEW_HOST: MAC},
        macs={DEVID: MAC},
    )

    result = await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == NEW_HOST
    assert result["relocated"] == [f"thermostat: {OLD_HOST} -> {NEW_HOST}"]
    assert result["unverified"] == []


@pytest.mark.asyncio
async def test_relocate_skips_device_that_is_reporting_state(hass, mocker):
    """A healthy device is never scanned for."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=True)
    calls = _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: NEW_HOST},
        arp={NEW_HOST: MAC},
        macs={DEVID: MAC},
    )

    result = await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    calls["scan"].assert_not_awaited()
    calls["broadcast"].assert_not_awaited()
    assert entry.data[CONF_HOST] == OLD_HOST
    assert result["relocated"] == []


@pytest.mark.asyncio
async def test_relocate_refuses_when_broadcast_and_mac_disagree(hass, mocker):
    """The MAC record wins only when the device id does not say otherwise."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=False)
    _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: "192.168.1.50"},
        arp={NEW_HOST: MAC},
        macs={DEVID: MAC},
    )

    result = await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == OLD_HOST
    assert result["unverified"] == ["thermostat"]


@pytest.mark.asyncio
async def test_relocate_refuses_broadcast_on_a_foreign_mac(hass, mocker):
    """A stale broadcast must not move the entry onto another device's address."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=False)
    _patch_scan(
        mocker,
        by_mac={},
        broadcast={DEVID: NEW_HOST},
        arp={NEW_HOST: OTHER_MAC},
        macs={DEVID: MAC},
    )

    result = await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == OLD_HOST
    assert result["unverified"] == ["thermostat"]


@pytest.mark.asyncio
async def test_relocate_leaves_device_alone_when_not_on_the_lan(hass, mocker):
    """A powered off device matches nothing, so nothing is rewritten."""
    entry = _make_entry(hass)
    _set_device(hass, returned_state=False)
    _patch_scan(mocker, by_mac={}, broadcast={}, arp={}, macs={DEVID: MAC})

    result = await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == OLD_HOST
    assert result["not_found"] == ["thermostat"]


@pytest.mark.asyncio
async def test_relocate_updates_host_stored_in_options(hass, mocker):
    """When the effective host lives in options, both copies are updated."""
    entry = _make_entry(hass, host="10.0.0.1", options={CONF_HOST: OLD_HOST})
    _set_device(hass, returned_state=False)
    _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: NEW_HOST},
        arp={NEW_HOST: MAC},
        macs={DEVID: MAC},
    )

    await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.options[CONF_HOST] == NEW_HOST
    assert entry.data[CONF_HOST] == NEW_HOST


@pytest.mark.asyncio
async def test_relocate_probes_host_when_entry_has_no_device_object(hass, mocker):
    """An entry whose setup failed has no device object, so its host is probed."""
    entry = _make_entry(hass)
    hass.data.setdefault(DOMAIN, {})
    calls = _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: NEW_HOST},
        arp={NEW_HOST: MAC},
        macs={DEVID: MAC},
    )
    # The stored host no longer answers; the new address does.
    calls["probe"].side_effect = lambda ip, **kwargs: ip == NEW_HOST

    await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == NEW_HOST


@pytest.mark.asyncio
async def test_relocate_fixes_entry_whose_old_address_is_now_someone_else(hass, mocker):
    """A TCP answer on the stored address is not proof it is our device.

    After a router reboot the old lease can be handed to another device: the
    port answers, but the MAC there is not ours, so the entry is still moved to
    the address our MAC is on.
    """
    entry = _make_entry(hass)
    hass.data.setdefault(DOMAIN, {})  # no device object either
    calls = _patch_scan(
        mocker,
        by_mac={MAC: NEW_HOST},
        broadcast={DEVID: NEW_HOST},
        arp={OLD_HOST: OTHER_MAC, NEW_HOST: MAC},
        macs={DEVID: MAC},
    )
    calls["probe"].return_value = True  # the stale address still answers

    await macmap.async_relocate_devices(hass)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == NEW_HOST


@pytest.mark.asyncio
async def test_relocate_does_nothing_without_entries(hass, mocker):
    calls = _patch_scan(mocker, by_mac={}, broadcast={}, arp={}, macs={})
    result = await macmap.async_relocate_devices(hass)
    assert result == {"relocated": [], "unverified": [], "not_found": []}
    calls["scan"].assert_not_awaited()


@pytest.mark.asyncio
async def test_learn_macs_remembers_the_mac_of_a_reachable_device(hass, mocker):
    """A healthy device's MAC is recorded so a later change can be corrected."""
    entry = _make_entry(hass)
    mocker.patch.object(
        macmap, "async_arp_table", AsyncMock(return_value={OLD_HOST: MAC})
    )
    saved = mocker.patch.object(macmap, "async_save_macs", AsyncMock())
    hass.data.setdefault(DOMAIN, {}).pop(macmap.MACS_CACHE, None)

    learned = await macmap.async_learn_macs(hass)

    assert learned == 1
    saved.assert_awaited_once()
    assert saved.await_args[0][1] == {DEVID: MAC}
    assert entry.data[CONF_HOST] == OLD_HOST


@pytest.mark.asyncio
async def test_learn_macs_is_quiet_when_arp_is_unavailable(hass, mocker):
    """An install that cannot see the ARP table learns nothing and logs nothing."""
    _make_entry(hass)
    mocker.patch.object(macmap, "async_arp_table", AsyncMock(return_value={}))
    saved = mocker.patch.object(macmap, "async_save_macs", AsyncMock())

    assert await macmap.async_learn_macs(hass) == 0
    saved.assert_not_awaited()
