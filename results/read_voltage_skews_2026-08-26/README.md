# Read-voltage skew runs — 2026-08-26

This directory contains the completed single-cell read-voltage sweeps collected on 2026-08-26. The compact CSV is the version-controlled record; large Saleae captures and transient GUI/API logs remain under the ignored `api_v1/runs/` tree.

## Measurement setup

- Operation: single-cell array read
- Read word-line voltage (`Vcc_wl`): 2.5 V
- Shunt resistance: 470 ohm
- Reported value: mean set-shunt current from the configured Saleae measurement window
- One read pulse was applied per voltage point
- Every included row decoded the expected packet and completed with `ok=True`

## Included sweeps

| Sweep ID | Cell | Vcc_set range | Step | Points | Current range |
| --- | ---: | ---: | ---: | ---: | ---: |
| `r03_0p1_0p9` | (3, 0) | 0.1–0.9 V | 0.1 V | 9 | -55.217 to 225.342 µA |
| `r18_0p1_0p9` | (18, 0) | 0.1–0.9 V | 0.1 V | 9 | -40.140 to 116.158 µA |
| `r18_initial_0p5_0p9` | (18, 0) | 0.5–0.9 V | 0.1 V | 5 | 33.805 to 117.599 µA |
| `r18_repeat_0p5_0p9` | (18, 0) | 0.5–0.9 V | 0.1 V | 5 | 30.712 to 118.578 µA |
| `r26_0p5_0p9` | (26, 0) | 0.5–0.9 V | 0.1 V | 5 | 29.896 to 104.901 µA |

The `run_id` column in [`read_voltage_skews.csv`](read_voltage_skews.csv) maps every point back to its original run directory. Negative low-voltage values are retained as measured; no clipping or post-hoc offset correction was applied.

## Reproducing a point

From the repository root, replace the row, column, voltage, and output directory as needed:

```powershell
python api_v1/scan_debug_cli.py read `
  --run-dir api_v1/runs/<run-id> `
  --row 18 --col 0 `
  --read-vcc-set 0.5 `
  --read-vcc-wl-set 2.5
```

Hardware commands use the shared queue and require the configured Zynq, Saleae Logic, and Teensy DAC/ADC connections.
