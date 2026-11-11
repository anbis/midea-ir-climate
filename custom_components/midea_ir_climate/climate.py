"""Climate entity for Midea air conditioners driven over an MQTT IR blaster."""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    PLATFORM_SCHEMA,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components import mqtt
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.util.dt as dt_util

from . import DOMAIN
from . import midea

_LOGGER = logging.getLogger(__name__)

CONF_TOPIC = "topic"
CONF_ENCODING = "encoding"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TIMER_PRESETS = "timer_presets"
CONF_TIMER_FORMAT = "timer_format"

ENCODING_TUYA = "tuya"  # base64 FastLZ string, zigbee2mqtt Tuya ZS06/ZS08 "ir_code_to_send"
ENCODING_RAW = "raw"  # JSON list of mark/space microseconds

DEFAULT_NAME = "Midea AC"
DEFAULT_TIMER_PRESETS = [0.5, 1, 1.5, 2, 3, 4, 6, 8]
DEFAULT_TIMER_FORMAT = "%g h"

ATTR_TIMER_ENDS_AT = "off_timer_ends_at"
ATTR_TIMER_REMAINING = "off_timer_remaining_min"
ATTR_HOURS = "hours"
SERVICE_SET_OFF_TIMER = "set_off_timer"

HVAC_TO_MIDEA = {
    HVACMode.AUTO: midea.MODE_AUTO,
    HVACMode.COOL: midea.MODE_COOL,
    HVACMode.DRY: midea.MODE_DRY,
    HVACMode.HEAT: midea.MODE_HEAT,
    HVACMode.FAN_ONLY: midea.MODE_FAN,
}
FAN_TO_MIDEA = {
    FAN_AUTO: midea.FAN_AUTO,
    FAN_LOW: midea.FAN_LOW,
    FAN_MEDIUM: midea.FAN_MEDIUM,
    FAN_HIGH: midea.FAN_HIGH,
}


def _timer_hours(value):
    hours = float(value)
    if hours <= 0 or hours > 24 or (hours * 2) != int(hours * 2):
        raise vol.Invalid("timer hours must be 0.5 .. 24 in steps of 0.5")
    return hours


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TOPIC): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_ENCODING, default=ENCODING_TUYA): vol.In([ENCODING_TUYA, ENCODING_RAW]),
        vol.Optional(CONF_TEMPERATURE_SENSOR): cv.entity_id,
        vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
        vol.Optional(CONF_MIN_TEMP, default=midea.MIN_TEMP): vol.All(int, vol.Range(midea.MIN_TEMP, midea.MAX_TEMP)),
        vol.Optional(CONF_MAX_TEMP, default=midea.MAX_TEMP): vol.All(int, vol.Range(midea.MIN_TEMP, midea.MAX_TEMP)),
        vol.Optional(CONF_TIMER_PRESETS, default=DEFAULT_TIMER_PRESETS): vol.All(cv.ensure_list, [_timer_hours]),
        vol.Optional(CONF_TIMER_FORMAT, default=DEFAULT_TIMER_FORMAT): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    async_add_entities([MideaIrClimate(hass, config)])
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_OFF_TIMER,
        {vol.Required(ATTR_HOURS): vol.All(vol.Coerce(float), vol.Range(min=0, max=24))},
        "async_set_off_timer",
    )


class MideaIrClimate(ClimateEntity, RestoreEntity):
    """One air conditioner. IR is one way, so the state here is what was last
    sent, plus the off timer the unit is counting down on its own."""

    _attr_should_poll = False
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL, HVACMode.DRY, HVACMode.HEAT, HVACMode.FAN_ONLY]
    _attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config) -> None:
        self.hass = hass
        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = config.get(CONF_UNIQUE_ID)
        self._topic = config[CONF_TOPIC]
        self._encoding = config[CONF_ENCODING]
        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)
        self._attr_min_temp = config[CONF_MIN_TEMP]
        self._attr_max_temp = config[CONF_MAX_TEMP]
        self._timer_format = config[CONF_TIMER_FORMAT]
        self._presets: dict[str, float] = {self._label(h): h for h in config[CONF_TIMER_PRESETS]}
        self._attr_preset_modes = [PRESET_NONE, *self._presets]

        self._attr_hvac_mode = HVACMode.OFF
        self._last_on_mode = HVACMode.COOL
        self._attr_fan_mode = FAN_AUTO
        self._attr_native_target_temperature = 24
        self._attr_native_current_temperature = None
        self._attr_current_humidity = None
        self._timer_end: datetime | None = None
        self._timer_label: str = PRESET_NONE
        self._unsub_timer = None

    # ------------------------------------------------------------------ setup
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_hvac_modes:
            self._attr_hvac_mode = HVACMode(last.state)
            if self._attr_hvac_mode != HVACMode.OFF:
                self._last_on_mode = self._attr_hvac_mode
            if last.attributes.get("fan_mode") in self._attr_fan_modes:
                self._attr_fan_mode = last.attributes["fan_mode"]
            if last.attributes.get(ATTR_TEMPERATURE) is not None:
                self._attr_native_target_temperature = float(last.attributes[ATTR_TEMPERATURE])
            ends = last.attributes.get(ATTR_TIMER_ENDS_AT)
            if ends and self._attr_hvac_mode != HVACMode.OFF:
                end = dt_util.parse_datetime(ends)
                # The unit kept counting while HA was down: pick the countdown
                # back up, or close it out if it already ran out.
                if end and end > dt_util.utcnow():
                    self._timer_end = end
                    self._timer_label = last.attributes.get("preset_mode") or self._label_for_end(end)
                    self._schedule_timer_end()
                elif end:
                    self._attr_hvac_mode = HVACMode.OFF

        for sensor, handler in (
            (self._temperature_sensor, self._temperature_changed),
            (self._humidity_sensor, self._humidity_changed),
        ):
            if sensor:
                self.async_on_remove(async_track_state_change_event(self.hass, sensor, handler))
                state = self.hass.states.get(sensor)
                if state:
                    handler(None, state)

    @callback
    def _temperature_changed(self, event: Event[EventStateChangedData] | None, state=None) -> None:
        state = state or event.data["new_state"]
        self._attr_native_current_temperature = self._number(state)
        self.async_write_ha_state()

    @callback
    def _humidity_changed(self, event: Event[EventStateChangedData] | None, state=None) -> None:
        state = state or event.data["new_state"]
        self._attr_current_humidity = self._number(state)
        self.async_write_ha_state()

    @staticmethod
    def _number(state):
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    # ------------------------------------------------------------------ timer
    def _label(self, hours: float) -> str:
        return self._timer_format % hours

    def _label_for_end(self, end: datetime) -> str:
        return self._label(self._halfhours_left(end) / 2)

    def _halfhours_left(self, end: datetime | None = None) -> int:
        """Half-hours the unit still has to run, rounded up: the unit takes the
        value as "from now", so a resend has to carry the remainder."""
        end = end or self._timer_end
        if end is None:
            return 0
        left = (end - dt_util.utcnow()).total_seconds()
        return max(1, math.ceil(left / 1800)) if left > 0 else 0

    def _schedule_timer_end(self) -> None:
        self._cancel_timer_schedule()
        if self._timer_end:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._timer_fired, self._timer_end)

    def _cancel_timer_schedule(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    def _clear_timer(self) -> None:
        self._cancel_timer_schedule()
        self._timer_end = None
        self._timer_label = PRESET_NONE

    @callback
    def _timer_fired(self, _now) -> None:
        """The unit switched itself off; mirror that without sending anything."""
        self._clear_timer()
        self._attr_hvac_mode = HVACMode.OFF
        self.async_write_ha_state()

    @property
    def preset_mode(self) -> str:
        return self._timer_label if self._timer_end else PRESET_NONE

    @property
    def extra_state_attributes(self):
        if not self._timer_end:
            return {ATTR_TIMER_ENDS_AT: None, ATTR_TIMER_REMAINING: None}
        left = max(0, int((self._timer_end - dt_util.utcnow()).total_seconds() // 60))
        return {ATTR_TIMER_ENDS_AT: self._timer_end.isoformat(), ATTR_TIMER_REMAINING: left}

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_NONE:
            await self.async_set_off_timer(0)
            return
        if preset_mode not in self._presets:
            raise ServiceValidationError(f"unknown preset {preset_mode}")
        await self.async_set_off_timer(self._presets[preset_mode], preset_mode)

    async def async_set_off_timer(self, hours: float, label: str | None = None) -> None:
        """Entity service midea_ir_climate.set_off_timer; hours=0 cancels."""
        hours = round(float(hours) * 2) / 2
        if hours <= 0:
            if self._timer_end:
                self._clear_timer()
                if self._attr_hvac_mode != HVACMode.OFF:
                    await self._send()
                self.async_write_ha_state()
            return
        if self._attr_hvac_mode == HVACMode.OFF:
            # A frame with a timer reaching a unit that is off (or just powered
            # on) switches it off; the timer only makes sense on a running unit.
            raise ServiceValidationError("Turn the air conditioner on before setting its off timer")
        self._timer_end = dt_util.utcnow() + timedelta(hours=hours)
        self._timer_label = label or self._label(hours)
        self._schedule_timer_end()
        await self._send()
        self.async_write_ha_state()

    # --------------------------------------------------------------- commands
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        self._attr_hvac_mode = hvac_mode
        self._last_on_mode = hvac_mode
        await self._send()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(self._last_on_mode)

    async def async_turn_off(self) -> None:
        self._clear_timer()
        self._attr_hvac_mode = HVACMode.OFF
        await self._send()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._attr_native_target_temperature = float(max(self._attr_min_temp, min(self._attr_max_temp, round(temp))))
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send()
        self.async_write_ha_state()

    async def _send(self) -> None:
        power = self._attr_hvac_mode != HVACMode.OFF
        mode = HVAC_TO_MIDEA[self._attr_hvac_mode if power else self._last_on_mode]
        frame = midea.state_bytes(
            power,
            mode,
            FAN_TO_MIDEA[self._attr_fan_mode],
            int(self._attr_native_target_temperature),
            self._halfhours_left() if power else 0,
        )
        if self._encoding == ENCODING_TUYA:
            payload = midea.tuya_code(frame)
        else:
            payload = json.dumps(midea.pulses(frame))
        _LOGGER.debug("%s -> %s: %s", self.entity_id, self._topic, " ".join(f"{b:02X}" for b in frame))
        await mqtt.async_publish(self.hass, self._topic, payload)
