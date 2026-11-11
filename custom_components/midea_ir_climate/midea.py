"""Midea 48-bit IR protocol (remotes RG10B, RG57, ... ; IRremoteESP8266 ir_Midea).

A state frame is 6 bytes, sent twice: as is, then bit-inverted, with a gap
between. Bits go MSB first; a 1 is a long space, a 0 a short one.

  byte 0  0xA1            state frame (0xA2 = special command, not used here)
  byte 1  bit 7 power, bits 3-5 fan, bits 0-2 mode
  byte 2  0x40 | (temperature - 17); 0x5E when off or in fan-only mode
  byte 3  bit 7 and bit 0 set, bits 1-6 off timer: half-hours - 1, 0x3F = none
  byte 4  0xFF   (on timer, not used)
  byte 5  checksum

The checksum works on bit-reversed bytes: reverse each of the five bytes,
sum, two's complement, reverse again. Verified against 280 captured codes.
The timer field was found by capturing the remote's own timer presses and
confirmed on the unit: a state frame carrying it sets the air conditioner's
built-in off timer; the unit then switches itself off without any further
command.
"""
from __future__ import annotations

import base64
import struct

# Pulse timings in microseconds, medians of the captures.
HDR_MARK = 4264
HDR_SPACE = 4264
BIT_MARK = 518
ZERO_SPACE = 518
ONE_SPACE = 1584
FRAME_GAP = 5056

MODE_COOL, MODE_DRY, MODE_AUTO, MODE_HEAT, MODE_FAN = 0, 1, 2, 3, 4
FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH = 4, 1, 2, 3
MIN_TEMP, MAX_TEMP = 17, 30
MAX_TIMER_HALFHOURS = 48  # 24 h
TIMER_NONE = 0x3F


def _rev(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def checksum(b5: list[int]) -> int:
    return _rev((256 - sum(_rev(x) for x in b5)) & 0xFF)


def state_bytes(power: bool, mode: int, fan: int, temp: int, timer_halfhours: int = 0) -> list[int]:
    """Six bytes of a state frame. Auto and dry modes ignore the fan field,
    the remote sends 0 there; the unit accepts any value but the remote's
    encoding is reproduced exactly so frames match captured ones."""
    if mode in (MODE_AUTO, MODE_DRY):
        fan = 0
    temp = max(MIN_TEMP, min(MAX_TEMP, int(temp)))
    b1 = (0x80 if power else 0x00) | ((fan & 7) << 3) | (mode & 7)
    b2 = 0x40 | (temp - MIN_TEMP) if power and mode != MODE_FAN else 0x5E
    t = TIMER_NONE
    if timer_halfhours:
        t = max(1, min(MAX_TIMER_HALFHOURS, int(timer_halfhours))) - 1
    b3 = 0x81 | (t << 1)
    b5 = [0xA1, b1, b2, b3, 0xFF]
    return b5 + [checksum(b5)]


def _frame_pulses(bs: list[int]) -> list[int]:
    p = [HDR_MARK, HDR_SPACE]
    for b in bs:
        for i in range(7, -1, -1):
            p += [BIT_MARK, ONE_SPACE if (b >> i) & 1 else ZERO_SPACE]
    return p + [BIT_MARK]


def pulses(frame: list[int]) -> list[int]:
    """Mark/space list in microseconds: frame, gap, inverted frame."""
    return _frame_pulses(frame) + [FRAME_GAP] + _frame_pulses([x ^ 0xFF for x in frame])


def tuya_compress(raw: bytes) -> bytes:
    """FastLZ level 1 as the Tuya IR blasters expect: literal runs of up to
    32 bytes, back-references of length >= 3 within 8 KiB."""
    out = bytearray()
    lit = bytearray()
    i = 0

    def flush() -> None:
        nonlocal lit
        while lit:
            chunk = lit[:32]
            out.append(len(chunk) - 1)
            out.extend(chunk)
            lit = lit[32:]

    while i < len(raw):
        best_len, best_d = 0, 0
        for j in range(max(0, i - 8192), i):
            n = 0
            while i + n < len(raw) and raw[j + n] == raw[i + n] and n < 264:
                n += 1
            if n > best_len:
                best_len, best_d = n, i - j
        if best_len >= 3:
            flush()
            n, d = best_len - 2, best_d - 1
            if n < 7:
                out.append((n << 5) | (d >> 8))
            else:
                out.append((7 << 5) | (d >> 8))
                out.append(n - 7)
            out.append(d & 0xFF)
            i += best_len
        else:
            lit.append(raw[i])
            i += 1
    flush()
    return bytes(out)


def tuya_code(frame: list[int]) -> str:
    """The base64 string a zigbee2mqtt Tuya blaster takes in ir_code_to_send."""
    p = pulses(frame)
    return base64.b64encode(tuya_compress(struct.pack(f"<{len(p)}H", *p))).decode()
