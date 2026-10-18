"""
Setup for different kinds of Tuya numbers
"""

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.number.const import (
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    NumberDeviceClass,
)
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from . import cloud_refresh
from .const import CONF_SERVICE_ENTRY, DOMAIN
from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity, unit_from_ascii
from .helpers.config import async_tuya_setup_platform
from .helpers.device_config import TuyaEntityConfig

_LOGGER = logging.getLogger(__name__)

MODE_AUTO = "auto"


SERVICE_DEVICE_INFO = DeviceInfo(
    identifiers={(DOMAIN, "hub")},
    name="Tuya Local",
    manufacturer="tuya-local",
    model="Integration services",
    entry_type=DeviceEntryType.SERVICE,
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    # The service entry owns the integration wide controls (PATCHES.md #20, #23).
    if config_entry.data.get(CONF_SERVICE_ENTRY):
        interval = await cloud_refresh.async_get_interval(hass)
        async_add_entities([TuyaLocalCloudRefreshInterval(interval)])
        return

    config = {**config_entry.data, **config_entry.options}
    await async_tuya_setup_platform(
        hass,
        async_add_entities,
        config,
        "number",
        TuyaLocalNumber,
    )


class TuyaLocalCloudRefreshInterval(NumberEntity):
    """How often the cloud device list is refreshed in the background.

    The per device cloud diagnostics read a cached list, so this interval is the
    only thing that decides how often the cloud is asked. 0 turns the refresh off.
    """

    _attr_has_entity_name = True
    _attr_name = "Cloud refresh interval"
    _attr_unique_id = "tuya_local_cloud_refresh_interval"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_info = SERVICE_DEVICE_INFO
    _attr_native_min_value = cloud_refresh.MIN_INTERVAL_MINUTES
    _attr_native_max_value = cloud_refresh.MAX_INTERVAL_MINUTES
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(self, interval: int):
        self._attr_native_value = interval

    async def async_set_native_value(self, value: float) -> None:
        """Store the interval and restart the background refresh."""
        stored = await cloud_refresh.async_set_interval(self.hass, int(value))
        self._attr_native_value = stored
        self.async_write_ha_state()


class TuyaLocalNumber(TuyaLocalEntity, NumberEntity):
    """Representation of a Tuya Number"""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the sensor.
        Args:
            device (TuyaLocalDevice): the device API instance
            config (TuyaEntityConfig): the configuration for this entity
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._value_dps = dps_map.pop("value")
        if self._value_dps is None:
            raise AttributeError(f"{config.config_id} is missing a value dps")
        self._unit_dps = dps_map.pop("unit", None)
        self._min_dps = dps_map.pop("minimum", None)
        self._max_dps = dps_map.pop("maximum", None)
        self._decimal_dps = dps_map.pop("decimal", None)
        self._init_end(dps_map)

    @property
    def device_class(self):
        """Return the class of this device"""
        dclass = self._config.device_class
        if dclass:
            try:
                return NumberDeviceClass(dclass)
            except ValueError:
                _LOGGER.warning(
                    "%s/%s: Unrecognized number device class of %s ignored",
                    self._config._device.config,
                    self.name or "number",
                    dclass,
                )

    @property
    def native_min_value(self):
        if self._min_dps is not None:
            minimum = self._min_dps.get_value(self._device)
            if minimum is not None:
                return minimum
        r = self._value_dps.range(self._device)
        return DEFAULT_MIN_VALUE if r is None else r[0]

    @property
    def native_max_value(self):
        if self._max_dps is not None:
            maximum = self._max_dps.get_value(self._device)
            if maximum is not None:
                return maximum
        r = self._value_dps.range(self._device)
        return DEFAULT_MAX_VALUE if r is None else r[1]

    @property
    def native_step(self):
        return self._value_dps.step(self._device)

    @property
    def mode(self):
        """Return the mode."""
        m = self._config.mode
        if m is None:
            m = MODE_AUTO
        return m

    @property
    def native_unit_of_measurement(self):
        """Return the unit associated with this number."""
        if self._unit_dps is None:
            unit = self._value_dps.unit
        else:
            unit = self._unit_dps.get_value(self._device)

        return unit_from_ascii(unit)

    @property
    def native_value(self):
        """Return the current value of the number."""
        val = self._value_dps.get_value(self._device)
        if self._decimal_dps is not None:
            decimal = self._decimal_dps.get_value(self._device)
            if decimal is not None:
                val = val + decimal
        return val

    async def async_set_native_value(self, value):
        """Set the number."""
        _LOGGER.info("%s setting value to %s", self._config.config_id, value)
        settings = {}
        if self._decimal_dps is not None:
            whole = int(value)
            decimal = value - whole
            settings = self._decimal_dps.get_values_to_set(self._device, decimal)
            value = whole

        settings = settings | self._value_dps.get_values_to_set(
            self._device, value, settings
        )

        await self._device.async_set_properties(settings)
