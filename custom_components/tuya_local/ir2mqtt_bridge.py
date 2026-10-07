"""IR2MQTT bridge support for Tuya Local IR remote devices.

Đóng vai một "bridge" đúng hợp đồng MQTT của IR2MQTT
(topics ``ir2mqtt/bridge/<id>/{config,state,received,command,response}``) ngay
trong HA, dùng chính kết nối Tuya Local sẵn có (không cần process ngoài).

Bật bằng option ``ir2mqtt_bridge`` của config entry (để trống = tắt).

Xem: https://github.com/steelcuts/ir2mqtt_bridge (MANUAL.md) — hợp đồng JSON.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)

CAPABILITIES = ["nec", "samsung", "sony", "raw"]
STUDY_REFRESH = 15  # giây: vào lại study mode định kỳ (gửi lệnh có thể thoát study)
POLL_INTERVAL = 1  # giây: đọc DP receive


# --------------------------------------------------------------------- encoders
def nec_pulses(address: int, command: int, repeats: int = 0) -> list[int]:
    """NEC kiểu ESPHome: header 9000/4500, address 16-bit LSB-first,
    command 16-bit LSB-first (x repeats), footer mark 560."""
    out = [9000, 4500]
    for mask in (1 << i for i in range(16)):
        out += [560, 1690 if address & mask else 560]
    for _ in range(max(1, repeats or 1)):
        for mask in (1 << i for i in range(16)):
            out += [560, 1690 if command & mask else 560]
    out.append(560)
    return out


def samsung_pulses(data: int, nbits: int = 32) -> list[int]:
    """Samsung TV: header 4500/4500, data MSB-first, footer 560/560."""
    out = [4500, 4500]
    for i in range(nbits - 1, -1, -1):
        out += [560, 1690 if (data >> i) & 1 else 560]
    out += [560, 560]
    return out


def sony_pulses(data: int, nbits: int = 12) -> list[int]:
    """Sony SIRC: header 2400/600; bit1 1200/600; bit0 600/600; data MSB-first."""
    out = [2400, 600]
    for i in range(nbits - 1, -1, -1):
        out += [1200, 600] if (data >> i) & 1 else [600, 600]
    out.append(600)
    return out


def raw_pulses(timings: list) -> list[int]:
    """IR2MQTT dùng số ÂM cho khoảng nghỉ -> trị tuyệt đối (µs)."""
    return [abs(int(t)) for t in timings]


def encode(protocol: str, payload: dict) -> list[int]:
    """Đổi code IR2MQTT thành mảng timings µs (mark dương, space âm->dương xen kẽ)."""
    p = protocol.lower()
    if p == "raw":
        return raw_pulses(payload["timings"])
    if p == "nec":
        return nec_pulses(
            int(str(payload["address"]), 0),
            int(str(payload["command"]), 0),
            int(payload.get("repeats", 0)),
        )
    if p == "samsung":
        return samsung_pulses(int(str(payload["data"]), 0), int(payload.get("nbits", 32)))
    if p == "sony":
        return sony_pulses(int(str(payload["data"]), 0), int(payload.get("nbits", 12)))
    raise ValueError(f"protocol '{protocol}' chưa hỗ trợ")


def to_b64(pulses: list[int]) -> str:
    """Base64 của mảng uint16 LE (định dạng key1 của Tuya), pad cho chẵn phần tử."""
    if len(pulses) % 2:
        pulses = pulses + [5000]
    if not pulses or max(pulses) > 65535:
        raise ValueError(f"timing ngoài uint16: max={max(pulses) if pulses else 0}")
    return base64.b64encode(
        struct.pack("<" + str(len(pulses)) + "H", *pulses)
    ).decode()


def split_for_tuya(pulses: list[int]) -> list[tuple[str, float]]:
    """Tách timings > 50000µs thành nhiều chunk (giống infrared.py của tuya_local).

    Trả [(b64, delay_s), ...]; gửi lần lượt, nghỉ delay giữa các chunk.
    """
    raw: list[int] = []
    splits: list[tuple[int, float]] = []
    for t in pulses:
        u = abs(int(t))
        if u > 50000:
            splits.append((len(raw), (u - 5000) / 1_000_000.0))
            raw.append(5000)
        else:
            raw.append(u)
    if len(raw) % 2:
        raw.append(5000)
    out: list[tuple[str, float]] = []
    start = 0
    for idx, delay in splits:
        out.append((to_b64(raw[start:idx]), delay))
        start = idx
    out.append((to_b64(raw[start:]), 0.0))
    return out


def b64_to_pulses(code: str) -> list[int] | None:
    """Giải mã code DP202 (base64 uint16 LE) -> timings µs."""
    try:
        padded = code + "=" * (-len(code) % 4)
        raw = base64.b64decode(padded)
        if len(raw) < 4 or len(raw) % 2:
            return None
        return list(struct.unpack("<" + str(len(raw) // 2) + "H", raw))
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------- bridge
class IR2MQTTBridge:
    """Cầu IR2MQTT ⇄ entity remote của Tuya Local."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity,
        bridge_id: str,
        name: str | None = None,
        receiver_id: str = "rx1",
        transmitter_id: str = "tx1",
    ) -> None:
        self.hass = hass
        self.entity = entity
        self.bridge_id = bridge_id
        self.name = name or f"Tuya Local {bridge_id}"
        self.receiver_id = receiver_id
        self.transmitter_id = transmitter_id
        self.base = f"ir2mqtt/bridge/{bridge_id}"
        self.protocols = list(CAPABILITIES)
        self._unsub = None
        self._task: asyncio.Task | None = None
        self._state_task: asyncio.Task | None = None
        self._last_code: str | None = None
        self._stopped = False

    # ---- lifecycle
    async def async_start(self) -> None:
        """Publish config/state + subscribe command + vòng lặp nhận IR."""
        await self._async_publish(f"{self.base}/config", self._config_payload(), retain=True)
        await self._async_publish(f"{self.base}/state", self._state_payload(), retain=True)
        self._unsub = await mqtt.async_subscribe(
            self.hass, f"{self.base}/command", self._handle_command, qos=0
        )
        self._task = self.hass.loop.create_task(self._async_receive_loop())
        self._state_task = self.hass.loop.create_task(self._async_state_loop())
        _LOGGER.info("IR2MQTT bridge '%s' started", self.bridge_id)

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._state_task is not None:
            self._state_task.cancel()
            self._state_task = None
        await self._async_publish(
            f"{self.base}/state",
            json.dumps({"type": "state", "online": False}),
            retain=True,
        )
        _LOGGER.info("IR2MQTT bridge '%s' stopped", self.bridge_id)

    # ---- helpers
    async def _async_publish(self, topic: str, payload: str, retain: bool = False) -> None:
        try:
            await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=retain)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("IR2MQTT bridge '%s': publish %s lỗi: %s", self.bridge_id, topic, err)

    def _config_payload(self) -> str:
        device = self.entity._device
        return json.dumps(
            {
                "type": "config",
                "id": self.bridge_id,
                "name": self.name,
                "version": "tuya-local",
                "mac": "",
                "ip": getattr(device, "_address", ""),
                "network_type": "wifi",
                "receivers": [{"id": self.receiver_id}],
                "transmitters": [{"id": self.transmitter_id}],
                "capabilities": CAPABILITIES,
                "enabled_protocols": self.protocols,
            }
        )

    def _state_payload(self) -> str:
        return json.dumps(
            {"type": "state", "online": True, "enabled_protocols": self.protocols}
        )

    async def _async_respond(self, request_id: Any, success: bool, message: str = "") -> None:
        if request_id is None:
            return
        await self._async_publish(
            f"{self.base}/response",
            json.dumps({"request_id": request_id, "success": success, "message": message}),
        )

    # ---- host -> bridge
    @callback
    def _handle_command(self, msg) -> None:
        self.hass.async_create_task(self._async_handle_command(msg.payload))

    async def _async_handle_command(self, raw_payload) -> None:
        try:
            data = json.loads(raw_payload)
        except (ValueError, TypeError):
            _LOGGER.warning("IR2MQTT bridge '%s': payload JSON lỗi", self.bridge_id)
            return
        cmd = data.get("command")
        rid = data.get("request_id")

        if cmd == "send":
            code = data.get("code") or {}
            protocol = code.get("protocol", "raw")
            payload = code.get("payload", {})
            try:
                pulses = encode(protocol, payload)
                chunks = split_for_tuya(pulses)
                for b64, delay in chunks:
                    await self.entity.async_send_command(
                        [f"b64:{b64}"], num_repeats=1, delay_secs=0.5
                    )
                    if delay:
                        await asyncio.sleep(delay)
                _LOGGER.debug(
                    "IR2MQTT bridge '%s': send %s (%d timings, %d chunk)",
                    self.bridge_id, protocol, len(pulses), len(chunks),
                )
                await self._async_respond(rid, True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "IR2MQTT bridge '%s': send %s lỗi: %s", self.bridge_id, protocol, err
                )
                await self._async_respond(rid, False, str(err))
        elif cmd == "set_protocols":
            self.protocols = data.get("protocols", self.protocols)
            await self._async_publish(f"{self.base}/state", self._state_payload(), retain=True)
            await self._async_respond(rid, True)
        elif cmd in ("get_state", "ping"):
            await self._async_publish(f"{self.base}/state", self._state_payload(), retain=True)
            await self._async_respond(rid, True)
        elif cmd == "get_config":
            await self._async_publish(f"{self.base}/config", self._config_payload(), retain=True)
            await self._async_respond(rid, True)
        else:
            _LOGGER.debug("IR2MQTT bridge '%s': command lạ '%s'", self.bridge_id, cmd)
            await self._async_respond(rid, False, f"unsupported command '{cmd}'")

    # ---- bridge -> host
    async def _async_state_loop(self) -> None:
        """Nhắc lại state online định kỳ (bù cho race lúc reload entry)."""
        while not self._stopped:
            await asyncio.sleep(30)
            if self._stopped:
                return
            await self._async_publish(f"{self.base}/state", self._state_payload(), retain=True)

    async def _async_receive_loop(self) -> None:
        """Giữ study mode + đọc DP receive, publish /received khi có mã mới."""
        receive_dp = getattr(self.entity, "_receive_dp", None)
        send_dp = getattr(self.entity, "_send_dp", None)
        if receive_dp is None or send_dp is None:
            _LOGGER.warning(
                "IR2MQTT bridge '%s': entity không có DP send/receive — chỉ gửi được",
                self.bridge_id,
            )
            return
        last_study = 0.0
        while not self._stopped:
            try:
                now = time.monotonic()
                if now - last_study > STUDY_REFRESH:
                    await send_dp.async_set_value(
                        self.entity._device, json.dumps({"control": "study"})
                    )
                    last_study = now
                code = receive_dp.get_value(self.entity._device)
                if code and code != self._last_code:
                    self._last_code = code
                    await self.async_publish_received(code)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("IR2MQTT bridge '%s': receive loop: %s", self.bridge_id, err)
            await asyncio.sleep(POLL_INTERVAL)

    async def async_publish_received(self, code: str) -> None:
        pulses = b64_to_pulses(code)
        if not pulses:
            return
        timings = [p if i % 2 == 0 else -p for i, p in enumerate(pulses)]
        payload: dict[str, Any] = {
            "type": "received",
            "protocol": "raw",
            "receiver_id": self.receiver_id,
            "timestamp": int(time.time() * 1000),
            "payload": {"timings": timings},
        }
        payload.update(_detect_protocol(pulses))
        await self._async_publish(f"{self.base}/received", json.dumps(payload))
        _LOGGER.debug(
            "IR2MQTT bridge '%s': received %s (%d timings)",
            self.bridge_id, payload["protocol"], len(timings),
        )


def _detect_protocol(pulses: list[int]) -> dict:
    """Thử nhận diện Samsung/NEC từ timings (thuần Python, không phụ thuộc tinytuya)."""
    try:
        if len(pulses) < 66:
            return {}
        # Samsung: header 4500/4500, 32 bit MSB-first, bit mark 560
        if abs(pulses[0] - 4500) < 800 and abs(pulses[1] - 4500) < 800:
            data = 0
            ok = True
            for bit in range(32):
                mark = pulses[2 + bit * 2]
                space = pulses[3 + bit * 2]
                if not (300 < mark < 900):
                    ok = False
                    break
                data = (data << 1) | (1 if space > 1000 else 0)
            if ok:
                return {
                    "protocol": "samsung",
                    "payload": {"data": "0x%08X" % data, "nbits": 32},
                }
        # NEC: header 9000/4500, 32 bit LSB-first, bit mark 560
        if abs(pulses[0] - 9000) < 1500 and abs(pulses[1] - 4500) < 1200:
            data = 0
            ok = True
            for bit in range(32):
                mark = pulses[2 + bit * 2]
                space = pulses[3 + bit * 2]
                if not (300 < mark < 900):
                    ok = False
                    break
                if space > 1000:
                    data |= 1 << bit
            if ok:
                return {
                    "protocol": "nec",
                    "payload": {
                        "address": "0x%04X" % ((data >> 16) & 0xFFFF),
                        "command": "0x%04X" % (data & 0xFFFF),
                    },
                }
    except Exception:  # noqa: BLE001
        return {}
    return {}
