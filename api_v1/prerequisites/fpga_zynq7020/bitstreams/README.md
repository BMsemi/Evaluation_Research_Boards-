# FPGA Bitstream Recovery Backup

These bitstreams are checked in so the AX7020/Zynq FPGA can be reprogrammed after a board power cycle or host reboot without rebuilding in Vivado.

## Backed-Up Files

| File | Use |
|---|---|
| `caravel_scan_debug_fpga_array_read_r00c00_burst.bit` | Full-array burst read starting at cell `(0,0)`, packet `0x0000`, with `SEQUENCE_MODE=1` and the API burst timing defaults. |
| `caravel_scan_debug_fpga_active_20260822_050304.bit` | Active remote Zynq bitstream copied from `C:/Users/geethika/zynq_scan_debug/caravel_scan_debug_fpga.bit` on 2026-08-22. |
| `caravel_scan_debug_fpga_read1405_repeat.bit` | Known-good read packet `0x1405`, cell `(5,0)`, copied from the chip1 cell (5,0) forming package. |
| `caravel_scan_debug_fpga_set9405_repeat.bit` | Known-good set packet `0x9405`, cell `(5,0)`, copied from the chip1 cell (5,0) forming package. |

## Reprogram After Reboot

From this directory on a machine with Vivado and JTAG access:

```bash
vivado -mode batch -source ../program_prebuilt_bitstream.tcl -tclargs caravel_scan_debug_fpga_active_20260822_050304.bit
```

For the original API programming TCL, copy the wanted backup to `caravel_scan_debug_fpga.bit` in the FPGA working directory and run:

```bash
vivado -mode batch -source program_scan_debug_zynq7020.tcl
```

The API can still generate new per-cell bitstreams dynamically during normal `read`, `set`, and `reset` commands. These backups are only for fast manual recovery.

## Checksums

```text
6e231460d5ac1bc2a0cf584965fb5d6cb1a7ebcd4a20f2117c8a9a74ed05ca86  caravel_scan_debug_fpga_array_read_r00c00_burst.bit
0fac3db89f481c7160c806c34c4b2ed840af1ed3d54ffcc1cbf93531acfcd604  caravel_scan_debug_fpga_active_20260822_050304.bit
298da5bd4849d9789028ef82339e200e31ca3d65778799bbc689b53ffcdac9b8  caravel_scan_debug_fpga_read1405_repeat.bit
188712a639d83a3f6f32f9e24e15753be0e4d2ec9f93a147c0827fcce8e74edc  caravel_scan_debug_fpga_set9405_repeat.bit
```
