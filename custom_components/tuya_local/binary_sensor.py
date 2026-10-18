"""
Setup for different kinds of Tuya Binary sensors
"""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_DEVICE_CID, CONF_TYPE, DOMAIN
from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity
from .helpers.config import async_tuya_setup_platform, get_device_id
from .helpers.device_config import TuyaEntityConfig, get_config

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = {**config_entry.data, **config_entry.options}
    device_id = get_device_id(config)
    device = hass.data[DOMAIN][device_id]["device"]

    cfg = await hass.async_add_executor_job(get_config, config[CONF_TYPE])
    if cfg is None:
        raise ValueError(f"No device config found for {config}")

    if any(e.entity == "binary_sensor" for e in cfg.all_entities()):
        await async_tuya_setup_platform(
            hass,
            async_add_entities,
            config,
            "binary_sensor",
            TuyaLocalBinarySensor,
        )

    # Every device also reports whether the local link is alive (PATCHES.md #22).
    async_add_entities(
        [
            TuyaLocalConnectedBinarySensor(device, config_entry.entry_id, device_id),
            TuyaLocalCloudOnlineBinarySensor(
                device,
                config_entry.entry_id,
                device_id,
                device_id,
                config.get(CONF_DEVICE_CID),
            ),
        ]
    )


class TuyaLocalConnectedBinarySensor(BinarySensorEntity):
    """Whether the device is answering on the LAN."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = True

    def __init__(self, device: TuyaLocalDevice, entry_id: str, device_id: str):
        self._device = device
        self._entry_id = entry_id
        self._device_id = device_id
        self._attr_unique_id = f"{device.unique_id}_connected"
        self._attr_name = "Connected"

    @property
    def device_info(self):
        """Return the device's information."""
        return self._device.device_info

    @property
    def is_on(self) -> bool:
        """Return whether the device has answered since the last failure."""
        return bool(self._device.has_returned_state)


class TuyaLocalBinarySensor(TuyaLocalEntity, BinarySensorEntity):
    """Representation of a Tuya Binary Sensor"""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the sensor.
        Args:
            device (TuyaLocalDevice): the device API instance.
            config (TuyaEntityConfig): the configuration for this entity
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._sensor_dps = dps_map.pop("sensor")
        if self._sensor_dps is None:
            raise AttributeError(f"{config.config_id} is missing a sensor dps")
        self._init_end(dps_map)

    @property
    def device_class(self):
        """Return the class of this device"""
        dclass = self._config.device_class
        try:
            return BinarySensorDeviceClass(dclass)
        except ValueError:
            if dclass:
                _LOGGER.warning(
                    "%s/%s: Unrecognised binary_sensor device class of %s ignored",
                    self._config._device.config,
                    self.name or "binary_sensor",
                    dclass,
                )

    @property
    def is_on(self):
        """Return true if the binary sensor is on."""
        return self._sensor_dps.get_value(self._device)


class TuyaLocalCloudOnlineBinarySensor(BinarySensorEntity):
    """Whether the Tuya cloud reports the device as online.

    Read from the cached account device list, so this never calls the cloud.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self,
        device: TuyaLocalDevice,
        entry_id: str,
        device_id: str,
        cloud_device_id: str | None,
        cid: str | None,
    ):
        self._device = device
        self._entry_id = entry_id
        self._device_id = device_id
        self._cloud_device_id = cloud_device_id
        self._cid = cid
        self._attr_unique_id = f"{device.unique_id}_cloud_online"
        self._attr_name = "Cloud online"

    @property
    def device_info(self):
        """Return the device's information."""
        return self._device.device_info

    @property
    def is_on(self) -> bool | None:
        """Return the cached cloud online state, or None when it is not known."""
        from .bulk_add import find_cached_device

        record = find_cached_device(self._cloud_device_id, self._cid)
        if not record:
            return None
        return bool(record.get("online"))
