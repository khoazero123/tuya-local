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

## 3. Local device config: Zitech ZT-Box

- `custom_components/tuya_local/devices/zitech_ztbox.yaml` — added so a HACS
  update cannot delete it (it previously lived only in `/config`).

## Re-basing after an upstream update

```sh
git fetch upstream
git checkout -b local-patches-<newver> <newver-tag>
# re-apply the patches listed above, bump manifest version to <newver>.1,
# commit, tag, push, create a release
```
