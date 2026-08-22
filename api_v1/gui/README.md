# Scan-Debug GUI

Local web GUI for watching `api_v1/runs/*/manifest.csv` while the cell API reads or programs cells. The GUI can be opened while an API command is already running in the background; it polls the manifest files and updates as new rows are written.

Start in view-only mode:

```bash
python api_v1/gui/server.py
```

Open:

```text
http://127.0.0.1:8765
```

The default mode only polls run manifests and does not start, stop, or alter any hardware process.

The GUI shows:

- latest API rows from the selected run;
- a 32 by 32 1Kbit heatmap of last recorded read-packet current values;
- the last recorded read-packet cell and current reading;
- a blinking marker for the cell currently being read or programmed;
- rising/falling current trends from read packets as records arrive.

To enable browser buttons that launch new `read`, `set`, `reset`, or `cycle` commands through `scan_debug_cli.py`, start with:

```bash
python api_v1/gui/server.py --allow-commands
```

The heatmap is a 32 by 32 1Kbit grid. Each cell displays the last recorded current from the selected run, and the latest read/program cell blinks while new manifest rows arrive.

The GUI's array-read command uses the CLI default `read-array` mode. Current default is burst mode: one FPGA sequence bitstream plus one Saleae-side burst helper fills the normal API manifest for all 1024 cells. Use the CLI directly with `--array-mode serial` if the old per-cell read loop is needed for debugging.

Existing background API processes are not stopped by the GUI. Start new commands from the browser only when the bench is ready for another operation.
