# Scan-Debug Cell API

This package exposes a reusable Python API and CLI for cell-level scan-debug read, set, reset, and cycle operations.

The API keeps the experiment behavior used in the recent stair-pulse runs:

- every packet programs the FPGA bitstream for the selected cell and mode;
- FPGA asserts Caravel reset before sending the packet;
- read verification uses `Vcc_set=1.2 V`, `Vcc_wl_set=2.5 V` by default;
- set uses `OP_SET=1` and ramps rails until read current crosses the set threshold;
- reset/read polarity uses `OP_SET=0` and ramps rails until read current crosses the reset threshold;
- Saleae A12-A13 is treated as the set shunt current through `shunt_ohms`, default `1 kOhm`.

## Python

```python
from scan_debug_cell_api import ScanDebugCellAPI

api = ScanDebugCellAPI()
read = api.read(row=5, col=0)
set_summary = api.set_cell(row=5, col=0)
reset_summary = api.reset_cell(row=5, col=0)
```

## CLI

Dry-run command, safe on Windows or Ubuntu:

```bash
python scan_debug_cell_api/scan_debug_cli.py read --row 5 --col 0 --dry-run
```

Hardware-backed set sweep:

```bash
python scan_debug_cell_api/scan_debug_cli.py set --row 5 --col 0 \
  --set-vcc-set 2.0 \
  --set-vcc-wl-set 0.5:2.0:0.1 \
  --set-threshold 150
```

Hardware-backed reset sweep:

```bash
python scan_debug_cell_api/scan_debug_cli.py reset --row 5 --col 0 \
  --reset-vcc-set 2.0,2.3,2.6,2.9,3.2,3.5 \
  --reset-vcc-wl-set 0.5:2.0:0.1 \
  --reset-threshold 100
```

## Cross-Platform Notes

The API itself is pure Python and runs on macOS, Ubuntu, or Windows. Hardware access uses external tools:

- `ssh` and `rsync` must be available on the machine running the API.
- For Windows Zynq programming, the remote side uses PowerShell plus Vivado.
- For Ubuntu/local Zynq programming, set `--zynq-os posix`.
- Prefer SSH keys or an existing SSH agent.
- On Ubuntu/macOS only, `--zynq-password ...` can use `expect` for the Windows Zynq host. On Windows, use SSH keys.

## Packet Format

Packets are encoded as:

```text
{OP_SET, SL_SEL[4:0], BL_SEL[4:0], WL_SEL[4:0]}
```

The present hardware mapping uses `row -> SL_SEL/WL_SEL` and `col -> BL_SEL`.

Examples:

- cell `(1,0)` read: `0x0401`
- cell `(4,0)` read: `0x1004`
- cell `(10,10)` read: `0x294a`
- cell `(5,0)` set: `0x9405`
