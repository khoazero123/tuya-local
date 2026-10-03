# Local patches in this fork

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

## Re-basing after an upstream update

```sh
git fetch upstream
git checkout -b local-patches-<newver> <newver-tag>
# re-apply the patches listed above, bump manifest version to <newver>.1,
# commit, tag, push, create a release
```
