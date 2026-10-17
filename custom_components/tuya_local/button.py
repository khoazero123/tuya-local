"""
Setup for different kinds of Tuya button devices
"""

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import CONF_SERVICE_ENTRY, DOMAIN
from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity
from .helpers.config import async_tuya_setup_platform
from .helpers.device_config import TuyaEntityConfig

_LOGGER = logging.getLogger(__name__)

DEVICE_INFO = DeviceInfo(
    identifiers={(DOMAIN, "hub")},
    name="Tuya Local",
    manufacturer="tuya-local",
    model="Integration services",
    entry_type=DeviceEntryType.SERVICE,
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    # The service entry exists only to own the integration level buttons, so they
    # get a device of their own instead of landing inside a real device's entry
    # (PATCHES.md #20).  It has no device connection to set up.
    if config_entry.data.get(CONF_SERVICE_ENTRY):
        async_add_entities([TuyaLocalRefreshButton(), TuyaLocalCloudLogoutButton()])
        return

    config = {**config_entry.data, **config_entry.options}
    await async_tuya_setup_platform(
        hass,
        async_add_entities,
        config,
        "button",
        TuyaLocalButton,
    )


class TuyaLocalRefreshButton(ButtonEntity):
    """Integration level button that refreshes the device list.

    Relocates configured devices whose LAN IP changed, refreshes the device
    names from the Tuya cloud and re-runs the LAN discovery scan.
    """

    _attr_has_entity_name = True
    _attr_name = "Refresh device list"
    _attr_unique_id = "tuya_local_refresh_devices"
    _attr_icon = "mdi:refresh"
    _attr_device_info = DEVICE_INFO

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.hass.services.async_call(
            DOMAIN, "refresh_devices", {}, blocking=True
        )


class TuyaLocalCloudLogoutButton(ButtonEntity):
    """Integration level button that forgets the saved Tuya cloud login.

    Device control stays local, so devices keep working; the cloud side (device
    names, and the local keys offered when adding a device) is what goes away.
    Pressing it and then adding a device through the cloud path logs in again,
    which is how you switch Tuya accounts.
    """

    _attr_has_entity_name = True
    _attr_name = "Logout Tuya account"
    _attr_unique_id = "tuya_local_logout_cloud"
    _attr_icon = "mdi:logout"
    _attr_device_info = DEVICE_INFO

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.hass.services.async_call(DOMAIN, "logout_cloud", {}, blocking=True)


class TuyaLocalButton(TuyaLocalEntity, ButtonEntity):
    """Representation of a Tuya Button"""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialize the button.
        Args:
            device (TuyaLocalDevice): The device API instance.
            config (TuyaEntityConfig): The config portion for this entity.
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._button_dp = dps_map.pop("button")
        self._init_end(dps_map)

    @property
    def device_class(self):
        """Return the class for this device"""
        dclass = self._config.device_class
        try:
            return ButtonDeviceClass(dclass)
        except ValueError:
            if dclass:
                _LOGGER.warning(
                    "%s/%s: Unrecognized button device class of %s ignored",
                    self._config._device.config,
                    self.name or "button",
                    dclass,
                )

    async def async_press(self):
        """Press the button"""
        _LOGGER.info("%s pressing button", self._config._device.config)
        await self._button_dp.async_set_value(self._device, True)
