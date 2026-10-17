"""
Setup for different kinds of Tuya sensors
"""

import logging

from homeassistant.components.sensor import (
    STATE_CLASSES,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import CONF_HOST
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_TYPE, DOMAIN
from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity, unit_from_ascii
from .helpers.config import async_tuya_setup_platform, get_device_id
from .helpers.device_config import TuyaEntityConfig, get_config
from .macmap import async_arp_table, async_load_macs

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = {**config_entry.data, **config_entry.options}
    device_id = get_device_id(config)
    device = hass.data[DOMAIN][device_id]["device"]

    cfg = await hass.async_add_executor_job(get_config, config[CONF_TYPE])
    if cfg is None:
        raise ValueError(f"No device config found for {config}")

    if any(e.entity == "sensor" for e in cfg.all_entities()):
        await async_tuya_setup_platform(
            hass,
            async_add_entities,
            config,
            "sensor",
            TuyaLocalSensor,
        )

    # Every device also gets its LAN location, as diagnostic sensors
    # (PATCHES.md #21).  Devices without sensor dps never reached this platform
    # before, which is why the caller forwards "sensor" for every device.
    async_add_entities(
        [
            TuyaLocalIpAddressSensor(device, config_entry.entry_id, device_id),
            TuyaLocalMacAddressSensor(device, config_entry.entry_id, device_id),
        ]
    )


class TuyaLocalLanInfoSensor(SensorEntity):
    """Diagnostic sensor showing where the device sits on the LAN."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, device, entry_id: str, device_id: str):
        self._device = device
        self._entry_id = entry_id
        self._device_id = device_id

    @property
    def device_info(self):
        """Return the device's information."""
        return self._device.device_info

    def entry_host(self) -> str | None:
        """Return the host the config entry currently points at."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return None
        return {**entry.data, **entry.options}.get(CONF_HOST)


class TuyaLocalIpAddressSensor(TuyaLocalLanInfoSensor):
    """The LAN address the device is polled on."""

    _attr_icon = "mdi:ip-network"

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id)
        self._attr_unique_id = f"{device.unique_id}_lan_ip"
        self._attr_name = "IP address"

    async def async_update(self):
        """Read the host from the config entry (it follows the device)."""
        self._attr_native_value = self.entry_host()


class TuyaLocalMacAddressSensor(TuyaLocalLanInfoSensor):
    """The MAC address learned for the device."""

    _attr_icon = "mdi:identifier"

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id)
        self._attr_unique_id = f"{device.unique_id}_mac_address"
        self._attr_name = "MAC address"

    async def async_update(self):
        """Prefer the remembered MAC, else read the ARP table for the host."""
        macs = await async_load_macs(self.hass)
        mac = macs.get(self._device_id)
        if not mac:
            host = self.entry_host()
            if host:
                mac = (await async_arp_table(self.hass)).get(host)
        self._attr_native_value = mac


class TuyaLocalSensor(TuyaLocalEntity, SensorEntity):
    """Representation of a Tuya Sensor"""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the sensor.
        Args:
            device (TuyaLocalDevice): the device API instance.
            config (TuyaEntityConfig): the configuration for this entity
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._sensor_dps = dps_map.pop("sensor", None)
        if self._sensor_dps is None:
            raise AttributeError(f"{config.config_id} is missing a sensor dps")
        self._unit_dps = dps_map.pop("unit", None)

        self._init_end(dps_map)

    @property
    def device_class(self):
        """Return the class of this device"""
        dclass = self._config.device_class
        if dclass:
            try:
                return SensorDeviceClass(dclass)
            except ValueError:
                _LOGGER.warning(
                    "%s/%s: Unrecognized sensor device class of %s ignored",
                    self._config._device.config,
                    self.name or "sensor",
                    dclass,
                )

    @property
    def state_class(self):
        """Return the state class of this entity"""
        sclass = self._sensor_dps.state_class
        if sclass in STATE_CLASSES:
            return sclass

    @property
    def native_value(self):
        """Return the value reported by the sensor"""
        return self._sensor_dps.get_value(self._device)

    @property
    def native_unit_of_measurement(self):
        """Return the unit for the sensor"""
        if self._unit_dps is None:
            unit = self._sensor_dps.unit
        else:
            unit = self._unit_dps.get_value(self._device)

        return unit_from_ascii(unit)

    @property
    def native_precision(self):
        """Return the precision for the sensor"""
        return self._sensor_dps.precision(self._device)

    @property
    def suggested_display_precision(self):
        """Return the suggested display precision for the sensor"""
        precision = self._sensor_dps.suggested_display_precision
        # if not explicitly defined, get based on scale
        # this is in line with older HA default behavior, and avoids
        # having to override the precision for every scaled sensor.
        if precision is None:
            precision = self._sensor_dps.precision(self._device)

        return precision

    @property
    def options(self):
        """Return a set of possible options."""
        # if mappings are all integers,  they are not options to HA
        values = self._sensor_dps.values(self._device)
        if values:
            for val in values:
                if isinstance(val, str):
                    return values
