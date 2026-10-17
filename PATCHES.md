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
