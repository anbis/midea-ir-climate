"""Every code in the SmartIR 9990.json (captured from the RG10B remote) must be
reproduced byte for byte by state_bytes(), and the Tuya encoding must round-trip."""
import base64, json, struct, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "midea_ir_climate"))
import midea

def tuya_decompress(data):
    out = bytearray(); i = 0
    while i < len(data):
        h = data[i]; i += 1
        n = h >> 5
        if n == 0:
            n = (h & 0x1F) + 1; out += data[i:i + n]; i += n
        else:
            if n == 7:
                n += data[i]; i += 1
            d = (((h & 0x1F) << 8) | data[i]) + 1; i += 1
            for _ in range(n + 2):
                out.append(out[-d])
    return out

def frame_of(code):
    raw = tuya_decompress(base64.b64decode(code))
    p = struct.unpack(f"<{len(raw)//2}H", raw)
    i = 2; bits = ""
    while i + 1 < len(p) and p[i] < 1000:
        bits += "1" if p[i + 1] > 1000 else "0"; i += 2
    return [int(bits[j:j + 8], 2) for j in range(0, 48, 8)]

MODES = {"cool": midea.MODE_COOL, "dry": midea.MODE_DRY, "auto": midea.MODE_AUTO,
         "heat": midea.MODE_HEAT, "fan_only": midea.MODE_FAN}
FANS = {"auto": midea.FAN_AUTO, "level1": midea.FAN_LOW, "level2": midea.FAN_MEDIUM, "level3": midea.FAN_HIGH}

def main(path):
    cmds = json.load(open(path))["commands"]
    n = 0
    for mode, fans in cmds.items():
        if not isinstance(fans, dict):
            continue
        for fan, temps in fans.items():
            for temp, code in temps.items():
                want = frame_of(code)
                got = midea.state_bytes(True, MODES[mode], FANS[fan], int(temp))
                assert got == want, (mode, fan, temp, got, want)
                assert frame_of(midea.tuya_code(got)) == got
                n += 1
    off = frame_of(cmds["off"])
    assert midea.state_bytes(False, midea.MODE_FAN, midea.FAN_HIGH, 24) == off, off
    # timer: 2 h = 4 half-hours -> byte 3 = 0x81 | (3 << 1) = 0x87
    assert midea.state_bytes(True, midea.MODE_COOL, midea.FAN_AUTO, 24, 4)[3] == 0x87
    assert midea.state_bytes(True, midea.MODE_COOL, midea.FAN_AUTO, 24, 0)[3] == 0xFF
    print(f"ok: {n} captured codes reproduced, off frame matches, timer field ok")

if __name__ == "__main__":
    main(sys.argv[1])
