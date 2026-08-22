# Scan-Debug Cell API

This package exposes a reusable Python API and CLI for cell-level scan-debug read, set, reset, and cycle operations.

All source files needed to set up the FPGA, DAC/ADC Teensy, Saleae capture host, and local summarizer are included under:

[prerequisites](./prerequisites)

The API keeps the experiment behavior used in the recent stair-pulse runs:

- every packet programs the FPGA bitstream for the selected cell and mode;
- FPGA asserts Caravel reset before sending the packet;
- read verification uses `Vcc_set=1.2 V`, `Vcc_wl_set=2.5 V` by default;
- set uses `OP_SET=1` and ramps rails until read current crosses the set threshold;
- reset/read polarity uses `OP_SET=0` and ramps rails until read current crosses the reset threshold;
- Saleae A12-A13 is treated as the set shunt current through `shunt_ohms`, default `1 kOhm`.

## Python

```python
from api_v1 import ScanDebugCellAPI

api = ScanDebugCellAPI()
read = api.read(row=5, col=0)
set_summary = api.set_cell(row=5, col=0)
reset_summary = api.reset_cell(row=5, col=0)
```

## CLI

Dry-run command, safe on Windows or Ubuntu:

```bash
python api_v1/scan_debug_cli.py read --row 5 --col 0 --dry-run
```

Hardware-backed set sweep:

```bash
python api_v1/scan_debug_cli.py set --row 5 --col 0 \
  --set-vcc-set 2.0 \
  --set-vcc-wl-set 0.5:2.0:0.1 \
  --set-threshold 150
```

Hardware-backed reset sweep:

```bash
python api_v1/scan_debug_cli.py reset --row 5 --col 0 \
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

## Required Files

FPGA files copied into this API folder:

- [prerequisites/fpga_zynq7020/caravel_scan_debug_fpga.v](./prerequisites/fpga_zynq7020/caravel_scan_debug_fpga.v)
- [prerequisites/fpga_zynq7020/caravel_scan_debug_fpga.xdc](./prerequisites/fpga_zynq7020/caravel_scan_debug_fpga.xdc)
- [prerequisites/fpga_zynq7020/program_scan_debug_zynq7020.tcl](./prerequisites/fpga_zynq7020/program_scan_debug_zynq7020.tcl)

Teensy DAC/ADC firmware copied into this API folder:

- [prerequisites/teensy_dac_adc/DAC_analog_vltgs.ino](./prerequisites/teensy_dac_adc/DAC_analog_vltgs.ino)
- [prerequisites/teensy_dac_adc/DAC_read.h](./prerequisites/teensy_dac_adc/DAC_read.h)
- [prerequisites/teensy_dac_adc/MovingAverageFilter.h](./prerequisites/teensy_dac_adc/MovingAverageFilter.h)
- [prerequisites/teensy_dac_adc/ads12xx.h](./prerequisites/teensy_dac_adc/ads12xx.h)
- [prerequisites/teensy_dac_adc/sync_slave.ino](./prerequisites/teensy_dac_adc/sync_slave.ino)

Saleae Ubuntu capture helper:

- [prerequisites/saleae_ubuntu/run_fpga_scan0000_la12_15_capture.py](./prerequisites/saleae_ubuntu/run_fpga_scan0000_la12_15_capture.py)

Local summarizer used by the API:

- [tools/summarize_capture.py](./tools/summarize_capture.py)

## Setup Checklist

1. Copy FPGA prerequisite files to the Zynq/Vivado working directory.

   Default API path:

   ```text
   C:/Users/geethika/zynq_scan_debug
   ```

   Required files in that directory:

   ```text
   caravel_scan_debug_fpga.v
   caravel_scan_debug_fpga.xdc
   program_scan_debug_zynq7020.tcl
   ```

   The API generates per-packet Vivado build TCL files and bitstreams in this same directory.

2. Flash the DAC/ADC Teensy with:

   ```text
   prerequisites/teensy_dac_adc/DAC_analog_vltgs.ino
   ```

   Keep the companion headers/sketch files in the same Arduino sketch folder.

3. Copy the Saleae helper to the Ubuntu Saleae API directory.

   Default API path:

   ```text
   /home/ubuntu-24-04/saleae-api/run_fpga_scan0000_la12_15_capture.py
   ```

   Required Ubuntu Python packages/environment:

   ```text
   saleae automation package
   pyserial
   rsync
   ssh server/client
   existing caravel utility environment, if HK reset mode is used
   ```

4. Start Logic 2 automation on Ubuntu before running hardware commands.

   The API expects Logic automation listening on:

   ```text
   127.0.0.1:10430
   ```

5. Confirm the DAC/ADC Teensy serial path.

   Default API path:

   ```text
   /dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00
   ```

   Override with:

   ```bash
   --adc-dac-port /dev/serial/by-id/...
   ```

## Voltage and Probe Connections

Rails controlled by the DAC/ADC Teensy firmware:

| Rail | Caravel/Chip node | DAC channel in current firmware | API default / behavior |
|---|---:|---:|---|
| `Vcc_read` | GPIO33 | DAC[0] | held `0 V` in `SCAN_CUSTOM_RAILS` |
| `Vcc_wl_read` | GPIO26 | DAC[1] | held `0 V` in `SCAN_CUSTOM_RAILS` |
| `Vcc_set` | GPIO27 | DAC[6] | ramped by API |
| `Vcc_wl_set` | GPIO30 | DAC[3] | ramped by API |
| `Vcc_wl_reset` | GPIO28 | DAC[4] | held `0 V` in `SCAN_CUSTOM_RAILS` |
| `Vcc_reset` | VDDA2 | DAC[5] | held `0 V` in `SCAN_CUSTOM_RAILS` |
| `VDDA1` | VDDA1 | DAC[14] / external supply as configured | not changed by API |
| `VDDC2` | VCCD2 | DAC[15] | not changed by API |

Current probes:

| Measurement | Saleae analog channels | Shunt |
|---|---:|---:|
| Set shunt current | A12 - A13 | default `1 kOhm` |
| Reset shunt current | A14 - A15 | default `1 kOhm` |
| ADC set monitor | A0 - A1 | firmware monitor |
| ADC read monitor | A2 - A3 | firmware monitor |
| ADC reset monitor | A4 - A5 | firmware monitor |

FPGA to Caravel and Saleae digital probes:

| Signal | Caravel node | FPGA J10 / Zynq pin | Saleae LA |
|---|---|---|---:|
| `wb_clk_i` / XCLK | Xclk | J10-16 / U14 | 8 |
| `rst_b` | Reset | J10-3 / W19 | 7 |
| `ready` | GPIO1 | J10-4 / W18 | 6 |
| `TM` | GPIO36 | J10-5 / R14 | 9 |
| `ScanInDR` | GPIO21 | J10-9 / W15 | 11 |
| `ScanInDL` | GPIO22 | J10-7 / Y17 | 10 |
| `ScanInCC` | scan clock/control | J10-8 / Y16 | optional |

The FPGA RTL changes scan data/control on the falling edge of `wb_clk_i`. Caravel samples on the rising edge.

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
