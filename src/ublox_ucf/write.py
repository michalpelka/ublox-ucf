"""Write a u-center 2 .ucf configuration file to a u-blox gen9+/X20 receiver (CFG-VALSET)."""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict

import serial
from pyubx2 import UBX_PROTOCOL, UBXReader

from . import KEY_INFO, KEY_SIZE, LAYERS, read_layer, receiver_version

LAYER_NAMES = {v: k for k, v in LAYERS.items()}
LAYER_BIT = {0: 0x01, 1: 0x02, 2: 0x04}  # CFG-VALSET layers bitmask: RAM, BBR, Flash
MAX_ITEMS = 64  # max items per CFG-VALSET message

TX_NONE, TX_BEGIN, TX_CONTINUE, TX_APPLY = 0, 1, 2, 3


def encode_value(key: int, value_type: str, value) -> bytes:
    """Encode a .ucf value as the little-endian bytes the receiver expects."""
    size = KEY_SIZE[(key >> 28) & 0x07]
    if isinstance(value, str):
        if not value.startswith("BI:"):
            raise ValueError(f"key 0x{key:08x}: unsupported value {value!r}")
        value = int(value[3:])
    kind = value_type[0]
    if kind == "R":
        return struct.pack("<f" if size == 4 else "<d", float(value))
    value = int(value)
    if kind == "I":
        return value.to_bytes(size, "little", signed=True)
    if value < 0:  # tolerate negative values written for unsigned types
        value &= (1 << (8 * size)) - 1
    return value.to_bytes(size, "little")


def load_ucf(path: str) -> dict[int, dict[int, bytes]]:
    """Return {layer: {keyID: raw bytes}} from a .ucf file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("type") != "CFG-ITEM":
        raise ValueError(f"{path}: not a CFG-ITEM .ucf file (type={data.get('type')!r})")
    layers: dict[int, dict[int, bytes]] = defaultdict(dict)
    for item in data["items"]:
        action = item.get("action", "Set")
        if action != "Set":
            raise ValueError(f"unsupported action {action!r} for key {item['key']}")
        layer = int(item["layer"])
        if layer not in LAYER_BIT:
            raise ValueError(f"key {item['key']}: invalid target layer {layer}")
        key = int(item["key"], 16)
        layers[layer][key] = encode_value(key, item["valueType"], item["value"])
    return dict(layers)


def _frame(payload: bytes) -> bytes:
    body = bytes([0x06, 0x8A]) + struct.pack("<H", len(payload)) + payload
    ck_a = ck_b = 0
    for b in body:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xb5\x62" + body + bytes([ck_a, ck_b])


def valset(ser, rdr, layer_mask: int, transaction: int, items: list[tuple[int, bytes]]) -> bool:
    """Send one CFG-VALSET, return True on ACK-ACK, False on ACK-NAK."""
    payload = bytes([1, layer_mask, transaction, 0])
    payload += b"".join(struct.pack("<I", k) + v for k, v in items)
    ser.reset_input_buffer()
    ser.write(_frame(payload))
    for _ in range(100):
        _, msg = rdr.read()
        if msg is None or msg.identity not in ("ACK-ACK", "ACK-NAK"):
            continue
        if msg.clsID == 0x06 and msg.msgID == 0x8A:
            return msg.identity == "ACK-ACK"
    raise TimeoutError("no ACK for CFG-VALSET")


def write_layer(ser, rdr, layer: int, cfg: dict[int, bytes], use_transaction: bool) -> bool:
    items = list(cfg.items())
    chunks = [items[i : i + MAX_ITEMS] for i in range(0, len(items), MAX_ITEMS)]
    mask = LAYER_BIT[layer]
    name = LAYER_NAMES[layer]
    for n, chunk in enumerate(chunks):
        if not use_transaction or len(chunks) == 1:
            tx = TX_NONE
        else:
            tx = TX_BEGIN if n == 0 else TX_APPLY if n == len(chunks) - 1 else TX_CONTINUE
        if not valset(ser, rdr, mask, tx, chunk):
            print(f"  {name}: receiver rejected (NAK) chunk {n + 1}/{len(chunks)}"
                  f"{' - transaction aborted, nothing applied' if tx != TX_NONE else ''}", file=sys.stderr)
            keys = ", ".join(KEY_INFO.get(k, (f"0x{k:08x}",))[0] for k, _ in chunk[:5])
            print(f"  first keys in chunk: {keys} ...", file=sys.stderr)
            return False
    print(f"  {name}: wrote {len(items)} items in {len(chunks)} message(s)", file=sys.stderr)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="input .ucf file")
    ap.add_argument("-p", "--port", default="/dev/ttyACM0", help="serial port (default: %(default)s)")
    ap.add_argument("-b", "--baud", type=int, default=115200, help="baud rate (ignored on USB, default: %(default)s)")
    ap.add_argument("-l", "--layers", action="append", choices=["RAM", "BBR", "Flash"],
                    help="only write items targeting this layer, repeatable (default: all layers in the file)")
    ap.add_argument("--no-transaction", action="store_true",
                    help="send chunks independently instead of as one atomic transaction per layer")
    ap.add_argument("--no-verify", action="store_true", help="skip reading the layers back to verify")
    ap.add_argument("-n", "--dry-run", action="store_true", help="parse and encode the file, but don't write")
    args = ap.parse_args(argv)

    layers = load_ucf(args.input)
    if args.layers:
        wanted = {LAYERS[n] for n in args.layers}
        layers = {l: c for l, c in layers.items() if l in wanted}
    for layer, cfg in sorted(layers.items()):
        print(f"{args.input}: {len(cfg)} items for {LAYER_NAMES[layer]}", file=sys.stderr)
    if args.dry_run or not layers:
        return 0

    ok = True
    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        rdr = UBXReader(ser, protfilter=UBX_PROTOCOL)
        print(f"Receiver: {receiver_version(ser, rdr)}", file=sys.stderr)
        for layer, cfg in sorted(layers.items()):
            ok &= write_layer(ser, rdr, layer, cfg, not args.no_transaction)
        if ok and not args.no_verify:
            for layer, cfg in sorted(layers.items()):
                actual = read_layer(ser, rdr, layer)
                bad = [k for k, v in cfg.items() if actual.get(k) != v]
                name = LAYER_NAMES[layer]
                if bad:
                    ok = False
                    print(f"  {name}: {len(bad)} items differ after write:", file=sys.stderr)
                    for k in bad[:20]:
                        print(f"    {KEY_INFO.get(k, (f'0x{k:08x}',))[0]}: wanted {cfg[k].hex()} "
                              f"got {actual[k].hex() if k in actual else 'missing'}", file=sys.stderr)
                else:
                    print(f"  {name}: verified {len(cfg)} items", file=sys.stderr)
    print("Done." if ok else "FAILED.", file=sys.stderr)
    return 0 if ok else 1


def cli() -> None:
    sys.exit(main())
