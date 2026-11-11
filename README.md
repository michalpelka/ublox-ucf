# ublox-ucf

Read and write the configuration of a u-blox receiver (generation 9 or newer, including X20) as a
**u-center 2 `.ucf` file**. This works without u-center 2.

Tested with a ZED-X20P (HPG 2.10, PROTVER 50.11) connected over USB.

- `ublox-ucf` reads the configuration from the receiver (`CFG-VALGET`) and writes it to a `.ucf` file.
- `ublox-ucf-write` sends a `.ucf` file to the receiver (`CFG-VALSET`) and then reads it back to check it.

## Install

You need [uv](https://docs.astral.sh/uv/). Run:

```sh
uv sync
```

On Linux, your user must be in the `dialout` group to open `/dev/ttyACM*` and `/dev/ttyUSB*`.

## Reading the configuration

```sh
uv run ublox-ucf config.ucf                    # RAM + Flash, like a u-center 2 export
uv run ublox-ucf -c changed.ucf                # only items that differ from the receiver defaults
uv run ublox-ucf -l RAM ram.ucf                # only the RAM layer
uv run ublox-ucf -k ucenter_export.ucf out.ucf # only the keys present in a reference .ucf
uv run ublox-ucf -p /dev/ttyUSB0 -b 38400 config.ucf
```

| Option | Description |
|---|---|
| `-p, --port PORT` | Serial port (default `/dev/ttyACM0`) |
| `-b, --baud BAUD` | Baud rate (default 115200; ignored over USB) |
| `-l, --layers LAYER` | Layer to read: `RAM`, `BBR` or `Flash`. Repeat for several layers. Default: `RAM` and `Flash` |
| `-c, --changed-only` | Only save items whose value differs from the receiver's default layer |
| `-k, --keys-from REF.ucf` | Only save keys that appear in a reference `.ucf`, such as a u-center 2 export |

The RAM layer holds every item. The BBR and Flash layers hold only the items that were saved there, so
the default output has each item once for RAM, plus a second copy for each item that is stored in Flash.
This matches what u-center 2 exports.

`-c` produces the smallest useful file, containing only your own changes. Use it to copy your setup to
another receiver.

## Writing a configuration

```sh
uv run ublox-ucf-write config.ucf              # write every layer in the file
uv run ublox-ucf-write -l RAM config.ucf       # write only the RAM items (lost at power cycle)
uv run ublox-ucf-write -l Flash config.ucf     # write only the Flash items (kept after power cycle)
uv run ublox-ucf-write -n config.ucf           # dry run: check the file, send nothing
```

| Option | Description |
|---|---|
| `-p, --port PORT` | Serial port (default `/dev/ttyACM0`) |
| `-b, --baud BAUD` | Baud rate (default 115200; ignored over USB) |
| `-l, --layers LAYER` | Only write items for this layer. Repeat for several layers. Default: every layer in the file |
| `--no-transaction` | Send the batches independently instead of as one all-or-nothing transaction per layer |
| `--no-verify` | Skip reading the values back after writing |
| `-n, --dry-run` | Parse and convert the file without connecting to the receiver |

Items are sent in batches of 64. By default, each layer is sent as a single transaction: if the
receiver rejects any batch (for example, an unknown key or an invalid value), nothing from that layer
is applied. After writing, the tool reads every layer back and compares the values. It exits with
status `1` if the receiver rejected the file or a value doesn't match.

> **Caution:** changing the baud rate or protocol settings of the port you are connected through
> takes effect as soon as it is written to RAM, which can cut the connection during the write.
> Over USB this is not a problem.

## The `.ucf` file format

A `.ucf` file is JSON, the same format that u-center 2 exports:

```json
{
	"type": "CFG-ITEM",
	"items": [
		{
			"value": 100,
			"layer": 0,
			"action": "Set",
			"valueType": "U2",
			"key": "0x30210001"
		}
	]
}
```

- `layer`: `0` = RAM, `1` = BBR, `2` = Flash
- `valueType`: `L` (bool), `U1`–`U8`, `I1`–`I8`, `X1`–`X8`, `E1`–`E4`, `R4`, `R8`
- 64-bit integers are stored as strings: `"BI:16238547128276412563"`
- Floats are plain JSON numbers (`298.257223563`)

## Limitations

- Item names and types come from the [pyubx2](https://github.com/semuconsulting/pyubx2) configuration
  database. The receiver also reports about 550 undocumented keys. `ublox-ucf` skips these.
- `ublox-ucf` can include some documented keys that a u-center 2 export of the same receiver leaves
  out. With a ZED-X20P there were 69 of them, such as `CFG-SIGNAL-LBAND_ENA` and some RTCM output
  rates. The receiver reports these keys itself, so they should be valid to set. Use
  `-k <u-center export>` if you need exactly the same set of keys as u-center 2.
- Item order differs from u-center 2: `ublox-ucf` keeps the order the receiver reports them in.
- Only the `Set` action is supported.
