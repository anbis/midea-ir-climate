"""Midea IR Climate: a climate entity that speaks the Midea 48-bit IR protocol.

Frames are generated from the protocol, not read from a code table, so every
combination of mode, fan, temperature and the air conditioner's own off timer
is available. The frames go out through an MQTT IR blaster (zigbee2mqtt Tuya
ZS06/ZS08 style, Tuya-compressed base64) or as a raw pulse list.
"""

DOMAIN = "midea_ir_climate"
