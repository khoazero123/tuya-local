# Local patches in this fork

Base: upstream `2026.10.0` (synced 2026-10-14: 11 upstream commits since
2026-10-03 — device YAMLs, `DEVICES.md` and `tests/test_entity.py` only, none of
the files patched below; the earlier 43 commits since `2026.9.2` likewise touch
none of them).

This fork is based on `make-all/tuya-local`. All local patches live on the
`local-patches` branch (see `git log` for details); releases are cut from it so
HACS updates keep the patches.

## 1. Persist the Tuya cloud login to disk (2026.9.2.1)

**Problem:** upstream keeps the cloud-assisted login (`auth_cache`) only in
`hass.data`, so it is lost on every Home Assistant restart and the user code +
QR login must be repeated before adding another device. Upstream considers this
by design (see make-all/tuya-local#6019).

**Patch:**
- `custom_components/tuya_local/cloud.py`
  - adds `_get_auth_store()`, `async_restore_auth()` and `async_save_auth()`
    using HA's `Store(hass, 1, "tuya_local.cloud_auth", private=True)`.
  - login success persists the cache to
    `/config/.storage/tuya_local.cloud_auth` (mode 0600); failed login and
    `logout()` remove it.
  - `TokenListener` now receives the auth dict and re-persists it whenever the
    Tuya SDK refreshes the token (`customerapi.refresh_access_token_if_need()`),
    keeping the refresh chain alive.
- `custom_components/tuya_local/config_flow.py`
  - `await async_restore_auth(self.hass)` at the start of `async_step_user`,
    so a saved login is loaded before the setup mode is handled.

**Notes:** only the cloud-assisted setup path is affected; device control stays
100% local (`iot_class: local_push`). If Tuya rejects the saved refresh token,
the flow falls back to the normal user code + QR login. The token file contains
Tuya access/refresh tokens, so treat `/config/.storage/tuya_local.cloud_auth` as
a secret (it is included in HA backups).

## 2. Show the device name on discovered-device cards (2026.9.2.2)

**Problem:** the "Discovered" cards in Settings > Devices & Services showed only
the IP address, so it was impossible to tell which device was which.

**Patch:**
- `custom_components/tuya_local/helpers/device_config.py`
  - `TuyaDeviceConfig.product_display_name(product_id)` returns
    `"<config name> · <manufacturer> <model>"` for a product id listed in that
    config.
  - module level `product_display_name(product_id)` finds the config file for a
    product id (cheap text pre-filter, only matching files are parsed, result
    cached per process; blocking, call from an executor).
- `custom_components/tuya_local/config_flow.py`
  - `async_step_integration_discovery` now sets the flow title to
    `"<device name> · <ip>"` (falls back to the device id when the product id is
    unknown).


## 3. Refresh device list (service + button) and cloud device names (2026.9.2.4)

**Problem:** when a device's LAN IP changes (DHCP, power cut) the stored host
goes stale and the entry sits in "device offline" until reconfigured by hand.
Discovered cards also only had the device *type* name, while the name the user
set in the Tuya/SmartLife app is only available from the cloud.

**Patch:**
- `custom_components/tuya_local/refresh.py` (new)
  - `tuya_local.refresh_devices` service (also usable as an action from
    automations): re-locates every configured device on the LAN **by device id**
    and updates the stored host when it changed (the entry reloads itself),
    refreshes the cloud device-name cache, and re-runs the LAN discovery scan.
    Returns a summary (relocated / unchanged / not_found / cloud_names).
  - caches `{device_id: app name}` in `/config/.storage/tuya_local.cloud_names`
    (private), fetched from the cloud only when a saved login exists.
- `custom_components/tuya_local/button.py`
  - adds an integration level **"Refresh device list"** button
    (`button.tuya_local_refresh_device_list`) on a "Tuya Local" service device,
    created once; it calls the service above.
- `custom_components/tuya_local/cloud.py`
  - `async_get_device_names()` returns the app names from the cloud list.
- `custom_components/tuya_local/discovery.py`
  - discovery flow data now carries the cached app name, and
    `async_scan_now()` allows an on-demand rescan.
- `custom_components/tuya_local/config_flow.py`
  - discovered-device label prefers the app name, falling back to the
    product-id device type, plus the IP.

**Notes:** the cloud call is optional and only happens when a saved login
exists; device control stays 100% local.


## 4. "already_in_progress" when adding a device by hand (2026.9.2.5)

**Problem:** a device that only shows up under "Discovered" has an in-progress
discovery flow.  Starting "Add device" and entering that same device made HA
abort with `already_in_progress` (`async_set_unique_id` defaults to
`raise_on_progress=True`), so the device could not be added by hand until the
discovery card was dealt with.

**Patch:**
- `custom_components/tuya_local/config_flow.py`
  - `async_step_local` now calls
    `async_set_unique_id(..., raise_on_progress=False)`.  Duplicate *entries*
    are still prevented by `_abort_if_unique_id_configured()`.
  - discovered-device labels prefer, in order: the name set in the Tuya app,
    a config type already in use for the same product id, then the product-id
    device type — plus the IP.
- `custom_components/tuya_local/discovery.py`
  - passes `preferred_type` (a config type already used by a configured device
    with the same product id) into the discovery flow data.


## 5. Adding a discovered device asked to choose it again (2026.9.2.7)

**Problem:** starting the add flow from a "Discovered" card and picking the cloud
path asked "Choose the device to add" even though the device was already known
from the LAN scan.  The "use the discovered device and skip the cloud list"
shortcut only existed on the QR scan path, so a cached cloud login (or any
login that did not go through the scan step) fell through to the cloud device
list.

**Patch:**
- `custom_components/tuya_local/config_flow.py`
  - the shortcut is factored into `_async_continue_after_login()` and used both
    after the QR scan and when an existing (cached) login is reused.


## 6. Discovered cards missing after a restart (2026.9.2.8)

**Problem:** a Home Assistant restart clears in-progress config flows, and the
discovery scan only runs every `SCAN_INTERVAL` (10 minutes), so for up to ten
minutes after a restart nothing was listed under "Discovered".

**Patch:**
- `custom_components/tuya_local/discovery.py`
  - runs one extra scan `STARTUP_SCAN_DELAY` (45s) after startup;
  - the LAN scan now uses `maxretry=2` so a single lost packet no longer makes
    a reachable device look absent.
- `custom_components/tuya_local/refresh.py`
  - same `maxretry=2` for the on-demand refresh scan.


## 7. Local key left empty on a discovered device (2026.9.2.10)

**Problem:** clicking Add on a Discovered card opened the device details with
the device id, IP and protocol prefilled but the **local key empty**, even
though a cloud login was saved.  The saved login lives on disk
(`/config/.storage/tuya_local.cloud_auth`) and is only restored into
`hass.data` by `async_restore_auth()`, which the discovery step never called —
so `Cloud.is_authenticated` was False and the cloud device list (the only local
source of the local key) was never consulted.

**Patch:**
- `custom_components/tuya_local/config_flow.py`
  - `async_step_integration_discovery` calls `await async_restore_auth(hass)`
    before `init_cloud()`.
- `custom_components/tuya_local/refresh.py`
  - `async_handle_refresh_devices` restores the saved login before refreshing
    the cloud device names, so the refresh button works right after a restart
    too.

## 8. Local device config: Zitech ZT-Box

- `custom_components/tuya_local/devices/zitech_ztbox.yaml` — added so a HACS
  update cannot delete it (it previously lived only in `/config`).

- **IR2MQTT bridge cho remote IR** (2026-10-07): đưa bridge IR2MQTT vào chính
  integration để không phải chạy process ngoài.
  - `custom_components/tuya_local/ir2mqtt_bridge.py` (mới): class `IR2MQTTBridge`
    (publish retained `ir2mqtt/bridge/<id>/{config,state}`, subscribe `/command`,
    publish `/received`) + encoder raw/nec/samsung/sony + tách timings >50ms
    thành nhiều chunk (giống `infrared.py`).
  - `const.py`: thêm `CONF_IR2MQTT_BRIDGE = "ir2mqtt_bridge"`.
  - `config_flow.py`: thêm field optional `ir2mqtt_bridge` vào options step
    (để trống = tắt).
  - `remote.py`: lưu option lên device trong `async_setup_entry`; entity
    `async_added_to_hass` start bridge, `async_will_remove_from_hass` stop,
    `on_receive` đẩy DP receive lên `/received`.
  - Bật: Settings → Devices & Services → Tuya Local → Configure →
    ô "IR2MQTT bridge" = id bridge (vd `s06`).

## IR2MQTT bridge — dùng cho mọi thiết bị IR của tuya-local

Module `ir2mqtt_bridge.py` chỉ cần entity `remote` có DP **send** (201) + **receive** (202),
nên dùng được cho mọi profile IR, ví dụ:

- S06/S18 IR (`basic_ir_remote`), S11 IR+RF (`s11_rfir_remote`), universal remote
  (`ir_remote_sensors` — bản có cảm biến), WOOX R7246, Moes heatpump, v.v.

Cách bật: **Settings → Devices & Services → Tuya Local → Configure → "IR2MQTT bridge"**
(chọn từ dropdown: `(tắt)` / id gợi ý theo tên thiết bị / id đang dùng / id của entry khác).

⚠️ **id bridge phải khác nhau giữa các nguồn** — ví dụ bridge `s11` đang do thiết bị S11
chạy **ESPHome** giữ; nếu thêm một S11 khác (firmware Tuya gốc) thì chọn id khác, ví dụ `s11_oem`.

⚠️ Hiện bridge chỉ hỗ trợ **IR** (raw/nec/samsung/sony). Phần **RF 433 MHz** của S11 chưa được
đưa vào (profile `s11_rfir_remote` của tuya-local không map DP RF; và hợp đồng JSON của IR2MQTT
cho RF dùng payload khác — `rf:`/`rfstudy_send`).

## CI (fork) — ghi chú

Các patch của fork làm lệch một số test/kiểm tra so với upstream; fork giữ CI xanh bằng:

- `button.py`: nút "Refresh device list" (integration level) được đăng ký **sau** khi
  `async_tuya_setup_platform` chạy xong ⇒ entry có config sai vẫn raise `ValueError`
  và không đăng ký nút (test upstream `assert_not_called()` giữ nguyên).
- `tests/test_button.py::test_init_entry_as_secondary`: kỳ vọng
  `m_add_entities.call_count == 2` (nút của thiết bị + nút hub).
- `config_flow.py`: option `ir2mqtt_bridge` rỗng thì **không** ghi vào entry data
  (để `test_options_flow_modifies_config` vẫn khớp dict kỳ vọng).
- Đã chạy `ruff format` cho các file fork patch (`cloud.py`, `config_flow.py`,
  `helpers/device_config.py`, `ir2mqtt_bridge.py`, `refresh.py`) để job "Linting" xanh.

## Re-basing after an upstream update

Sync log:

- **2026-10-16** — release `2026.10.0.8`: MAC verified IP relocation
  (patch #10). No upstream sync in this release; the base is still upstream
  `2026.10.0`.
- **2026-10-14** — merged `upstream/main` (11 commits, `c0fb71b4..95300d05`:
  EZAIoT Lite socket thermostat, T34-Mini in `relay_switch_remote`, Dreamlight
  strips, Meaco 16K, Arlec PC193HA, Klarta Stor3 `mild`, Fisher Summer work time,
  smartplugv2 forced-polling removal, `ci(entity)` fixes) into `local-patches`: no
  conflicts, no patched file touched. Release `2026.10.0.7` also carries the
  IR2MQTT RF sub-GHz patch (#9) and the mobile API services commit.

```sh
git fetch upstream
git checkout -b local-patches-<newver> <newver-tag>
# re-apply the patches listed above, bump manifest version to <newver>.1,
# commit, tag, push, create a release
```

## 9. IR2MQTT bridge: hỗ trợ RF sub-GHz (2026.10.0.7)

**Vấn đề:** module `ir2mqtt_bridge.py` chỉ encode protocol IR (`nec/samsung/sony/raw`),
nên khi thiết bị S11 được flash về firmware OEM (dùng Tuya Local làm cầu), các mã
**RF 433 MHz** (quạt trần) không gửi được.

**Patch:**
- `custom_components/tuya_local/ir2mqtt_bridge.py`
  - phát hiện code RF qua `payload["rf"] is True` (fallback `receiver_id == "rf"`),
    gửi qua entity remote với prefix **`rf:`** (đúng đường `remote.py::_extract_codes`
    → `_encode_send_code(is_rf=True)` → DP 201 payload `rfstudy_send`);
  - IR gửi với prefix **`b64:`** (khớp `remote.py`), giữ nguyên hành vi cũ;
  - mã học được có prefix `rf:` → publish về `.../received` dạng
    `{"rf": true, "receiver_id": "rf", "timings": [...]}` (bỏ qua `_detect_protocol`).
- Sửa **bug SyntaxError** ở HEAD: `except ValueError, TypeError:` (cú pháp Python 2)
  khiến cả platform `remote` fail load; nay là `except (ValueError, TypeError):  # fmt: skip`
  (`# fmt: skip` bắt buộc vì ruff < 0.17 cắt ngoặc và tái tạo bug).
- Thêm `tests/test_ir2mqtt_bridge_rf.py` (11 test, chạy độc lập không cần HA/pytest).

**Chưa khép kín:** học RF *qua bridge* vẫn cần dấu hiệu chế độ RF (option mới
`ir2mqtt_rf` hoặc lệnh MQTT `learn_rf`); send RF đã xong.

## 10. MAC verified IP relocation (2026.10.0.8)

**Problem:** a device's LAN address is stored as a plain IP in the config entry.
When DHCP moves a device (typically after a router reboot) the entry keeps
dialling the old address and sits in `setup_retry` until the address is fixed by
hand. Patch 3 could already relocate a device *by device id*, but only when the
user pressed the button or called the service: upstream disabled the periodic
sweep in #5713 because an unreachable device could be moved to a wrong address.

**Patch:**

- `custom_components/tuya_local/macmap.py` (new)
  - remembers each device's **MAC address** in a private `Store`
    (`/config/.storage/tuya_local.device_macs`), learned from the host's ARP
    table while the device is healthy;
  - finds a device again **by MAC**: a bounded, concurrent TCP probe of the LAN
    on the Tuya port (6668) plus one ARP table read yields the whole
    `{mac: ip}` map, so a device is located even when it ignores the Tuya
    discovery broadcast -- the case the device-id relocation cannot handle;
  - rewrites the stored host only after **both identities agree** (the MAC in
    the ARP table at the candidate address *and* the device id from the
    broadcast scan), refuses an address another configured device is answering
    on, and requires the candidate to accept a connection;
  - takes the subnets from Home Assistant's own network adapters (plus the
    subnet of each configured device), so nothing is hardcoded and the same
    code runs on any install;
  - on an install where the ARP table is not visible (the container is not on
    the host network) the MAC half disables itself silently and relocation
    still happens from the broadcast alone.
- `custom_components/tuya_local/discovery.py`
  - the periodic sweep is re-enabled (60 s). It only runs when at least one
    configured device has stopped reporting state, and delegates to `macmap`,
    which closes the #5713 hazard.
- `custom_components/tuya_local/refresh.py`
  - relocation delegates to `macmap`; the service response is now
    `relocated` / `unverified` / `not_found`;
  - new `tuya_local.relocate_devices` service (relocation only: no cloud call,
    no discovery rescan) for automations.
- `custom_components/tuya_local/services.yaml` -- documents the new service.
- `tests/test_macmap.py` (new, plain Python, 19 tests) -- MAC normalisation,
  subnet selection, host enumeration, ARP parsing, probing and the
  verification rules.
- `tests/test_macmap_ha.py` (new) -- integration side: learning, relocation,
  refusals, options-host handling, probe fallback.
- `tests/test_discovery.py` -- the sweep tests now cover the delegation.

**Notes:**

- No cloud round trip and no router access: everything comes from the LAN.
- Multi-exception `except` clauses carry `# fmt: skip`: the pinned ruff (0.16.x)
  strips the parentheses, which the Python 3.14 runtime used by Home Assistant
  and CI *accepts* (PEP 758) but which is a SyntaxError on older interpreters.
## 12. Relocation on a box whose IPs changed before the patch existed (2026.10.0.10)

**Problem:** after `2026.10.0.8` was installed on a second Home Assistant box,
the devices set up *before* the router reboot stayed "Failed setup, will retry:
tuya-local device offline" and were never relocated. Two reasons, both in
patch #10:

1. **A bare TCP answer was taken as proof of identity.** For an entry with no
   device object (which is what a failed setup leaves behind) the sweep probed
   the stored address and skipped the entry when *something* answered. After a
   DHCP reshuffle the stale address is usually held by a **different** device,
   so the entry was considered healthy and never moved.
2. **The MAC record did not exist yet.** The store is filled from devices that
   are already answering, so a box that was patched *after* the addresses changed
   had nothing to match on, and if the device also ignores the Tuya discovery
   broadcast there was no evidence at all. Worse, learning ran for every entry:
   the MAC at a stale address (now another device's) could be recorded as *this*
   device's MAC, and every later lookup would then follow the wrong device.

**Patch:**

- `custom_components/tuya_local/macmap.py`
  - a third identity source: `probe_identity()` talks the Tuya protocol to every
    device answering on the Tuya port and matches the ids it is looking for, so a
    device is placed by a full handshake with **its own device id and local key**
    — no MAC record and no discovery broadcast needed. Versions are tried
    cheapest-first (the entry's own protocol version when it is known), the scan
    stops as soon as everything is found, and it is throttled
    (`PROTOCOL_SCAN_INTERVAL`, 10 min) because it is the slow check; the manual
    `relocate_devices` service bypasses the throttle.
  - `choose_candidate()` now takes `identified` and trusts the handshake first,
    then the MAC, then the broadcast;
  - an entry with **no device object and no recorded MAC** is no longer treated
    as healthy just because its address answers: it is handed to the verified
    relocation, which does nothing when the device is already where it should be;
  - `async_learn_macs()` only learns from entries whose device is *answering its
    own protocol*, so a stale address held by another device can no longer poison
    the MAC record;
  - disabled entries are skipped entirely.
- `custom_components/tuya_local/refresh.py` — the manual service forces the
  handshake probe (a button press should not be throttled).
- tests: `tests/test_macmap.py` (handshake identity, refusal on a used address)
  and `tests/test_macmap_ha.py` (relocation with no MAC record at all; learning
  refuses a MAC it cannot attribute; the handshake probe is patched out by
  default, as it opens sockets).

**Notes:** three independent identities now have to agree before a stored host is
rewritten, so the #5713 hazard stays closed. Cost is paid only while a device is
offline: one LAN sweep, and at most one handshake scan per 10 minutes.

## Branches

- ``local-patches`` (the default line, releases ``2026.10.0.11`` and up): upstream
  plus patches #1-#10 and #12.
- ``feat/multi-account``: the several-Tuya-accounts work (patch #11), released as
  ``2026.10.0.9`` / ``2026.10.0.10`` and **kept off the default line on purpose**
  until it has been in use for a while.  Merging it back brings #11 on top of the
  default line; patch #12 is already there, so only the accounts files should
  conflict.

## 13. Add every device of the account after a login (2026.10.0.12)

**Problem:** after the cloud login the devices still had to be added one at a
time (choose the device → review → submit). The login already returns the whole
device list **with each device's local key**, so that is busywork — and on a
second box with ~25 devices it is a lot of it.

**Patch:**

- `custom_components/tuya_local/bulk_add.py` (new)
  - `async_plan_devices()` classifies the whole account in one pass: one LAN scan
    (`tinytuya.deviceScan`) for the addresses the cloud does not report, one pass
    over `devices/*.yaml` to map product ids to config types, then a concurrent
    TCP check on the Tuya port so the plan can say which devices answer now;
  - a device is skipped, **with a reason**, when it is already configured, is a
    hub, has no local key, is marked cloud-only, has no known LAN address, or when
    no device config lists its product id;
  - `async_add_planned()` creates the entries through the same flow the mobile app
    uses (`source=mobile`) and reports per-device failures instead of aborting the
    batch;
  - service `tuya_local.add_all_devices` (`dry_run` default **true**,
    `include_unreachable` default true) returns the plan, or the result when
    `dry_run: false`.
- `custom_components/tuya_local/config_flow.py`
  - after a login that did not come from a discovered card, the flow lands on a
    new **`add_all`** step: the plan as one line of summary, one checkbox to add
    them all; unticking falls back to the old single-device picker.
- `services.yaml` + `translations/en.json`: the service, the step and the abort
  text.
- `tests/test_bulk_add_ha.py` (new, 6 tests): classification, LAN-address
  fallback, the unreachable filter, dry run, real creation, per-device failure.

**Notes:**

- **The address the cloud reports is the WAN address, not the LAN address.** A
  dry run on the live box showed `171.242.82.77`, `116.97.28.11`, `1.54.158.230`
  for four devices. Only a private address is ever taken from the cloud; the real
  source is the LAN scan, and a device that is not on the LAN is skipped with
  `no_host` rather than pointed at an address on the internet.
- Product matching uses `matches_product()`, **not** `matches()`: a config with no
  required DP matches any product id, which would silently pick the wrong type.
- A product id that is listed in **no** config is not a dead end: the plan marks
  the device `needs_probe` and the type is inferred from the device itself
  (DP-based, the path the mobile app uses) when the entries are created. On the
  live account 11 of 26 devices had an unlisted product id, so without this the
  feature would have added almost nothing. A device that is not answering cannot
  be asked, so it is skipped with `type_unknown`.
- A device whose type cannot be derived is **reported, never guessed** — an entry
  with the wrong type loads without the expected entities, which is worse than a
  visible skip.
- When several configs list the same product id, the first (alphabetical) is used
  and the rest are returned in `alternatives`, so a wrong pick can be spotted.
- Sub-devices behind a hub are skipped in this version: their `cid` handling is a
  separate piece of work.
- This is on the default line (one cloud account); the multi-account branch will
  take it over when merged.

## 14. A sub-device is named after itself, not after its hub (2026.10.0.14)

**Problem:** adding a Zigbee switch (a sub-device reached through its hub) offered
the **hub's** name in the "Device details" step — the name set for the switch in
the app was ignored:

> Device details — *ZemiSmart Zigbee Hub* — *ZemiSmart Zigbee Hub*
> Name: **ZemiSmart Zigbee Hub**

**Cause:** `config_flow.py::async_step_choose_device` took the *hub's* record from
the cloud list and mutated it to carry the sub-device's connection details
(`ip=""`, `device_cid`, local key, `product_id`), then assigned that same dict to
`self.__discovered_device`. The naming step
(`async_step_choose_entities`) defaults the name from
`__discovered_device["name"]`, so it read the hub's name. Mutating the hub's
record also corrupted it for every later step in the same flow (its `name` /
`product_name` were replaced and a foreign `product_id` added).

**Patch:**

- `custom_components/tuya_local/config_flow.py`
  - build a **copy** for the sub-device: the hub's connection details plus the
    sub-device's own `device_cid`, local key, `product_id`, `name` and
    `product_name` (falling back to the hub's name only when the sub-device has
    none). The hub's own record is left untouched.
- `tests/test_config_flow.py`
  - `test_flow_choose_device_indirect_keeps_the_sub_device_name`: picking
    "Công tắc phòng khách" through "ZemiSmart Zigbee Hub" must carry the switch's
    name/product name/`cid`, and must not modify the hub's record.

**Notes:** sub-devices are only offered through the cloud path (they are not on
the LAN), so this affects the "SmartLife cloud-assisted" setup only.

## 15. The add-all step no longer takes a minute (2026.10.0.15)

**Problem:** after picking "SmartLife cloud-assisted device setup" the next step
took 45 s - 1 min. Measured on the live box (a Pi 5), per plan:

| part | before |
|---|---|
| cloud device list (`manager.update_device_cache`) | ~20 s, and the flow fetched it **twice** (once to open the step, once inside the plan) |
| parse `devices/*.yaml` to map product ids to config types | 4.7 s (all ~1700 files, though only a handful matter) |
| LAN scan (`tinytuya.deviceScan`) + TCP probe | 2.0 s + 0.03 s |

So the flow was ~47 s: two cloud round trips plus the config scan.

**Patch:**

- `custom_components/tuya_local/bulk_add.py`
  - `async_cached_devices()`: the account's device list is cached for
    `_CLOUD_CACHE_TTL` (5 min) with a lock so two callers cannot fetch at once;
    `force=True` bypasses it. The cache is **warmed in the background** once the
    services are set up (only when a login exists), so the first flow that opens
    does not pay for it.
  - `async_plan_devices(..., devices=...)`: the config flow hands over the list
    it has already fetched, so the plan never fetches again.
  - `_type_index()`: reads each config as **text** first and only parses the files
    that mention the product id (`matches_product` still confirms), and keeps the
    answer in `_TYPE_CACHE` for the session. 4.7 s → ~0.1 s for the same work.
  - the type index and the LAN scan run **concurrently** (`asyncio.gather`).
- `custom_components/tuya_local/config_flow.py`
  - the three places that fetched the cloud list by hand now go through
    `async_cached_devices(hass, cloud=self.cloud)` - same instance, one fetch.
- tests: `_clear_bulk_caches` (module caches must not leak between tests) and
  `_no_lan_sockets` (the plan would otherwise open a UDP socket, which the Home
  Assistant test harness records and fails the teardown for).

**Notes:** the cache lives for the process; a device added in the app between two
fetches shows up after the TTL, or immediately with `force=True`
(`tuya_local.refresh_devices` still forces a fresh read).

## 16. Log out of the Tuya cloud, from a button (2026.10.0.16)

**Problem:** patch #1 keeps the cloud login on disk so the QR/user-code login is
not needed after every restart, but there was **no way to forget it** — switching
to another Tuya account (or clearing a login that belongs to somebody else) meant
deleting `/config/.storage/tuya_local.cloud_auth` by hand.

**Patch:**

- `custom_components/tuya_local/cloud.py`
  - `async_forget_auth(hass)`: clears the cached login **and** awaits the store
    removal, returning whether a login was there (the existing fire-and-forget
    `async_save_auth(None)` cannot report that).
- `custom_components/tuya_local/refresh.py`
  - service **`tuya_local.logout_cloud`** (response optional): restores the saved
    login first (so the answer is accurate), forgets it, and drops the cached
    cloud device names — they belong to the login that just went away;
  - returns `{"success": true, "was_authenticated": <bool>}`.
- `custom_components/tuya_local/button.py`
  - integration level button **"Logout Tuya account"**
    (`button.tuya_local_logout_tuya_account`, `mdi:logout`) next to the existing
    "Refresh device list" button, on the same "Tuya Local" service device.
- `services.yaml`: documents `logout_cloud`.
- `tests/test_logout_ha.py` (new, 5 tests): memory + disk cleared, the "no login"
  answer, configured devices untouched, cached names dropped, the button logs out.

**Notes:**

- **Device control is local and keeps working**; only the cloud side goes away
  (names for discovered cards, and the local keys offered when adding a device).
- To switch accounts: press the button, then *Add device* → SmartLife
  cloud-assisted → log in with the other account.
- The button appears once per installation (same guard as the refresh button) and
  on the "Tuya Local" service device.

## 17. Name the integration level device so it is not confused with a device (2026.10.0.17)

**Problem:** the integration level device (the one holding *Refresh device list*
and *Logout Tuya account*) was called **"Tuya Local"**, and so is the HACS device
for the repository - and so is the leftover HACS device for a repository that was
later removed (its `update` / `pre-release` switch stay behind as dead entities).
Three cards with the same name in the device list.

**Patch:** `button.py` - the integration level device is now
**"Tuya Local (integration)"**, next to "Tuya Local" for HACS and whatever the
stale entry is called.

No entity ids change (`button.tuya_local_refresh_device_list`,
`button.tuya_local_logout_tuya_account`), only the device name.

## 18. Take the integration level buttons off a device (2026.10.0.18)

**Problem:** Home Assistant attaches every device to the config entry whose setup
created it. The integration level buttons (patch #2, #16) were created by whichever
entry loaded the button platform first, so on
*Settings → Devices & Services → Tuya Local* a device row **"Tuya Local"
appeared inside that real device's entry** - next to the device itself, which reads
like a device that belongs to it. Renaming the device (patch #17) does not help:
the grouping is by config entry, not by name.

**Patch:** `button.py` - `TuyaLocalRefreshButton` and `TuyaLocalCloudLogoutButton`
no longer declare `device_info`, so they are plain entities of the integration and
no device row is created.

- `_attr_has_entity_name` removed and the names spelled out
  (`"Tuya Local Refresh device list"`, `"Tuya Local Logout Tuya account"`), so the
  entity ids stay **`button.tuya_local_refresh_device_list`** and
  **`button.tuya_local_logout_tuya_account`** - automations and dashboards that
  already reference them keep working.
- The services (`tuya_local.refresh_devices`, `tuya_local.logout_cloud`) are the
  other way to trigger the same things.
- `tests/test_logout_ha.py`: asserts both buttons have `device_info is None`.

**Cleanup on an existing install:** the device created by earlier versions
(`identifiers {(tuya_local, hub)}`) stays in the registry with no entities after
the upgrade. Delete it once in *Settings → Devices* (it shows "0 entities"), or
remove it from the device registry.

## 19. Allow deleting a device that has no entities left (2026.10.0.19)

**Problem:** after patch #18 the buttons are plain entities, so the device they
used to live on is left behind with **0 entities**. Home Assistant refuses to
remove it from the config entry ("Config entry does not support device removal")
because the integration does not implement the optional removal hook, so the empty
"Tuya Local" row stayed inside a real device's entry on the integrations page.

**Patch:** `__init__.py` - `async_remove_config_entry_device` returns True only
when the device has no entities at all (disabled included). With the hook in
place, *Settings → Devices → the empty device → Delete* works, and the empty
device can also be removed through the websocket API
(`config/device_registry/remove_config_entry`).

The guard means the button can never strip entities off a working device: any
device that still has an entity is not removable.

## 20. Give the integration level buttons their own config entry (2026.10.0.20)

**Problem:** patches #18 / #19 removed the confusing device row by removing the
device, which also removed the thing the buttons were found under. Home Assistant
requires every device to belong to a config entry (`async_get_or_create` takes
`config_entry_id` as a required argument), and this integration has one entry per
device, so a shared device inevitably lands inside a real device's entry.

**Patch:** the integration now creates **one service entry** (title "Tuya Local",
data `{service_entry: true}`, no device connection) that exists only to own the
device holding the two buttons.

- `const.py`: `CONF_SERVICE_ENTRY`.
- `__init__.py`:
  - `is_service_entry()` helper;
  - `async_ensure_service_entry()` creates the entry once, guarded, from the first
    real entry's setup (an existing service entry is never duplicated);
  - `async_setup_entry()` forwards only the `button` platform for it, and skips all
    device connection logic;
  - `async_unload_entry()` unloads the platform for it;
  - the "last device removed, stop discovery" check ignores the service entry.
- `button.py`: the service entry adds exactly the two integration buttons; device
  entries no longer do (the "created once from whichever entry loads first" hack
  is gone). Both buttons declare `device_info` again: identifiers
  `{(tuya_local, hub)}`, name "Tuya Local", `entry_type=service`.
- `mobile_api.py`: `list_configured` skips the service entry (it is not a device).

**Result on the integrations page:** an entry card "Tuya Local" whose only device
row is "Tuya Local" with the two buttons - separate from every real device's entry.
Entity ids are unchanged: `button.tuya_local_refresh_device_list`,
`button.tuya_local_logout_tuya_account`.

Patches #18 / #19 stay: an empty leftover device is still removable, and the strict
`async_remove_config_entry_device` guard remains.

## 21. Show each device's LAN IP and MAC as diagnostic sensors (2026.10.0.21)

**Request:** every tuya_local device should also show its LAN address and its MAC
address.

**Patch:**

- `sensor.py`
  - `TuyaLocalIpAddressSensor` ("IP address", `mdi:ip-network`) reads the host from
    **the config entry it is attached to**, so it follows a device that was
    relocated to a new address (patch #5 updates the entry);
  - `TuyaLocalMacAddressSensor` ("MAC address", `mdi:identifier`) prefers the MAC
    remembered by `macmap` (learned and verified in patch #5), and falls back to
    the ARP table entry for the device's current host;
  - both are `EntityCategory.DIAGNOSTIC`, so they appear in the device page under
    "Diagnostics" and do not clutter the main entity list;
  - `async_setup_entry` now always adds them, even for devices without sensor dps
    (it used to raise `ValueError` for those), and still raises when the device
    config itself is missing.
- `__init__.py`: `"sensor"` is always forwarded for a device entry, since devices
  without sensor dps would never reach the sensor platform.

**Notes:**

- values are polled (30 s default), so the MAC appears once discovery learns it;
- the MAC is **not** added as a device connection: that would make Home Assistant
  merge this device with another integration's device declaring the same MAC.

Tests: `tests/test_lan_sensors.py` (new, 4) plus updated `tests/test_sensor.py`.

## 22. Group A diagnostics: link state, timing, protocol, errors (2026.10.0.22)

**Request:** more per device information than IP and MAC.

**Patch - `device.py`** (tracking, all cheap and local):

- `_last_response_ms`: duration of the last successful poll, measured around the
  executor call;
- `_last_error`: `{code, message, at}` of the last failure reported by the device
  (the codes were only logged before, e.g. 901/914), cleared on the next success;
- properties `last_error`, `last_seen` (`updated_at` of the cached state),
  `response_time_ms`, `protocol_version` (the negotiated tinytuya version),
  `dp_count`, `parent_id` / `node_id` (from `<parent>/<node>` unique ids).

**Patch - new entities, one set per device, all `EntityCategory.DIAGNOSTIC`:**

| Entity | Source |
|---|---|
| `sensor.<device>_last_seen` | when data was last received (timestamp) |
| `sensor.<device>_response_time` | ms of the last poll |
| `sensor.<device>_protocol_version` | 3.1 / 3.3 / 3.4 actually in use |
| `sensor.<device>_datapoints` | how many dps the device reports |
| `sensor.<device>_last_error` | last error code, message + time as attributes |
| `sensor.<device>_parent_hub` | parent id for Zigbee children, node id attribute |
| `sensor.<device>_config_type` | the device config file in use |
| `binary_sensor.<device>_connected` | whether the LAN link is alive |

`__init__.py` now also always forwards `"binary_sensor"`; both platforms skip their
normal setup when the device has no matching dps and still raise when the device
config is missing.

**Not added, on purpose:** local_key / full device id (sensitive), MAC as a device
connection (would merge devices across integrations), Wi-Fi RSSI (the Tuya LAN
protocol does not report it).

Tests: `tests/test_lan_sensors.py` (+6), `tests/test_sensor.py`,
`tests/test_binary_sensor.py`.

## 23. Device id, local key, cloud details, and a scheduled cloud refresh (2026.10.0.23)

**Request:** also show the device id and local key, plus group B (cloud details),
with the cloud part optimised: reuse the cached list and fetch it in the background
every 15 minutes, configurable from an entity.

**New per device diagnostics:**

| Entity | Source |
|---|---|
| `sensor.<device>_device_id` | the Tuya device id from the config entry |
| `sensor.<device>_local_key` | the local key from the config entry (**sensitive**) |
| `sensor.<device>_product_id` | cached cloud list |
| `sensor.<device>_category` | cached cloud list |
| `sensor.<device>_cloud_name` | name set in the Tuya/SmartLife app |
| `binary_sensor.<device>_cloud_online` | `online` flag from the cloud list |

**Cloud part is cache only.** The entity path never calls the cloud: group B reads
the account device list that `bulk_add.async_cached_devices()` already keeps (one
fetch, 5 minute TTL, shared with the add-all flow). `bulk_add.peek_cached_devices()`
/ `find_cached_device(device_id, cid)` expose it read-only, and a sub device is
matched by `<hub id>/<node id>` so its own record wins over the hub's.

**New module `cloud_refresh.py`:**

- a background task refreshes the list on an interval, and the same payload also
  fills the device name cache, so one cloud call serves both;
- the interval is persisted in its own store (default **15** minutes, `0` disables
  the task, clamped to 0…1440);
- started from the first device entry, stopped when the last one is unloaded;
- failures are logged and never kill the task.

**New entity on the service entry device:** `number.tuya_local_cloud_refresh_interval`
("Cloud refresh interval", `mdi:timer-sync-outline`, unit `min`, step 5, config
category). `__init__` now forwards the `number` platform for the service entry as
well as `button`.

**Security note:** `local_key` is a credential. It is shown because it was asked for;
the state also lands in the recorder database, so disable that entity (or exclude the
domain from the recorder) if history should not keep it.

Tests: `tests/test_lan_sensors.py` (+6), `tests/test_sensor.py`,
`tests/test_binary_sensor.py`.

## 24. Device type: smarter auto-add pick, and a way to change it later (2026.10.0.24)

**Problem:** auto-add had to choose a device config (the `type`) for every device it
creates, and the type used to be fixed for the life of the entry:

- a device is matched by `matches_product(product_id)` against each config's
  `products:` list;
- when several configs list the same product, the **first alphabetically** was
  taken and the rest only reported as `alternatives` - a wrong pick could only be
  fixed by deleting the entry and adding the device again.

**Patch - smarter pick (`bulk_add`):**

- if more than one config lists the product id, the plan now **asks the device**
  (`_infer_type` -> `async_inferred_type()`, DP quality scoring, the path the mobile
  app uses) and takes that type when it is one of the candidates;
- `type_source` says where the type came from (`product` or `probe`);
- the remaining candidates stay in `alternatives` either way.

**Patch - change it afterwards (`config_flow` options flow):**

- the device's **Configure** dialog now has a **Device type** dropdown;
- the types that list the device's product id come first (labelled "khớp product
  id"), the current one is labelled "đang dùng", everything else follows;
- the choice is stored in the entry *options*, so it wins over the original data
  and the entry reloads with the new config;
- the connection is tested with the new type before saving, so a type that does not
  fit the device is rejected like in the add flow.

**Note:** changing the type replaces the entities. Registry entries for entities the
new config does not have stay behind as unavailable and can be deleted.

Tests: `tests/test_config_flow.py` (options flow now carries the type, plus a type
change test), `tests/test_bulk_add_ha.py` (+2 plan tests).

## 25. Find a device type: search instead of one huge dropdown (2026.10.0.25)

**Problem:** patch #24 listed all ~1700 bundled templates in the options "Device
type" dropdown, which is unusable to browse.

**Patch - `config_flow` options flow:**

- the main form's **Device type** list now only contains the templates that list
  the device's product id (A–Z), the type currently in use, and a last entry
  **"🔍 Tìm template khác (danh sách đầy đủ)"**;
- picking that entry opens **step `type_search`**: a search box that filters the
  full template list by substring (case insensitive) and sorts it A–Z, capped at
  150 results with the total count in the description;
- then **step `type_choose`** lists the matches A–Z; choosing one goes through the
  same connection test as before, and a type that does not connect returns to that
  list with the error instead of losing the flow;
- "no template matches that search" is reported on the search step.

**Patch - translations:** `options.step.user.data.type`, the new
`options.step.type_search` / `type_choose` titles + field labels and
`options.error.no_matches` in `en.json` (other languages fall back to English).

Tests: `tests/test_config_flow.py` - the search flow end to end, the "no matches"
case, and the main form keeping the current type.

## 26. Type search: a text box in the form, not a dropdown trick (2026.10.0.26)

**Problem:** #25 added a "🔍 Tìm template khác" entry to the type dropdown and jumped
to a search step when it was picked. Picking a dropdown value does not submit the
form in Home Assistant, so choosing it looked like nothing happened - the request
never even reached the backend (nothing in the log).

**Patch - `config_flow` options flow:** the extra steps are gone; the search is a
normal field on the Configure form:

- `type_search` (optional text field, label "🔍 Find another template (type part of
  a name, then press Submit to filter the list above)");
- submitting with something typed filters the full template list (A–Z, 150 max),
  keeps everything else the user filled in, and shows the form again with the
  filtered templates added to the **Device type** dropdown; the description says how
  many matched;
- an empty search saves the options as before ("nothing to do" is a plain Submit);
- no match shows the `no_matches` error and keeps the previous list.

The dropdown therefore only ever holds the product-id matches plus the current
search results - never ~1700 entries.

Tests: `tests/test_config_flow.py` - the search filters and then saves, and the
no-match case.

## 27. Type list: only the templates that fit the device (2026.10.0.27)

**Request:** drop the search box; just list the templates that suit the device, like
the add-device flow does.

**Patch - `config_flow` options flow:**

- `type_search` field, `_type_search` / `_type_results` state and `_async_all_types`
  are gone; the form is the plain Configure form again;
- the type list is built the same way the add flow builds it:
  `device.async_possible_types()` (the running device's dps + product ids, scored by
  `match_quality`, 20 s timeout) gives the candidates, labelled
  `"<manufacturer> <model> (<config>)"` and sorted best match first;
- the type in use is always first, labelled "(đang dùng)";
- if the device does not answer, it falls back to the product-id index in
  `bulk_add` (no connection needed), and an empty list keeps the current type so the
  form is never impossible to submit;
- `translations/en.json`: the search field label, the `{search_hint}` placeholder and
  the `no_matches` error are removed.

Tests: `tests/test_config_flow.py` - the list holds the current type plus the
device's candidates, and the form has no `type_search` field.

## 28. Type list resolves the running sub device (2026.10.0.28)

**Problem:** for a Zigbee child, the Configure dialog offered only one type (the
current one). `hass.data[DOMAIN]` keys a sub device as **`<hub id>/<node id>`**, but
the options flow looked it up by `CONF_DEVICE_ID` (the hub id alone), found nothing,
and fell back to the product-id index - which only knows the hub's product.

**Patch - `config_flow` options flow:** `_async_type_options()` resolves the runtime
key with `get_device_id(config)` (which appends the cid), so the real device object
is used and its `async_possible_types()` candidates are listed. The offline fallback
still queries `bulk_add` with the bare device id + cid.

Tests: `tests/test_config_flow.py` - a sub device keyed `hubid/nodeid` now yields its
runner's candidate list.
