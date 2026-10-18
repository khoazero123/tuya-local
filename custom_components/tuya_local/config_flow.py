import asyncio
import logging
from collections import OrderedDict
from typing import Any

import tinytuya
import voluptuous as vol
from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_PUSH,
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import slugify

from . import DOMAIN
from .bulk_add import async_cached_devices
from .cloud import Cloud, async_restore_auth
from .const import (
    API_PROTOCOL_VERSIONS,
    CONF_DEVICE_CID,
    CONF_DEVICE_ID,
    CONF_IR2MQTT_BRIDGE,
    CONF_LOCAL_KEY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_POLL_ONLY,
    CONF_PROTOCOL_VERSION,
    CONF_SERVICE_ENTRY,
    CONF_TYPE,
    CONF_USER_CODE,
    DATA_STORE,
    SERVICE_ENTRY_TITLE,
)
from .device import TuyaLocalDevice
from .helpers.config import get_device_id
from .helpers.device_config import get_config, product_display_name
from .helpers.log import log_json

_LOGGER = logging.getLogger(__name__)

IR2MQTT_BRIDGE_OFF = "(tắt)"

DEVICE_DETAILS_URL = (
    "https://github.com/make-all/tuya-local/blob/main/DEVICE_DETAILS.md"
    "#finding-your-device-id-and-local-key"
)


class ConfigFlowHandler(ConfigFlow, domain=DOMAIN):
    VERSION = 13
    MINOR_VERSION = 24

    async def async_step_import(self, import_config=None):
        """Create the entry that owns the integration level buttons.

        Started by the integration itself (PATCHES.md #20): the two integration
        level buttons need a device, Home Assistant ties every device to a config
        entry, and this integration has one entry per device.
        """
        if not import_config or not import_config.get(CONF_SERVICE_ENTRY):
            return self.async_abort(reason="import_failed")
        return self.async_create_entry(
            title=SERVICE_ENTRY_TITLE, data=dict(import_config)
        )

    CONNECTION_CLASS = CONN_CLASS_LOCAL_PUSH
    device = None
    data = {}

    __qr_code: str | None = None
    __cloud_devices: dict[str, Any] = {}
    __discovered_device: dict[str, Any] | None = None
    __bulk_plan: dict[str, Any] | None = None

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.cloud = None
        self.__bulk_plan = None

    def init_cloud(self):
        if self.cloud is None:
            self.cloud = Cloud(self.hass)

    async def async_step_integration_discovery(self, discovery_info):
        """Handle a device found on the LAN by the background scanner.

        Pre-fills the manual setup form with the discovered id/ip/version; the
        user still supplies the local key. Aborts if the device is already
        configured or has been ignored.
        """
        device_id = discovery_info.get(CONF_DEVICE_ID)
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()
        # Reuse the cloud-device plumbing that async_step_local reads for its
        # form defaults; the local key is not known from discovery.
        self.__discovered_device = {
            "id": device_id,
            "ip": discovery_info.get(CONF_HOST),
            "version": discovery_info.get("version"),
            "local_product_id": discovery_info.get("product_id"),
            CONF_LOCAL_KEY: "",
        }
        # Label the Discovered card with the name set in the Tuya/SmartLife app
        # when the cloud list is cached, otherwise with the device type looked
        # up from the product id, plus the IP (an IP alone makes it impossible
        # to tell which device is which).
        label = discovery_info.get("name")
        if not label:
            # Prefer a config already used for the same product id: a product id
            # alone can match several device configs, and the one the user
            # already runs is the best guess for this one.
            preferred = discovery_info.get("preferred_type")
            if preferred:
                preferred_config = await self.hass.async_add_executor_job(
                    get_config, preferred
                )
                if preferred_config is not None:
                    label = preferred_config.name
        if not label:
            label = await self.hass.async_add_executor_job(
                product_display_name, discovery_info.get("product_id")
            )
        self.context["title_placeholders"] = {
            "name": " · ".join(
                part for part in (label, discovery_info.get(CONF_HOST)) if part
            )
            or device_id
        }

        # The device is already known from the LAN scan, so go straight to the
        # device details instead of showing the setup-mode form again (which
        # made this look identical to the bulk "Add device" flow).  A saved
        # cloud login is used to fill in the local key when one is available,
        # so make sure the saved login is loaded first.
        await async_restore_auth(self.hass)
        self.init_cloud()
        try:
            if self.cloud.is_authenticated:
                self.__cloud_devices = await async_cached_devices(
                    self.hass, cloud=self.cloud
                )
                return await self._async_continue_after_login()
        except Exception as e:  # noqa: BLE001 - fall back to the manual form
            _LOGGER.warning("Could not use the saved Tuya cloud login: %s", e)
        return await self.async_step_local()

    async def async_step_mobile(self, user_input=None):
        """Programmatic entry from the Smarthome mobile ``add_device`` service.

        Expects a fully populated dict (device_id, host, local_key, type, …).
        """
        if user_input is None:
            return self.async_abort(reason="missing_data")

        device_id = user_input.get(CONF_DEVICE_ID)
        if not device_id:
            return self.async_abort(reason="missing_device_id")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        title = user_input.get(CONF_NAME) or f"Tuya {device_id[-6:]}"
        data = {
            CONF_DEVICE_ID: device_id,
            CONF_HOST: user_input[CONF_HOST],
            CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY],
            CONF_PROTOCOL_VERSION: user_input.get(CONF_PROTOCOL_VERSION, 3.3),
            CONF_POLL_ONLY: user_input.get(CONF_POLL_ONLY, False),
            CONF_TYPE: user_input[CONF_TYPE],
        }
        if user_input.get(CONF_DEVICE_CID):
            data[CONF_DEVICE_CID] = user_input[CONF_DEVICE_CID]
        return self.async_create_entry(title=title, data=data)

    async def async_step_user(self, user_input=None):
        errors = {}

        if self.hass.data.get(DOMAIN) is None:
            self.hass.data[DOMAIN] = {}
        if self.hass.data[DOMAIN].get(DATA_STORE) is None:
            self.hass.data[DOMAIN][DATA_STORE] = {}

        # Local patch: load a previously saved cloud login from disk so that
        # adding a device after a restart does not need a new QR/user-code login.
        await async_restore_auth(self.hass)

        if user_input is not None:
            mode = user_input.get("setup_mode")
            if mode == "cloud" or mode == "cloud_fresh_login":
                self.init_cloud()
                try:
                    if mode == "cloud_fresh_login":
                        # Force a fresh login
                        self.cloud.logout()

                    if self.cloud.is_authenticated:
                        self.__cloud_devices = await async_cached_devices(
                            self.hass, cloud=self.cloud
                        )
                        return await self._async_continue_after_login()
                except Exception as e:
                    # Re-authentication is needed.
                    _LOGGER.warning("Connection test failed with %s %s", type(e), e)
                    _LOGGER.warning("Re-authentication is required.")
                return await self.async_step_cloud()
            if mode == "manual":
                return await self.async_step_local()

        # Build form
        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("setup_mode")] = SelectSelector(
            SelectSelectorConfig(
                options=["cloud", "manual", "cloud_fresh_login"],
                mode=SelectSelectorMode.LIST,
                translation_key="setup_mode",
            )
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step user."""
        errors = {}
        placeholders = {}
        self.init_cloud()

        if user_input is not None:
            response = await self.cloud.async_get_qr_code(user_input[CONF_USER_CODE])
            if response:
                self.__qr_code = response
                return await self.async_step_scan()

            errors["base"] = "login_error"
            placeholders = self.cloud.last_error

        else:
            user_input = {}

        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USER_CODE, default=user_input.get(CONF_USER_CODE, "")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step scan."""
        if user_input is None:
            return self.async_show_form(
                step_id="scan",
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
            )
        self.init_cloud()
        if not await self.cloud.async_login():
            # Try to get a new QR code on failure
            response = await self.cloud.async_get_qr_code()
            errors = {"base": "login_error"}
            placeholders = self.cloud.last_error
            if response:
                self.__qr_code = response

            return self.async_show_form(
                step_id="scan",
                errors=errors,
                data_schema=vol.Schema(
                    {
                        vol.Optional("QR"): QrCodeSelector(
                            config=QrCodeSelectorConfig(
                                data=f"tuyaSmart--qrLogin?token={self.__qr_code}",
                                scale=5,
                                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                            )
                        )
                    }
                ),
                description_placeholders=placeholders,
            )

        self.__cloud_devices = await async_cached_devices(self.hass, cloud=self.cloud)
        return await self._async_continue_after_login()

    async def _async_continue_after_login(self) -> FlowResult:
        """Continue after a cloud login.

        When the flow was started from a LAN-discovered device, use that device
        and skip the cloud device list, so the user is not asked to choose the
        device a second time (the shortcut previously only existed on the QR
        scan path, so a cached login sent the user back to "Choose the device
        to add").
        """
        if self.__discovered_device:
            # If local discovery already found a device, we can skip the choose
            # device step after updating discovery_info.
            device_choice = self.__cloud_devices.get(self.__discovered_device["id"])
            if device_choice:
                self.__discovered_device[CONF_LOCAL_KEY] = device_choice.get(
                    CONF_LOCAL_KEY
                )
                self.__discovered_device["product_id"] = device_choice.get("product_id")
                self.__discovered_device["product_name"] = device_choice.get(
                    "product_name"
                )
            return await self.async_step_local()
        return await self.async_step_add_all()

    async def async_step_add_all(self, user_input=None):
        """Offer to add every device on the account, straight after a login.

        The cloud list already carries each device's local key, so picking them
        one at a time is busywork.  The plan says what can be added and why the
        rest cannot; one submit adds them all, and declining falls back to the
        single-device picker.
        """
        from .bulk_add import async_add_planned, async_plan_devices

        if user_input is None:
            plan = await async_plan_devices(self.hass, devices=self.__cloud_devices)
            self.__bulk_plan = plan
            reasons = {}
            for item in plan["skip"]:
                reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
            detail = ", ".join(
                f"{reason} ×{count}" for reason, count in sorted(reasons.items())
            )
            summary = f"{len(plan['add'])}/{plan['found']} device(s) can be added"
            if detail:
                summary = f"{summary} — skipped: {detail}"
            if not plan["add"]:
                # Nothing can be added automatically (no cloud login, nothing
                # addable, or nothing left to add): fall back to the picker.
                return await self.async_step_choose_device()
            return self.async_show_form(
                step_id="add_all",
                data_schema=vol.Schema({vol.Required("add_all", default=True): bool}),
                description_placeholders={"summary": summary},
            )

        if not user_input.get("add_all"):
            return await self.async_step_choose_device()

        plan = self.__bulk_plan or await async_plan_devices(
            self.hass, devices=self.__cloud_devices
        )
        result = await async_add_planned(self.hass, plan)
        return self.async_abort(
            reason="devices_added",
            description_placeholders={
                "added": str(len(result["added"])),
                "failed": str(len(result["failed"])),
            },
        )

    async def async_step_choose_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            device_choice = self.__cloud_devices[user_input["device_id"]]

            if device_choice["ip"] != "":
                # This is a directly addable device.
                if user_input["hub_id"] == "None":
                    device_choice["ip"] = ""
                    self.__discovered_device = device_choice
                    return await self.async_step_search()
                else:
                    # Show error if user selected a hub.
                    errors["base"] = "does_not_need_hub"
                    # Fall through to reshow the form.
            else:
                # This is an indirectly addressable device. Need to know which hub it is connected to.
                if user_input["hub_id"] != "None":
                    hub_choice = self.__cloud_devices[user_input["hub_id"]]
                    # The entry is created for the *sub-device*, reached through
                    # the hub: take the connection details from the hub, but the
                    # sub-device's own identity (cid, local key, product id) and
                    # its own name - otherwise the name offered in the next step
                    # is the hub's, not the name the user gave the switch.
                    # A copy, so the hub's own record keeps its name and product
                    # id for a later attempt in the same flow.
                    sub_device = {
                        **hub_choice,
                        "ip": "",
                        CONF_DEVICE_CID: (
                            device_choice["node_id"] or device_choice["uuid"]
                        ),
                        "product_id": device_choice["product_id"],
                        "name": device_choice.get("name") or hub_choice.get("name"),
                        "product_name": device_choice.get("product_name")
                        or hub_choice.get("product_name"),
                    }
                    if device_choice.get(CONF_LOCAL_KEY):
                        sub_device[CONF_LOCAL_KEY] = device_choice[CONF_LOCAL_KEY]
                    self.__discovered_device = sub_device
                    return await self.async_step_search()
                else:
                    # Show error if user did not select a hub.
                    errors["base"] = "needs_hub"
                    # Fall through to reshow the form.

        device_list = []
        for key in self.__cloud_devices.keys():
            device_entry = self.__cloud_devices[key]
            if device_entry.get("exists"):
                continue
            if device_entry[CONF_LOCAL_KEY] != "":
                if device_entry["online"]:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']})",
                        )
                    )
                else:
                    device_list.append(
                        SelectOptionDict(
                            value=key,
                            label=f"{device_entry['name']} ({device_entry['product_name']}) OFFLINE",
                        )
                    )

        _LOGGER.debug(f"Device count: {len(device_list)}")
        if len(device_list) == 0:
            return self.async_abort(reason="no_devices")

        device_selector = SelectSelector(
            SelectSelectorConfig(options=device_list, mode=SelectSelectorMode.DROPDOWN)
        )

        hub_list = []
        hub_list.append(SelectOptionDict(value="None", label="None"))
        for key in self.__cloud_devices.keys():
            hub_entry = self.__cloud_devices[key]
            if hub_entry["is_hub"]:
                hub_list.append(
                    SelectOptionDict(
                        value=key,
                        label=f"{hub_entry['name']} ({hub_entry['product_name']})",
                    )
                )

        _LOGGER.debug(f"Hub count: {len(hub_list) - 1}")

        hub_selector = SelectSelector(
            SelectSelectorConfig(options=hub_list, mode=SelectSelectorMode.DROPDOWN)
        )

        # Build form
        fields: OrderedDict[vol.Marker, Any] = OrderedDict()
        fields[vol.Required("device_id")] = device_selector
        fields[vol.Required("hub_id")] = hub_selector

        return self.async_show_form(
            step_id="choose_device",
            data_schema=vol.Schema(fields),
            errors=errors or {},
            last_step=False,
        )

    @property
    def _device_name_placeholder(self) -> str:
        """Return device name placeholder for step descriptions."""
        if self.__discovered_device and self.__discovered_device.get("product_name"):
            parts = []
            if self.__discovered_device.get("name"):
                parts.append(self.__discovered_device["name"])
            parts.append(self.__discovered_device["product_name"])
            return "**" + " — ".join(parts) + "**\n\n"
        return ""

    async def async_step_search(self, user_input=None):
        if user_input is not None:
            # Current IP is the WAN IP which is of no use. Need to try and discover to the local IP.
            # This scan will take 18s with the default settings. If we cannot find the device we
            # will just leave the IP address blank and hope the user can discover the IP by other
            # means such as router device IP assignments.
            _LOGGER.debug(
                f"Scanning network to get IP address for {self.__discovered_device.get('id', 'DEVICE_KEY_UNAVAILABLE')}."
            )
            self.__discovered_device["ip"] = ""
            try:
                local_device = await self.hass.async_add_executor_job(
                    scan_for_device, self.__discovered_device.get("id")
                )
            except OSError:
                local_device = {"ip": None, "version": ""}

            if local_device.get("ip"):
                _LOGGER.debug(f"Found: {local_device}")
                self.__discovered_device["ip"] = local_device.get("ip")
                self.__discovered_device["version"] = local_device.get("version")
                if not self.__discovered_device.get(CONF_DEVICE_CID):
                    self.__discovered_device["local_product_id"] = local_device.get(
                        "productKey"
                    )
            else:
                _LOGGER.warning(
                    f"Could not find device: {self.__discovered_device.get('id', 'DEVICE_KEY_UNAVAILABLE')}"
                )
            return await self.async_step_local()

        return self.async_show_form(
            step_id="search",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
            errors={},
            last_step=False,
        )

    async def async_step_local(self, user_input=None):
        errors = {}
        devid_opts = {}
        host_opts = {"default": ""}
        key_opts = {}
        proto_opts = {"default": "auto"}
        polling_opts = {"default": False}
        devcid_opts = {}

        if self.__discovered_device is not None:
            # We already have some or all of the device settings from the cloud flow. Set them into the defaults.
            devid_opts = {"default": self.__discovered_device.get("id")}
            host_opts = {"default": self.__discovered_device.get("ip")}
            key_opts = {"default": self.__discovered_device.get(CONF_LOCAL_KEY)}
            if self.__discovered_device.get("version"):
                proto_opts = {"default": str(self.__discovered_device.get("version"))}
            if self.__discovered_device.get(CONF_DEVICE_CID):
                devcid_opts = {"default": self.__discovered_device.get(CONF_DEVICE_CID)}

        if user_input is not None:
            proto = user_input.get(CONF_PROTOCOL_VERSION)
            if proto != "auto":
                user_input[CONF_PROTOCOL_VERSION] = float(proto)
            self.device = await async_test_connection(user_input, self.hass)
            if self.device:
                self.data = user_input
                # If auto mode found a working protocol, save it so future
                # HA restarts connect directly without re-cycling all versions.
                self._auto_detected_protocol = None
                if (
                    user_input.get(CONF_PROTOCOL_VERSION) == "auto"
                    and self.device._protocol_configured != "auto"
                ):
                    self._auto_detected_protocol = self.device._protocol_configured
                    self.data = {
                        **self.data,
                        CONF_PROTOCOL_VERSION: self._auto_detected_protocol,
                    }
                if self.__discovered_device:
                    if self.__discovered_device.get("product_id"):
                        self.device.set_detected_product_id(
                            self.__discovered_device.get("product_id")
                        )
                    if self.__discovered_device.get("local_product_id"):
                        self.device.set_detected_product_id(
                            self.__discovered_device.get("local_product_id")
                        )
                await self.async_set_unique_id(
                    get_device_id(user_input), raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_select_type()
            else:
                errors["base"] = "connection"
                devid_opts["default"] = user_input[CONF_DEVICE_ID]
                host_opts["default"] = user_input[CONF_HOST]
                key_opts["default"] = user_input[CONF_LOCAL_KEY]
                if CONF_DEVICE_CID in user_input:
                    devcid_opts["default"] = user_input[CONF_DEVICE_CID]
                proto_opts["default"] = str(user_input[CONF_PROTOCOL_VERSION])
                polling_opts["default"] = user_input[CONF_POLL_ONLY]

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID, **devid_opts): str,
                    vol.Required(CONF_HOST, **host_opts): str,
                    vol.Required(CONF_LOCAL_KEY, **key_opts): str,
                    vol.Required(
                        CONF_PROTOCOL_VERSION,
                        **proto_opts,
                    ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
                    vol.Required(CONF_POLL_ONLY, **polling_opts): bool,
                    vol.Optional(CONF_DEVICE_CID, **devcid_opts): str,
                }
            ),
            description_placeholders={
                "device_details_url": DEVICE_DETAILS_URL,
                "device_name": self._device_name_placeholder,
            },
            errors=errors,
        )

    async def async_step_select_type(self, user_input=None):
        if user_input is not None:
            # Value is a compound key: "config_type||manufacturer||model"
            parts = user_input[CONF_TYPE].split("||", 2)
            self.data[CONF_TYPE] = parts[0]
            if len(parts) > 1 and parts[1]:
                self.data[CONF_MANUFACTURER] = parts[1]
            if len(parts) > 2 and parts[2]:
                self.data[CONF_MODEL] = parts[2]
            return await self.async_step_choose_entities()

        all_matches = []
        best_match = 0
        best_matching_type = None
        best_matching_key = None

        for dev_type in await self.device.async_possible_types():
            q = dev_type.match_quality(
                self.device._get_cached_state(),
                self.device._product_ids,
            )
            for manufacturer, model in dev_type.product_display_entries(
                self.device._product_ids
            ):
                key = f"{dev_type.config_type}||{manufacturer or ''}||{model or ''}"
                parts = [p for p in [manufacturer, model] if p]
                if parts:
                    label = f"{' '.join(parts)} ({dev_type.config_type})"
                else:
                    label = f"{dev_type.name} ({dev_type.config_type})"
                all_matches.append((SelectOptionDict(value=key, label=label), q))
                if q > best_match:
                    best_match = q
                    best_matching_type = dev_type.config_type
                    best_matching_key = key

        all_matches.sort(key=lambda x: x[1], reverse=True)
        type_options = [opt for opt, _ in all_matches]

        best_match = int(best_match)
        dps = self.device._get_cached_state()
        if self.__discovered_device:
            _LOGGER.warning(
                "Adding %s device with product id %s",
                self.__discovered_device.get("product_name", "UNKNOWN"),
                self.__discovered_device.get("product_id", "UNKNOWN"),
            )
            if self.__discovered_device.get(
                "local_product_id"
            ) and self.__discovered_device.get(
                "local_product_id"
            ) != self.__discovered_device.get("product_id"):
                _LOGGER.warning(
                    "Local product id differs from cloud: %s",
                    self.__discovered_device.get("local_product_id"),
                )
            # Don't log the partial cloud spec. It confuses users into
            # thinking they do not need to download the full Query Things Data Model
            # result.
            #
            # try:
            #     self.init_cloud()
            #     model = await self.cloud.async_get_datamodel(
            #         self.__discovered_device.get("id"),
            #     )
            #     if model:
            #         _LOGGER.warning(
            #             "Partial cloud device spec:\n%s",
            #             log_json(model),
            #         )
            # except Exception as e:
            #     _LOGGER.warning(
            #         "Unable to fetch data model from cloud: %s %s",
            #         type(e).__name__,
            #         e,
            #     )
        _LOGGER.warning(
            "Device matches %s with quality of %d%%. LOCAL DPS: %s",
            best_matching_type,
            best_match,
            log_json(dps),
        )
        _LOGGER.warning(
            "Include the previous log messages with any new device request to https://github.com/make-all/tuya-local/issues/",
        )
        if type_options:
            detected = getattr(self, "_auto_detected_protocol", None)
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_TYPE,
                        default=best_matching_key,
                    ): SelectSelector(SelectSelectorConfig(options=type_options)),
                }
            )
            if detected:
                return self.async_show_form(
                    step_id="select_type_auto_detected",
                    data_schema=schema,
                    description_placeholders={
                        "detected_protocol": str(detected),
                        "device_name": self._device_name_placeholder,
                    },
                )
            return self.async_show_form(
                step_id="select_type",
                data_schema=schema,
                description_placeholders={
                    "device_name": self._device_name_placeholder,
                },
            )
        else:
            return self.async_abort(reason="not_supported")

    async def async_step_select_type_auto_detected(self, user_input=None):
        return await self.async_step_select_type(user_input)

    async def async_step_choose_entities(self, user_input=None):
        config = await self.hass.async_add_executor_job(
            get_config,
            self.data[CONF_TYPE],
        )
        if user_input is not None:
            title = user_input[CONF_NAME]
            del user_input[CONF_NAME]
            return self.async_create_entry(
                title=title, data={**self.data, **user_input}
            )
        default_name = config.name
        if self.__discovered_device and self.__discovered_device.get("name"):
            default_name = self.__discovered_device["name"]
        schema = {vol.Required(CONF_NAME, default=default_name): str}

        return self.async_show_form(
            step_id="choose_entities",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device_name": self._device_name_placeholder,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return OptionsFlowHandler()


# Sentinel shown in the options "Device type" list: browsing ~1700 templates in a
# dropdown is unusable, so the list only shows the ones matching the product id and
# this entry opens a search step (PATCHES.md #25).
TYPE_RESULT_LIMIT = 150


class OptionsFlowHandler(OptionsFlow):
    def __init__(self):
        """Initialize options flow."""
        self._type_results: list[str] = []
        self._type_search = ""

    async def async_step_init(self, user_input=None):
        return await self.async_step_user(user_input)

    async def _async_save_options(self, user_input: dict):
        """Validate with the device, then store the options."""
        proto = user_input.get(CONF_PROTOCOL_VERSION)
        if proto != "auto":
            user_input[CONF_PROTOCOL_VERSION] = float(proto)
        # Chỉ lưu option khi có giá trị (tránh thêm key rỗng vào entry data)
        bridge = user_input.get(CONF_IR2MQTT_BRIDGE)
        if bridge in (None, "", IR2MQTT_BRIDGE_OFF):
            user_input.pop(CONF_IR2MQTT_BRIDGE, None)
        else:
            user_input[CONF_IR2MQTT_BRIDGE] = str(bridge).strip()
        config = {**self.config_entry.data, **self.config_entry.options, **user_input}
        device = await async_test_connection(config, self.hass)
        if device is None:
            return None
        return self.async_create_entry(title="", data=user_input)

    async def _async_all_types(self, search: str) -> list[str]:
        """Every template, A-Z, optionally filtered by a substring."""
        from .helpers.device_config import available_configs

        def _list() -> list[str]:
            names = sorted({name.removesuffix(".yaml") for name in available_configs()})
            if not search:
                return names
            return [name for name in names if search in name.lower()]

        names = await self.hass.async_add_executor_job(_list)
        return names[:TYPE_RESULT_LIMIT]

    async def _async_type_options(self, config):
        """Device types to choose from, the ones listing this product first."""
        from .bulk_add import async_types_for_products, find_cached_device

        current = config.get(CONF_TYPE)
        record = (
            find_cached_device(config.get(CONF_DEVICE_ID), config.get(CONF_DEVICE_CID))
            or {}
        )
        product_id = record.get("product_id")
        matched: list[str] = []
        if product_id:
            index = await async_types_for_products(self.hass, [product_id])
            matched = index.get(product_id, [])

        # The list holds the templates that declare this product id, plus whatever
        # the search box found, so it never becomes a 1700 item dropdown.
        ordered = sorted(set(matched) | set(self._type_results))
        if current and current not in ordered:
            ordered.insert(0, current)

        options = []
        for value in ordered:
            if value == current:
                label = f"{value} (đang dùng)"
            elif value in matched:
                label = f"{value} (khớp product id)"
            else:
                label = value
            options.append(SelectOptionDict(value=value, label=label))
        return options

    async def async_step_user(self, user_input=None):
        """Manage the options."""
        errors = {}
        config = {**self.config_entry.data, **self.config_entry.options}

        search_hint = ""
        if user_input is not None:
            search = str(user_input.pop("type_search", "") or "").strip()
            if search:
                # Filter the type list and show the form again with what was filled
                # in so far, instead of jumping to a separate step (a dropdown
                # selection alone never submits, which is confusing).
                self._type_search = search
                self._type_results = await self._async_all_types(search)
                if self._type_results:
                    search_hint = (
                        f"🔍 {len(self._type_results)} template khớp '{search}'."
                    )
                else:
                    self._type_search = ""
                    self._type_results = []
                    errors["base"] = "no_matches"
                config = {**config, **user_input}
            else:
                self._type_search = ""
                self._type_results = []
                saved = await self._async_save_options(user_input)
                if saved is not None:
                    return saved
                errors["base"] = "connection"

        # IR2MQTT bridge: select thay vì input — gồm "(tắt)", gợi ý theo tên
        # thiết bị, id đang dùng, và id của các entry tuya_local khác.
        current_bridge = config.get(CONF_IR2MQTT_BRIDGE) or ""
        suggested = slugify(self.config_entry.title or "") or slugify(
            config.get(CONF_NAME) or ""
        )
        bridge_ids: list[str] = []
        for candidate in (suggested, current_bridge):
            if candidate and candidate not in bridge_ids:
                bridge_ids.append(candidate)
        for other in self.hass.config_entries.async_entries(DOMAIN):
            if other.entry_id == self.config_entry.entry_id:
                continue
            other_id = (other.options or {}).get(CONF_IR2MQTT_BRIDGE)
            if other_id and other_id not in bridge_ids:
                bridge_ids.append(other_id)
        bridge_options = [SelectOptionDict(value=IR2MQTT_BRIDGE_OFF, label="(tắt)")]
        for bid in bridge_ids:
            if bid == current_bridge:
                label = f"{bid} (đang dùng)"
            elif bid == suggested:
                label = f"{bid} (theo tên thiết bị)"
            else:
                label = f"{bid} (entry khác)"
            bridge_options.append(SelectOptionDict(value=bid, label=label))

        # fork: the device type used to be fixed at add time (auto-add picks the
        # first config listing the product id), so a wrong pick could only be
        # fixed by removing and re-adding the device.  Let it be changed here.
        current_type = config.get(CONF_TYPE)
        type_options = await self._async_type_options(config)
        schema = {
            vol.Required(
                CONF_TYPE,
                default=current_type,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=type_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional("type_search", default=""): str,
            vol.Required(
                CONF_LOCAL_KEY,
                default=config.get(CONF_LOCAL_KEY, ""),
            ): str,
            vol.Required(CONF_HOST, default=config.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PROTOCOL_VERSION,
                default=str(config.get(CONF_PROTOCOL_VERSION, "auto")),
            ): vol.In(["auto"] + [str(v) for v in API_PROTOCOL_VERSIONS]),
            vol.Required(
                CONF_POLL_ONLY, default=config.get(CONF_POLL_ONLY, False)
            ): bool,
            vol.Optional(
                CONF_IR2MQTT_BRIDGE,
                default=current_bridge or IR2MQTT_BRIDGE_OFF,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=bridge_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        cfg = await self.hass.async_add_executor_job(
            get_config,
            config[CONF_TYPE],
        )
        if cfg is None:
            return self.async_abort(reason="not_supported")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "device_details_url": DEVICE_DETAILS_URL,
                "search_hint": search_hint,
            },
            errors=errors,
        )


def create_test_device(hass: HomeAssistant, config: dict):
    """Set up a tuya device based on passed in config."""
    subdevice_id = config.get(CONF_DEVICE_CID)
    device = TuyaLocalDevice(
        "Test",
        config[CONF_DEVICE_ID],
        config[CONF_HOST],
        config[CONF_LOCAL_KEY],
        config[CONF_PROTOCOL_VERSION],
        subdevice_id,
        hass,
        True,
    )

    return device


async def async_test_connection(config: dict, hass: HomeAssistant):
    domain_data = hass.data.get(DOMAIN)
    existing = domain_data.get(get_device_id(config)) if domain_data else None
    if existing and existing.get("device"):
        _LOGGER.info("Pausing existing device to test new connection parameters")
        existing["device"].pause()
        await asyncio.sleep(5)

    retval = None

    if config.get(CONF_PROTOCOL_VERSION) == "auto":
        # Test each protocol with a fresh device object. Reusing one device
        # object across protocol rotations causes 3.4/3.5 handshakes to fail:
        # the shared tinytuya object carries stale internal state from the
        # prior connection attempts.
        for proto in API_PROTOCOL_VERSIONS:
            proto_config = {**config, CONF_PROTOCOL_VERSION: proto}
            device = None
            try:
                device = await hass.async_add_executor_job(
                    create_test_device, hass, proto_config
                )
                await device.async_refresh()
                if device.has_returned_state:
                    retval = device
                    break
            except Exception as e:
                _LOGGER.debug("Protocol %s test failed with %s %s", proto, type(e), e)
            if device is not None:
                device._api.set_socketPersistent(False)
                if device._api.parent:
                    device._api.parent.set_socketPersistent(False)
    else:
        try:
            device = await hass.async_add_executor_job(
                create_test_device,
                hass,
                config,
            )
            await device.async_refresh()
            retval = device if device.has_returned_state else None
        except Exception as e:
            _LOGGER.warning("Connection test failed with %s %s", type(e), e)

    if existing and existing.get("device"):
        _LOGGER.info("Restarting device after test")
        existing["device"].resume()

    return retval


def scan_for_device(devid):
    return tinytuya.find_device(dev_id=devid)
