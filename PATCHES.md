# Local patches in this fork

This fork is based on `make-all/tuya-local` and adds one local patch on the
`token-persistence` branch (see `git log` for details).

## 1. Persist the Tuya cloud login to disk

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

**Notes:**
- Only the cloud-assisted setup path is affected; device control stays 100%
  local (`iot_class: local_push`).
- If Tuya rejects the saved refresh token, the flow falls back to the normal
  user code + QR login.
- The token file contains Tuya access/refresh tokens, so treat
  `/config/.storage/tuya_local.cloud_auth` as a secret (it is included in HA
  backups).

## Re-basing after an upstream update

```sh
git fetch upstream
git checkout -b token-persistence-<newver> <newver-tag>
# re-apply the two edits (see PATCHES.md above), bump manifest version
# e.g. <newver>.1, commit, tag, push, create a release
```
