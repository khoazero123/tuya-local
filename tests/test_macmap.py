"""Tests for ``macmap`` (plain Python, no Home Assistant required).

Run directly::

    python3 tests/test_macmap.py

The module stubs the few ``homeassistant`` imports ``macmap`` makes at import
time and installs the integration directory as a package, so the relative
imports resolve the same way they do inside Home Assistant.  Under ``pytest``
the ``test_*`` functions are collected as well.
"""

from __future__ import annotations

import importlib
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "tuya_local"


def _install_homeassistant_stubs() -> None:
    """Stub the Home Assistant modules macmap imports at import time."""
    try:
        import homeassistant.const  # noqa: F401
        import homeassistant.core  # noqa: F401
        import homeassistant.helpers.storage  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []  # type: ignore[attr-defined]

    const = types.ModuleType("homeassistant.const")
    const.CONF_HOST = "host"

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:  # noqa: D101 - stub
        pass

    core.HomeAssistant = HomeAssistant

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []  # type: ignore[attr-defined]
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:  # noqa: D101 - stub
        def __init__(self, *args, **kwargs) -> None:
            pass

    storage.Store = Store

    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.const", const)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.storage", storage)


def _load_macmap():
    """Import ``tuya_local.macmap`` without running the package __init__."""
    _install_homeassistant_stubs()
    if "tuya_local" not in sys.modules:
        package = types.ModuleType("tuya_local")
        package.__path__ = [str(PKG_DIR)]  # type: ignore[attr-defined]
        sys.modules["tuya_local"] = package
    return importlib.import_module("tuya_local.macmap")


macmap = _load_macmap()


def _require_standalone() -> None:
    """Skip a loopback socket test when running under pytest.

    Home Assistant's pytest plugin blocks AF_INET sockets for every test, in
    ``pytest_runtest_setup`` *after* markers are handled, so neither
    ``@pytest.mark.enable_socket`` nor ``pytest_socket.enable_socket()`` makes
    this file's listener usable inside that harness.  The same assertions run
    in the plain ``python3 tests/test_macmap.py`` invocation.
    """
    if "pytest" in sys.modules:
        raise unittest.SkipTest(
            "loopback socket test; run `python3 tests/test_macmap.py`"
        )


def _listener() -> tuple[socket.socket, int, threading.Event, threading.Thread]:
    """Start a throwaway TCP listener; returns (socket, port, stop, thread).

    The accept loop polls a stop flag instead of blocking forever, so the thread
    is gone by the end of the test: Home Assistant's test harness fails a test
    that leaks a thread.
    """
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    stop = threading.Event()
    thread = threading.Thread(
        target=_accept_until_stopped, args=(server, stop), daemon=True
    )
    thread.start()
    return server, server.getsockname()[1], stop, thread


def _stop_listener(
    server: socket.socket, stop: threading.Event, thread: threading.Thread
) -> None:
    """Stop a listener started by ``_listener`` and wait for its thread."""
    stop.set()
    thread.join(timeout=2)
    server.close()


def _closed_port() -> int:
    """Return a port nothing is listening on.

    A listener whose ``accept()`` is blocked in another thread keeps accepting
    even after ``close()``, so a bound-then-closed socket is used instead: it
    reserves the port without ever listening, and the probe is refused.
    """
    probe_socket = socket.socket()
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    return port


def _accept_until_stopped(server: socket.socket, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            client, _ = server.accept()
        except (TimeoutError, OSError):  # fmt: skip
            continue
        client.close()


MAC_A = "d0:82:eb:84:87:59"
MAC_B = "bc:35:1e:4d:0a:f3"


# --------------------------------------------------------------------- MACs


def test_normalise_mac_accepts_lower_and_dash_forms() -> None:
    assert macmap._normalise_mac("D0:82:EB:84:87:59") == MAC_A
    assert macmap._normalise_mac("d0-82-eb-84-87-59") == MAC_A
    assert macmap._normalise_mac(" d0:82:eb:84:87:59 ") == MAC_A


def test_normalise_mac_rejects_junk_and_failed_arp() -> None:
    assert macmap._normalise_mac(None) is None
    assert macmap._normalise_mac("") is None
    assert macmap._normalise_mac("d0:82:eb:84:87") is None
    assert macmap._normalise_mac("d0:82:eb:84:87:5g") is None
    assert macmap._normalise_mac("00:00:00:00:00:00") is None


# ----------------------------------------------------------------- networks


def test_network_for_uses_the_address_own_slash_24() -> None:
    assert macmap._network_for("192.168.1.36", 24) == "192.168.1.0/24"
    # A /16 (or wider) is narrowed to the /24 the address sits in: sweeping a
    # whole /16 would mean thousands of probes.
    assert macmap._network_for("192.168.1.36", 16) == "192.168.1.0/24"
    assert macmap._network_for("10.5.7.9", None) == "10.5.7.0/24"


def test_network_for_skips_addresses_that_cannot_hold_devices() -> None:
    assert macmap._network_for("127.0.0.1", 8) is None
    assert macmap._network_for("169.254.3.4", 16) is None
    assert macmap._network_for("224.0.0.1", 24) is None
    assert macmap._network_for("not-an-ip", 24) is None
    assert macmap._network_for(None, 24) is None
    # A /31 has no usable host addresses.
    assert macmap._network_for("192.168.1.0", 31) is None


def test_network_for_skips_home_assistant_container_networks() -> None:
    assert macmap._network_for("172.17.0.2", 16) is None
    assert macmap._network_for("172.30.32.1", 23) is None
    assert macmap._network_for("172.31.5.5", 16) is None


def test_hosts_is_bounded() -> None:
    hosts = macmap._hosts(["192.168.1.0/24"])
    assert len(hosts) == 254
    assert "192.168.1.1" in hosts and "192.168.1.254" in hosts
    assert (
        macmap._hosts(["10.0.0.0/8"])
        == macmap._hosts(["10.0.0.0/8"])[: macmap.MAX_HOSTS]
    )
    assert len(macmap._hosts(["10.0.0.0/8"])) == macmap.MAX_HOSTS


# ---------------------------------------------------------------------- ARP


def test_read_arp_table_parses_proc_and_ignores_incomplete_entries() -> None:
    content = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.56     0x1         0x2         d0:82:eb:84:87:59     *        end0\n"
        "192.168.1.31     0x1         0x0         00:00:00:00:00:00     *        end0\n"
        "192.168.1.42     0x1         0x2         BC:35:1E:4D:0A:F3     *        end0\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".arp") as handle:
        handle.write(content)
        handle.flush()
        original = macmap.ARP_FILE
        macmap.ARP_FILE = handle.name
        try:
            table = macmap.read_arp_table()
        finally:
            macmap.ARP_FILE = original

    assert table == {"192.168.1.56": MAC_A, "192.168.1.42": MAC_B}


def test_read_arp_table_missing_file_falls_back_without_raising() -> None:
    original = macmap.ARP_FILE
    macmap.ARP_FILE = "/nonexistent/arp"
    try:
        table = macmap.read_arp_table()
    finally:
        macmap.ARP_FILE = original
    assert isinstance(table, dict)


# --------------------------------------------------------------------- probe


def test_probe_and_sweep_find_a_listening_port() -> None:
    _require_standalone()
    server, port, stop, thread = _listener()
    try:
        assert macmap.probe("127.0.0.1", port=port) is True
        found = macmap._sweep(["127.0.0.1", "127.0.0.2"], port=port)
    finally:
        _stop_listener(server, stop, thread)

    assert "127.0.0.1" in found
    assert "127.0.0.2" not in found


def test_probe_reports_closed_port_as_false() -> None:
    _require_standalone()
    assert macmap.probe("127.0.0.1", port=_closed_port(), timeout=0.2) is False


# ------------------------------------------------------------ decision rules


def test_choose_candidate_prefers_mac_match_confirmed_by_broadcast() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={MAC_A: "192.168.1.99"},
        broadcast={"dev1": "192.168.1.99"},
    )
    assert (ip, by, problem) == ("192.168.1.99", "mac", None)


def test_choose_candidate_accepts_mac_match_without_broadcast() -> None:
    # A device that ignores the Tuya discovery broadcast can still be found by
    # its MAC -- the case the broadcast-only relocation cannot handle.
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={MAC_A: "192.168.1.99"},
        broadcast={},
    )
    assert (ip, by, problem) == ("192.168.1.99", "mac", None)


def test_choose_candidate_rejects_mac_and_broadcast_disagreement() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={MAC_A: "192.168.1.99"},
        broadcast={"dev1": "192.168.1.50"},
    )
    assert ip is None
    assert by == "mac"
    assert problem is not None and "192.168.1.50" in problem


def test_choose_candidate_accepts_broadcast_confirmed_by_arp_mac() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={},
        broadcast={"dev1": "192.168.1.99"},
        arp={"192.168.1.99": MAC_A},
    )
    assert (ip, by, problem) == ("192.168.1.99", "broadcast", None)


def test_choose_candidate_rejects_broadcast_when_arp_mac_differs() -> None:
    # This is the guard for upstream #5713: a stale broadcast answer must not
    # move the entry onto an address that belongs to a different device.
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={},
        broadcast={"dev1": "192.168.1.99"},
        arp={"192.168.1.99": MAC_B},
    )
    assert ip is None
    assert by == "broadcast"
    assert problem is not None and MAC_B in problem


def test_choose_candidate_rejects_broadcast_when_mac_unknown_at_address() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={},
        broadcast={"dev1": "192.168.1.99"},
        arp={},
    )
    assert ip is None
    assert by == "broadcast"


def test_choose_candidate_prefers_the_handshake_identity() -> None:
    """A device that answered its own id and key needs no other evidence."""
    ip, by, problem = macmap.choose_candidate(
        mac=None,
        device_id="dev1",
        by_mac={},
        broadcast={},
        identified={"dev1": "192.168.1.99"},
    )
    assert (ip, by, problem) == ("192.168.1.99", "protocol", None)


def test_choose_candidate_refuses_handshake_identity_on_a_used_address() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=None,
        device_id="dev1",
        by_mac={},
        broadcast={},
        identified={"dev1": "192.168.1.99"},
        live_hosts={"192.168.1.99"},
    )
    assert ip is None
    assert by == "protocol"
    assert problem is not None and "in use" in problem


def test_choose_candidate_reports_no_candidate_when_nothing_matches() -> None:
    assert macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={},
        broadcast={},
    ) == (None, None, None)


def test_choose_candidate_refuses_address_another_device_answers_on() -> None:
    ip, by, problem = macmap.choose_candidate(
        mac=MAC_A,
        device_id="dev1",
        by_mac={MAC_A: "192.168.1.99"},
        broadcast={},
        live_hosts={"192.168.1.99"},
    )
    assert ip is None
    assert by == "mac"
    assert problem is not None and "in use" in problem


def test_choose_candidate_works_without_a_recorded_mac() -> None:
    # First sighting after an IP change on an install that never learned the
    # MAC: the broadcast is the only identity available, and it is enough.
    ip, by, problem = macmap.choose_candidate(
        mac=None,
        device_id="dev1",
        by_mac={},
        broadcast={"dev1": "192.168.1.99"},
        arp={},
    )
    assert (ip, by, problem) == ("192.168.1.99", "broadcast", None)


def _main() -> int:
    tests = [
        (name, value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {name}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
