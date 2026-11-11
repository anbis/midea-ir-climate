# Midea IR Climate for Home Assistant

A `climate` entity for Midea-protocol air conditioners (Midea, Comfee, Carrier,
Electrolux, Hyundai, Lennox and other brands using the RG10B / RG57 / RG51
family of remotes) driven through an IR blaster. Frames are **generated from
the protocol**, not looked up in a code table, so the air conditioner's own
**off timer** (0.5 h steps, up to 24 h) is available from Home Assistant - the
unit counts it down itself and switches off even if Home Assistant is down.

The timer shows up as the climate entity's **preset modes**, so the standard
tile/thermostat cards and the more-info dialog expose it with no custom card.

## Blasters

- zigbee2mqtt Tuya IR blasters (ZS06, ZS08, UFO-R11 and friends): `encoding: tuya`
  (default), topic `zigbee2mqtt/<device>/set/ir_code_to_send`.
- anything that takes a raw mark/space list over MQTT: `encoding: raw`.

## Configuration (YAML)

```yaml
climate:
  - platform: midea_ir_climate
    name: Bedroom AC
    unique_id: bedroom_ac
    topic: zigbee2mqtt/ir-remote-bedroom/set/ir_code_to_send
    temperature_sensor: sensor.bedroom_temperature   # optional
    humidity_sensor: sensor.bedroom_humidity         # optional
    timer_presets: [0.5, 1, 1.5, 2, 3, 4, 6, 8]      # optional, hours
    timer_format: "%g h"                             # optional, preset label
```

Service `midea_ir_climate.set_off_timer` with `hours` (0 cancels) sets any
value the presets do not list.

## Notes

- IR is one way: the entity shows what was last sent. A command from the
  original remote is not seen.
- The timer is accepted by a **running** unit only. A frame with a timer sent
  to a unit that is off switches it off again, so the entity refuses to set a
  timer while off.
- Every later command (temperature, fan, mode) is sent with the **remaining**
  timer, so adjusting the unit does not cancel the countdown.
- Protocol: 48-bit Midea (IRremoteESP8266 `ir_Midea`), verified against 280
  captured codes of an RG10B(D1)/BGEFU1 remote.
