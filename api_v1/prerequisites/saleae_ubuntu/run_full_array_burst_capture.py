#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from saleae import automation

import run_fpga_scan0000_la12_15_capture as base


DIGITAL_CHANNELS = [6, 7, 8, 9, 10, 11]
ANALOG_CHANNELS = [12, 13, 14, 15]
DIGITAL_SAMPLE_RATE = int(os.environ.get("DIGITAL_SAMPLE_RATE", "50000000"))
ANALOG_SAMPLE_RATE = int(os.environ.get("ANALOG_SAMPLE_RATE", "3125000"))
DIGITAL_THRESHOLD_VOLTS = float(os.environ.get("DIGITAL_THRESHOLD_VOLTS", "1.2"))
AFTER_TRIGGER_SECONDS = float(os.environ.get("AFTER_TRIGGER_SECONDS", "0.000028"))
TRIM_DATA_SECONDS = float(os.environ.get("TRIM_DATA_SECONDS", "0.000003"))
MAX_CELLS = int(os.environ.get("MAX_CELLS", "1024"))
SHUNT_OHMS = 1000.0
RAIL_COMMAND = os.environ.get("RAIL_COMMAND", "SCAN_SET_RAILS").strip()
SKIP_SET_RAILS = os.environ.get("SKIP_SET_RAILS", "0") == "1"
ENABLE_ADC_MONITOR = os.environ.get("ENABLE_ADC_MONITOR", "1") != "0"
ADC_START_DELAY_SECONDS = float(os.environ.get("ADC_START_DELAY_SECONDS", "0.0"))
STOP_ON_MISMATCH = os.environ.get("STOP_ON_MISMATCH", "1") != "0"
START_ROW = int(os.environ.get("START_ROW", "0"))
START_COL = int(os.environ.get("START_COL", "0"))
TRIGGER_CHANNEL_INDEX = int(os.environ.get("TRIGGER_CHANNEL_INDEX", "9"))
TRIGGER_TYPE = os.environ.get("TRIGGER_TYPE", "RISING").strip().upper()


def sweep_cells():
    cells = []
    for row in range(32):
        cells.append((row, 0))
    for col in range(1, 32):
        for row in range(32):
            cells.append((row, col))
    try:
        start_index = cells.index((START_ROW, START_COL))
    except ValueError as exc:
        raise ValueError(f"START_ROW/START_COL ({START_ROW},{START_COL}) is not in the sweep order") from exc
    return cells[start_index:start_index + MAX_CELLS]


def packet_for(row: int, col: int) -> int:
    return (row << 10) | (col << 5) | row


def lsb_bits(packet: int) -> str:
    return "".join(str((packet >> bit) & 1) for bit in range(16))


def stats(values):
    values = list(values)
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None, "span": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 9),
        "std": round(statistics.pstdev(values), 9) if len(values) > 1 else 0.0,
        "min": round(min(values), 9),
        "max": round(max(values), 9),
        "span": round(max(values) - min(values), 9),
    }


def transitions_from_csv(path: Path, channel: int):
    field = f"Channel {channel}"
    transitions = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        previous = None
        for row in reader:
            t = float(row["Time [s]"])
            value = int(row[field])
            if previous is None or value != previous:
                transitions.append((t, value))
                previous = value
    return transitions


def deglitch(transitions, min_width_s=80e-9):
    if len(transitions) <= 2:
        return transitions
    changed = True
    items = list(transitions)
    while changed and len(items) > 2:
        changed = False
        cleaned = [items[0]]
        i = 1
        while i < len(items) - 1:
            prev_t, prev_v = cleaned[-1]
            t, v = items[i]
            next_t, next_v = items[i + 1]
            if next_t - t < min_width_s and prev_v == next_v:
                i += 2
                changed = True
                continue
            cleaned.append((t, v))
            i += 1
        while i < len(items):
            cleaned.append(items[i])
            i += 1
        items = cleaned
    return items


def value_at(transitions, t):
    value = transitions[0][1]
    for edge_t, edge_v in transitions[1:]:
        if edge_t > t:
            break
        value = edge_v
    return value


def first_transition(transitions, from_v, to_v, after=None):
    prior_t, prior_v = transitions[0]
    for t, v in transitions[1:]:
        if after is not None and t <= after:
            prior_t, prior_v = t, v
            continue
        if prior_v == from_v and v == to_v:
            return t
        prior_t, prior_v = t, v
    return None


def decode_packet(digital_csv: Path):
    clk = transitions_from_csv(digital_csv, 8)
    tm = transitions_from_csv(digital_csv, 9)
    dl = deglitch(transitions_from_csv(digital_csv, 10))
    dr = deglitch(transitions_from_csv(digital_csv, 11))
    tm_rise = first_transition(tm, 0, 1)
    tm_fall = first_transition(tm, 1, 0, after=tm_rise)
    dr_fall = first_transition(dr, 1, 0, after=tm_rise)
    dr_rise = first_transition(dr, 0, 1, after=dr_fall)

    low_samples = []
    clock_rises = []
    prev_t, prev_v = clk[0]
    for t, v in clk[1:]:
        if prev_v == 0 and v == 1:
            clock_rises.append(t)
            if tm_rise is not None and t > tm_rise and value_at(tm, t) == 1 and value_at(dr, t) == 0:
                low_samples.append(value_at(dl, t))
        prev_t, prev_v = t, v

    decoded = 0
    if len(low_samples) >= 17:
        for bit in range(16):
            decoded |= (low_samples[1 + bit] & 1) << bit

    periods = [b - a for a, b in zip(clock_rises, clock_rises[1:])]
    return {
        "decoded_packet": decoded if len(low_samples) >= 17 else None,
        "low_sample_count": len(low_samples),
        "low_samples": "".join(str(v) for v in low_samples[:18]),
        "tm_rise_s": tm_rise,
        "tm_fall_s": tm_fall,
        "dr_fall_s": dr_fall,
        "dr_rise_s": dr_rise,
        "dr_low_width_s": (dr_rise - dr_fall) if dr_fall is not None and dr_rise is not None else None,
        "clock_period_s_median": round(statistics.median(periods), 12) if periods else None,
    }


def latest_adc_sample(adc_csv: Path):
    if not adc_csv.exists():
        return None
    latest = None
    with adc_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("check") == "PASS":
                latest = row
    if latest is None:
        return None
    return {
        "t_s": float(latest["t_s"]),
        "read_uA": float(latest["adc_A2_A3_read_uA"]),
        "set_uA": float(latest["adc_A0_A1_set_uA"]),
        "reset_uA": float(latest["adc_A4_A5_reset_uA"]),
    }


def compact_analog_summary(analog_csv: Path):
    set_values = []
    reset_values = []
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            set_values.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
            reset_values.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
    return {
        "set_A12_minus_A13_uA": {**stats(set_values), "units": "uA"},
        "reset_A14_minus_A15_uA": {**stats(reset_values), "units": "uA"},
    }


def window_analog_summary(analog_csv: Path, start_s, stop_s):
    if start_s is None or stop_s is None or stop_s <= start_s:
        return compact_analog_summary(analog_csv)
    set_values = []
    reset_values = []
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            t_s = float(row["Time [s]"])
            if start_s <= t_s <= stop_s:
                set_values.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
                reset_values.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
    return {
        "set_A12_minus_A13_uA": {**stats(set_values), "units": "uA"},
        "reset_A14_minus_A15_uA": {**stats(reset_values), "units": "uA"},
    }


def configure_base_globals():
    base.DIGITAL_CHANNELS = DIGITAL_CHANNELS
    base.ANALOG_CHANNELS = ANALOG_CHANNELS
    base.ANALOG_SAMPLE_RATE = ANALOG_SAMPLE_RATE
    base.DIGITAL_THRESHOLD_VOLTS = DIGITAL_THRESHOLD_VOLTS
    base.SHUNT_OHMS = SHUNT_OHMS
    base.SCAN_RAIL_COMMAND = RAIL_COMMAND


def digital_trigger_type():
    if TRIGGER_TYPE == "FALLING":
        return automation.DigitalTriggerType.FALLING
    if TRIGGER_TYPE == "RISING":
        return automation.DigitalTriggerType.RISING
    raise ValueError(f"Unsupported TRIGGER_TYPE={TRIGGER_TYPE!r}; use RISING or FALLING")


def main():
    configure_base_globals()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path.home() / "saleae-api" / "captures" / f"fpga-full-cell-sweep-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    adc_csv = root / "adc_monitor.csv"
    manifest_csv = root / "manifest.csv"
    manifest_json = root / "manifest.json"
    run_log = root / "run.log"

    cells = sweep_cells()
    rails = {
        "command": "SCAN_SET_RAILS",
        "response": "SKIPPED_BY_ENV",
        "parsed_volts": {},
    } if SKIP_SET_RAILS else base.set_scan_set_rails()
    print(f"OUTPUT_ROOT={root}", flush=True)
    print(f"RAILS {rails['response']}", flush=True)

    stop_event = threading.Event()
    starts = {}
    adc_state = {"enabled": ENABLE_ADC_MONITOR}
    adc_thread = None
    if ENABLE_ADC_MONITOR:
        adc_thread = threading.Thread(target=base.adc_monitor, args=(stop_event, root, starts, adc_state), daemon=True)
        adc_thread.start()
        if ADC_START_DELAY_SECONDS > 0:
            time.sleep(ADC_START_DELAY_SECONDS)

    manifest = []
    fieldnames = [
        "index", "row", "col", "packet", "bits_lsb_first", "output_dir", "ok",
        "decoded_packet", "low_sample_count", "tm_rise_s", "dr_low_width_s",
        "clock_period_s_median", "la_set_mean_uA", "la_set_min_uA", "la_set_max_uA",
        "la_reset_mean_uA", "la_reset_min_uA", "la_reset_max_uA",
        "adc_read_uA", "adc_set_uA", "adc_reset_uA", "error",
    ]

    try:
        with automation.Manager.connect(port=base.SALEAE_PORT, connect_timeout_seconds=5) as manager, \
                manifest_csv.open("w", newline="") as manifest_handle, \
                run_log.open("w") as log_handle:
            device = base.find_logic_device(manager)
            app_info = manager.get_app_info()
            writer = csv.DictWriter(manifest_handle, fieldnames=fieldnames)
            writer.writeheader()
            device_config = automation.LogicDeviceConfiguration(
                enabled_digital_channels=DIGITAL_CHANNELS,
                enabled_analog_channels=ANALOG_CHANNELS,
                digital_sample_rate=DIGITAL_SAMPLE_RATE,
                analog_sample_rate=ANALOG_SAMPLE_RATE,
                digital_threshold_volts=DIGITAL_THRESHOLD_VOLTS,
            )
            capture_config = automation.CaptureConfiguration(
                capture_mode=automation.DigitalTriggerCaptureMode(
                    trigger_channel_index=TRIGGER_CHANNEL_INDEX,
                    trigger_type=digital_trigger_type(),
                    after_trigger_seconds=AFTER_TRIGGER_SECONDS,
                    trim_data_seconds=TRIM_DATA_SECONDS,
                )
            )

            for index, (row, col) in enumerate(cells):
                packet = packet_for(row, col)
                cell_dir = root / f"{index:04d}_r{row:02d}_c{col:02d}_pkt{packet:04x}"
                cell_dir.mkdir()
                detail = (
                    f"MEASURE index={index:04d} row={row:02d} col={col:02d} "
                    f"packet=0x{packet:04x} bits_lsb_first={lsb_bits(packet)}"
                )
                print(detail, flush=True)
                log_handle.write(detail + "\n")
                log_handle.flush()

                try:
                    capture_started = time.monotonic()
                    print(f"ARMING index={index:04d} packet=0x{packet:04x}", flush=True)
                    with manager.start_capture(
                        device_id=device.device_id,
                        device_configuration=device_config,
                        capture_configuration=capture_config,
                    ) as capture:
                        print(f"ARMED index={index:04d} packet=0x{packet:04x}", flush=True)
                        capture.wait()
                        capture.export_raw_data_csv(
                            directory=str(cell_dir),
                            digital_channels=DIGITAL_CHANNELS,
                            analog_channels=ANALOG_CHANNELS,
                            analog_downsample_ratio=1,
                        )
                    capture_done = time.monotonic()

                    digital = base.parse_digital(cell_dir / "digital.csv")
                    events = base.summarize_events(digital, {"reset_skipped": True}, capture_started)
                    decoded = decode_packet(cell_dir / "digital.csv")
                    analog = window_analog_summary(cell_dir / "analog.csv", decoded["dr_rise_s"], decoded["tm_fall_s"])
                    adc_latest = latest_adc_sample(adc_csv) or {}
                    ok = decoded["decoded_packet"] == packet

                    analysis = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "ok": ok,
                        "decoded": {
                            **decoded,
                            "decoded_packet_hex": f"0x{decoded['decoded_packet']:04x}" if decoded["decoded_packet"] is not None else None,
                        },
                        "events": events,
                        "analog_currents": analog,
                        "adc_latest": adc_latest,
                        "capture_started_monotonic": capture_started,
                        "capture_done_monotonic": capture_done,
                        "rails": rails,
                        "saleae": {
                            "logic_app_version": app_info.app_version,
                            "digital_sample_rate": DIGITAL_SAMPLE_RATE,
                            "analog_sample_rate": ANALOG_SAMPLE_RATE,
                            "trigger_channel_index": TRIGGER_CHANNEL_INDEX,
                            "trigger_type": TRIGGER_TYPE,
                            "after_trigger_seconds": AFTER_TRIGGER_SECONDS,
                            "trim_data_seconds": TRIM_DATA_SECONDS,
                        },
                    }
                    (cell_dir / "analysis.json").write_text(json.dumps(analysis, indent=2))

                    row_out = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "output_dir": str(cell_dir),
                        "ok": ok,
                        "decoded_packet": analysis["decoded"]["decoded_packet_hex"],
                        "low_sample_count": decoded["low_sample_count"],
                        "tm_rise_s": decoded["tm_rise_s"],
                        "dr_low_width_s": decoded["dr_low_width_s"],
                        "clock_period_s_median": decoded["clock_period_s_median"],
                        "la_set_mean_uA": analog["set_A12_minus_A13_uA"]["mean"],
                        "la_set_min_uA": analog["set_A12_minus_A13_uA"]["min"],
                        "la_set_max_uA": analog["set_A12_minus_A13_uA"]["max"],
                        "la_reset_mean_uA": analog["reset_A14_minus_A15_uA"]["mean"],
                        "la_reset_min_uA": analog["reset_A14_minus_A15_uA"]["min"],
                        "la_reset_max_uA": analog["reset_A14_minus_A15_uA"]["max"],
                        "adc_read_uA": adc_latest.get("read_uA"),
                        "adc_set_uA": adc_latest.get("set_uA"),
                        "adc_reset_uA": adc_latest.get("reset_uA"),
                        "error": "",
                    }
                except Exception as exc:
                    row_out = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "output_dir": str(cell_dir),
                        "ok": False,
                        "decoded_packet": "",
                        "low_sample_count": "",
                        "tm_rise_s": "",
                        "dr_low_width_s": "",
                        "clock_period_s_median": "",
                        "la_set_mean_uA": "",
                        "la_set_min_uA": "",
                        "la_set_max_uA": "",
                        "la_reset_mean_uA": "",
                        "la_reset_min_uA": "",
                        "la_reset_max_uA": "",
                        "adc_read_uA": "",
                        "adc_set_uA": "",
                        "adc_reset_uA": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    (cell_dir / "analysis_error.json").write_text(json.dumps(row_out, indent=2))
                    print(f"ERROR index={index:04d} {row_out['error']}", flush=True)
                writer.writerow(row_out)
                manifest_handle.flush()
                manifest.append(row_out)
                if row_out["ok"]:
                    print(
                        f"RESULT index={index:04d} ok=True decoded={row_out['decoded_packet']} "
                        f"la_set_mean={row_out['la_set_mean_uA']:.3f}uA "
                        f"la_reset_mean={row_out['la_reset_mean_uA']:.3f}uA",
                        flush=True,
                    )
                if STOP_ON_MISMATCH and not row_out["ok"]:
                    break
    finally:
        stop_event.set()
        if adc_thread is not None:
            adc_thread.join(timeout=2.0)

    manifest_json.write_text(json.dumps({
        "output_root": str(root),
        "rails": rails,
        "start_row": START_ROW,
        "start_col": START_COL,
        "cells_requested": len(cells),
        "cells_captured": len(manifest),
        "adc_state": adc_state,
        "manifest": manifest,
    }, indent=2))
    print(f"DONE output_root={root} cells={len(manifest)}", flush=True)


if __name__ == "__main__":
    main()
