"""
Setup for different kinds of Tuya sensors
"""

import logging

from homeassistant.components.sensor import (
    STATE_CLASSES,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_HOST
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

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
            TuyaLocalLastSeenSensor(device, config_entry.entry_id, device_id),
            TuyaLocalResponseTimeSensor(device, config_entry.entry_id, device_id),
            TuyaLocalProtocolVersionSensor(device, config_entry.entry_id, device_id),
            TuyaLocalDatapointCountSensor(device, config_entry.entry_id, device_id),
            TuyaLocalLastErrorSensor(device, config_entry.entry_id, device_id),
            TuyaLocalParentHubSensor(device, config_entry.entry_id, device_id),
            TuyaLocalConfigTypeSensor(
                device, config_entry.entry_id, device_id, config.get(CONF_TYPE)
            ),
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


class TuyaLocalDeviceInfoSensor(TuyaLocalLanInfoSensor):
    """Base for diagnostic sensors that read a value from the device object."""

    def __init__(self, device, entry_id: str, device_id: str, suffix: str, name: str):
        super().__init__(device, entry_id, device_id)
        self._attr_unique_id = f"{device.unique_id}_{suffix}"
        self._attr_name = name


class TuyaLocalLastSeenSensor(TuyaLocalDeviceInfoSensor):
    """When data was last received from the device."""

    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id, "last_seen", "Last seen")

    async def async_update(self):
        """Read the timestamp of the last received data."""
        seen = self._device.last_seen
        self._attr_native_value = dt_util.utc_from_timestamp(seen) if seen else None


class TuyaLocalResponseTimeSensor(TuyaLocalDeviceInfoSensor):
    """How long the last poll of the device took."""

    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id, "response_time", "Response time")

    async def async_update(self):
        """Read the duration of the last successful poll."""
        elapsed = self._device.response_time_ms
        self._attr_native_value = round(elapsed, 1) if elapsed is not None else None


class TuyaLocalProtocolVersionSensor(TuyaLocalDeviceInfoSensor):
    """The Tuya protocol version in use for this device."""

    _attr_icon = "mdi:swap-horizontal"

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(
            device, entry_id, device_id, "protocol_version", "Protocol version"
        )

    async def async_update(self):
        """Read the negotiated protocol version."""
        self._attr_native_value = self._device.protocol_version


class TuyaLocalDatapointCountSensor(TuyaLocalDeviceInfoSensor):
    """How many datapoints the device currently reports."""

    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id, "datapoint_count", "Datapoints")

    async def async_update(self):
        """Count the datapoints in the cached state."""
        self._attr_native_value = self._device.dp_count


class TuyaLocalLastErrorSensor(TuyaLocalDeviceInfoSensor):
    """The last error the device reported, if any."""

    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id, "last_error", "Last error")

    async def async_update(self):
        """Read the last error, with the message as an attribute."""
        err = self._device.last_error
        if not err:
            self._attr_native_value = "none"
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = err.get("code") or "connection error"
        self._attr_extra_state_attributes = {
            "message": err.get("message"),
            "at": dt_util.utc_from_timestamp(err["at"]) if err.get("at") else None,
        }


class TuyaLocalParentHubSensor(TuyaLocalDeviceInfoSensor):
    """The hub a sub device (e.g. a Zigbee child) belongs to."""

    _attr_icon = "mdi:lan-connect"

    def __init__(self, device, entry_id: str, device_id: str):
        super().__init__(device, entry_id, device_id, "parent_hub", "Parent hub")

    async def async_update(self):
        """Read the parent device id and the node id within it."""
        self._attr_native_value = self._device.parent_id
        self._attr_extra_state_attributes = {"node_id": self._device.node_id}


class TuyaLocalConfigTypeSensor(TuyaLocalDeviceInfoSensor):
    """The device configuration file this device is set up with."""

    _attr_icon = "mdi:file-cog-outline"

    def __init__(self, device, entry_id: str, device_id: str, config_type: str | None):
        super().__init__(device, entry_id, device_id, "config_type", "Config type")
        self._config_type = config_type

    async def async_update(self):
        """Read the configured device type."""
        self._attr_native_value = self._config_type
