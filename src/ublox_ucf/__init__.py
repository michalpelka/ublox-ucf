"""Read the configuration of a u-blox gen9+/X20 receiver and save it as a u-center 2 .ucf file."""

from __future__ import annotations

import argparse
import json
import struct
import sys

import serial
from pyubx2 import POLL, UBX_CONFIG_DATABASE, UBX_PROTOCOL, UBXMessage, UBXReader

LAYERS = {"RAM": 0, "BBR": 1, "Flash": 2, "Default": 7}
ALL_KEYS = 0x0FFFFFFF  # wildcard: every item in every group
MAX_ITEMS = 64  # max items the receiver returns per CFG-VALGET response

# key size field (bits 28..30) -> value length in bytes
KEY_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}

# keyID -> (name "CFG-XXX-YYY", type e.g. "U002")
KEY_INFO = {kid: (name.replace("_", "-", 2), typ) for name, (kid, typ) in UBX_CONFIG_DATABASE.items()}


def _poll(ser: serial.Serial, rdr: UBXReader, msg: UBXMessage, ident: str, tries: int = 100):
    ser.write(msg.serialize())
    for _ in range(tries):
        raw, parsed = rdr.read()
        if parsed is None:
            continue
        if parsed.identity == ident:
            return raw, parsed
        if parsed.identity == "ACK-NAK":
            return None, parsed
    raise TimeoutError(f"no {ident} response from receiver")


def receiver_version(ser, rdr) -> str:
    _, ver = _poll(ser, rdr, UBXMessage("MON", "MON-VER", POLL), "MON-VER")
    parts = [ver.swVersion, ver.hwVersion]
    parts += [getattr(ver, a) for a in dir(ver) if a.startswith("extension_")]
    return " | ".join(p.rstrip(b"\x00").decode(errors="replace") for p in parts)


def read_layer(ser, rdr, layer: int) -> dict[int, bytes]:
    """Return {keyID: raw little-endian value} for every item in a layer."""
    items: dict[int, bytes] = {}
    position = 0
    while True:
        ser.reset_input_buffer()
        raw, parsed = _poll(ser, rdr, UBXMessage.config_poll(layer, position, [ALL_KEYS]), "CFG-VALGET")
        if raw is None:  # NAK: nothing (more) at this position
            break
        payload = raw[6:-2]
        i, count = 4, 0  # skip version, layer, position(2)
        while i < len(payload):
            kid = struct.unpack_from("<I", payload, i)[0]
            size = KEY_SIZE[(kid >> 28) & 0x07]
            items[kid] = payload[i + 4 : i + 4 + size]
            i += 4 + size
            count += 1
        if count < MAX_ITEMS:
            break
        position += count
    return items


def decode(raw: bytes, typ: str | None):
    """Decode a raw value according to its pyubx2 type (U/I/X/E/L/R + size)."""
    if typ and typ[0] == "R":
        return struct.unpack("<f" if len(raw) == 4 else "<d", raw)[0]
    return int.from_bytes(raw, "little", signed=bool(typ and typ[0] == "I"))



def _float32_short(x: float) -> float:
    """Shortest decimal that round-trips through float32 (0.1 instead of 0.10000000149)."""
    target = struct.pack("<f", x)
    for prec in range(1, 10):
        cand = float(f"{x:.{prec}g}")
        if struct.pack("<f", cand) == target:
            return cand
    return x


def ucf_value_type(typ: str) -> str:
    """pyubx2 type 'U002' -> u-center 2 valueType 'U2' ('L001' -> 'L')."""
    return "L" if typ[0] == "L" else f"{typ[0]}{int(typ[1:])}"


def ucf_value(raw: bytes, typ: str):
    """Value encoded the way u-center 2 writes it in a .ucf file."""
    val = decode(raw, typ)
    if typ[0] == "R":
        if len(raw) == 4:
            val = _float32_short(val)
        return int(val) if val.is_integer() else val
    if len(raw) == 8:
        return f"BI:{val}"  # 64-bit integers are stored as strings
    return val


def ucf_item(kid: int, layer: int, raw: bytes) -> dict:
    typ = KEY_INFO[kid][1]
    return {
        "value": ucf_value(raw, typ),
        "layer": layer,
        "action": "Set",
        "valueType": ucf_value_type(typ),
        "key": f"0x{kid:08x}",
    }


def build_ucf(layers: dict[int, dict[int, bytes]]) -> str:
    """layers: {layer number: {keyID: raw}}; items are grouped per key, layers ascending (like u-center 2)."""
    order: dict[int, None] = {}
    for cfg in layers.values():
        order.update(dict.fromkeys(cfg))
    items = [
        ucf_item(kid, layer, layers[layer][kid])
        for kid in order
        for layer in sorted(layers)
        if kid in layers[layer]
    ]
    return json.dumps({"type": "CFG-ITEM", "items": items}, indent="\t")


def load_ucf_keys(path: str) -> set[int]:
    with open(path, encoding="utf-8") as f:
        return {int(i["key"], 16) for i in json.load(f)["items"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", help="output .ucf file")
    ap.add_argument("-p", "--port", default="/dev/ttyACM0", help="serial port (default: %(default)s)")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="baud rate (ignored on USB, default: %(default)s)")
    ap.add_argument("-l", "--layers", action="append", choices=["RAM", "BBR", "Flash"], default=None,
                    help="layer to read and save, repeatable; BBR/Flash only contain items stored there "
                         "(default: RAM Flash, same as u-center 2)")
    ap.add_argument("-c", "--changed-only", action="store_true",
                    help="only save items that differ from the receiver default layer")
    ap.add_argument("-k", "--keys-from", metavar="REF.ucf",
                    help="only save items whose keys appear in this reference .ucf (e.g. a u-center 2 export)")
    args = ap.parse_args(argv)

    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        rdr = UBXReader(ser, protfilter=UBX_PROTOCOL)
        print(f"Receiver: {receiver_version(ser, rdr)}", file=sys.stderr)
        layers = {}
        for name in args.layers or ["RAM", "Flash"]:
            cfg = read_layer(ser, rdr, LAYERS[name])
            print(f"Read {len(cfg)} items from {name} layer", file=sys.stderr)
            layers[LAYERS[name]] = cfg
        defaults = read_layer(ser, rdr, LAYERS["Default"]) if args.changed_only else None

    allowed = load_ucf_keys(args.keys_from) if args.keys_from else None
    skipped_unknown = set()
    for layer, cfg in layers.items():
        for kid in list(cfg):
            if kid not in KEY_INFO:
                skipped_unknown.add(kid)
                del cfg[kid]
            elif (allowed is not None and kid not in allowed) or (defaults is not None and defaults.get(kid) == cfg[kid]):
                del cfg[kid]

    text = build_ucf(layers)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)
    n = sum(len(c) for c in layers.values())
    print(f"Wrote {n} items to {args.output} "
          f"({len(skipped_unknown)} undocumented item ids skipped)", file=sys.stderr)
    return 0


def cli() -> None:
    sys.exit(main())
