#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARIZER = ROOT / "api_v1/tools/summarize_capture.py"
FPGA_BITSTREAM_DIR = ROOT / "api_v1/prerequisites/fpga_zynq7020/bitstreams"
MANIFEST_FIELDS = [
    "index",
    "stage",
    "kind",
    "cell",
    "operation",
    "packet",
    "vcc_set_V",
    "vcc_wl_set_V",
    "bits_lsb_first",
    "bitstream",
    "ok",
    "decoded_packet",
    "la_set_window_mean_uA",
    "local_output_dir",
    "error",
]


Operation = Literal["read", "set", "reset"]


@dataclass(frozen=True)
class CellAddress:
    row: int
    col: int = 0

    def validate(self) -> None:
        if not 0 <= self.row <= 31:
            raise ValueError(f"row must be 0..31, got {self.row}")
        if not 0 <= self.col <= 31:
            raise ValueError(f"col must be 0..31, got {self.col}")

    @property
    def label(self) -> str:
        return f"{self.row}_{self.col}"


@dataclass(frozen=True)
class RailVoltages:
    """Voltages used by the DAC rail command.

    The existing Teensy command path controls the two rails relevant for scan-debug
    pulse experiments as millivolts: Vcc_set and Vcc_wl_set. Other rails are kept
    at the firmware defaults used by SCAN_CUSTOM_RAILS.
    """

    vcc_set_v: float
    vcc_wl_set_v: float

    @property
    def command(self) -> str:
        return f"SCAN_CUSTOM_RAILS {round(self.vcc_set_v * 1000):.0f} {round(self.vcc_wl_set_v * 1000):.0f}"


@dataclass(frozen=True)
class SweepConfig:
    vcc_set_v: tuple[float, ...]
    vcc_wl_set_v: tuple[float, ...]
    threshold_uA: float
    direction: Literal["above", "below"]
    confirm_reads: int = 10
    stop_on_threshold: bool = True

    @staticmethod
    def from_ranges(
        *,
        vcc_set_v: Iterable[float],
        vcc_wl_set_v: Iterable[float],
        threshold_uA: float,
        direction: Literal["above", "below"],
        confirm_reads: int = 10,
        stop_on_threshold: bool = True,
    ) -> "SweepConfig":
        return SweepConfig(
            tuple(vcc_set_v),
            tuple(vcc_wl_set_v),
            threshold_uA,
            direction,
            confirm_reads,
            stop_on_threshold,
        )


@dataclass
class ScanDebugConfig:
    run_dir: Path = ROOT / "api_v1/runs/default"
    read_rails: RailVoltages = field(default_factory=lambda: RailVoltages(1.0, 2.5))
    set_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=(1.6, 2.0, 2.3, 2.4, 2.5, 2.8),
            vcc_wl_set_v=(
                0.5,
                0.6,
                0.7,
                0.8,
                0.9,
                1.0,
                1.1,
                1.2,
                1.3,
                1.4,
                1.5,
                1.6,
                1.7,
                1.8,
                1.9,
                2.0,
            ),
            threshold_uA=200.0,
            direction="above",
        )
    )
    reset_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=(3.0, 3.1, 3.2, 3.3, 3.4, 3.5),
            vcc_wl_set_v=(0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9, 2.0),
            threshold_uA=130.0,
            direction="below",
        )
    )
    attempts: int = 3
    shunt_ohms: float = 470.0
    dry_run: bool = False

    zynq_host: str | None = "geethika@100.116.216.70"
    zynq_password: str | None = None
    zynq_os: Literal["windows", "posix"] = "windows"
    zynq_dir: str = "C:/Users/geethika/zynq_scan_debug"
    vivado_cmd: str = "C:/Xilinx/Vivado/2019.1/bin/vivado.bat"

    saleae_host: str | None = "ubuntu-24-04@100.98.132.51"
    saleae_dir: str = "/home/ubuntu-24-04/saleae-api"
    saleae_capture_script: str = ".venv/bin/python run_fpga_scan0000_la12_15_capture.py"
    saleae_burst_capture_script: str = ".venv/bin/python run_full_array_burst_capture.py"
    saleae_restart_script: str = "./start-logic2-automation.sh"
    saleae_restart_wait_seconds: float = 10.0
    adc_dac_port: str = "/dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00"
    summarizer: Path = DEFAULT_SUMMARIZER

    digital_sample_rate: int = 50_000_000
    analog_sample_rate: int = 6_250_000
    analog_channels: str = "12,13,14,15"
    trigger_channel: int = 11
    trigger_edge: str = "falling"
    after_trigger_seconds: float = 0.000090
    trim_data_seconds: float = 0.000003
    digital_threshold_volts: float = 1.2
    enable_adc_monitor: bool = True
    burst_initial_delay_cycles: int = 40_000_000
    burst_repeat_after_done_cycles: int = 10_000_000
    burst_after_trigger_seconds: float = 0.000028
    burst_trim_data_seconds: float = 0.000003
    burst_capture_timeout_seconds: float = 420.0


@dataclass
class CellOperationResult:
    cell: CellAddress
    operation: str
    packet: str
    rails: RailVoltages
    current_uA: float | None
    decoded_packet: str = ""
    ok: bool = False
    local_output_dir: str = ""
    error: str = ""


def packet_for_cell(cell: CellAddress, op_set: int) -> int:
    """Return `{OP_SET, SL_SEL[4:0], BL_SEL[4:0], WL_SEL[4:0]}`.

    Current hardware mapping uses row for SL/WL and col for BL. Examples:
    `(1,0), read -> 0x0401`; `(10,10), read -> 0x294a`.
    """

    cell.validate()
    if op_set not in (0, 1):
        raise ValueError(f"op_set must be 0 or 1, got {op_set}")
    return (op_set << 15) | (cell.row << 10) | (cell.col << 5) | cell.row


def bits_lsb(packet: int) -> str:
    return f"{packet:016b}"[::-1]


class CommandRunner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def run(self, cmd: list[str], *, log: Path | None = None, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        text = " ".join(cmd)
        if self.dry_run:
            if log:
                log.write_text(f"DRY_RUN {text}\n")
            return subprocess.CompletedProcess(cmd, 0, f"DRY_RUN {text}\n", "")
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout_s)
        if log:
            log.write_text(proc.stdout)
        return proc

    def ssh(self, host: str, command: str, *, timeout_s: int | None = None, log: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.run(["ssh", host, command], timeout_s=timeout_s, log=log)

    def ssh_with_expect_password(
        self,
        host: str,
        password: str,
        command: str,
        *,
        timeout_s: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.dry_run:
            text = f"DRY_RUN ssh {host} {command}\n"
            return subprocess.CompletedProcess(["ssh", host, command], 0, text, "")
        if platform.system().lower().startswith("win"):
            raise RuntimeError("password SSH automation on Windows needs SSH keys or an external tool; use --zynq-host with key auth")
        if shutil.which("expect") is None:
            raise RuntimeError("expect is required for password SSH automation on this platform; use SSH keys or install expect")
        script = f"""
set timeout {timeout_s or 600}
spawn ssh {host} {{{command}}}
expect {{
  -re "password:" {{ send "{password}\\r"; exp_continue }}
  eof
}}
catch wait result
exit [lindex $result 3]
"""
        proc = subprocess.run(["expect", "-c", script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return subprocess.CompletedProcess(["ssh", host, command], proc.returncode, proc.stdout, "")


class ScanDebugCellAPI:
    def __init__(self, config: ScanDebugConfig | None = None):
        self.config = config or ScanDebugConfig()
        self.config.run_dir.mkdir(parents=True, exist_ok=True)
        (self.config.run_dir / "raw").mkdir(exist_ok=True)
        self.runner = CommandRunner(self.config.dry_run)
        self.manifest = self.config.run_dir / "manifest.csv"
        self._ensure_manifest()

    def read(self, row: int, col: int = 0) -> CellOperationResult:
        cell = CellAddress(row, col)
        return self._pulse_and_capture(cell, "read", self.config.read_rails, "read")

    def read_array(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
        *,
        mode: Literal["burst", "burst-columns", "serial"] = "burst-columns",
    ) -> dict[str, object]:
        if not 0 <= row_start <= row_end <= 31:
            raise ValueError(f"row range must be 0..31, got {row_start}..{row_end}")
        if not 0 <= col_start <= col_end <= 31:
            raise ValueError(f"col range must be 0..31, got {col_start}..{col_end}")
        if mode == "burst":
            return self.read_array_burst(row_start, row_end, col_start, col_end)
        if mode == "burst-columns":
            return self.read_array_burst_columns(row_start, row_end, col_start, col_end)
        if mode != "serial":
            raise ValueError(f"array mode must be burst, burst-columns, or serial, got {mode!r}")
        reads: list[dict[str, object]] = []
        for row in range(row_start, row_end + 1):
            for col in range(col_start, col_end + 1):
                reads.append(asdict(self.read(row, col)))
        summary = {
            "operation": "read-array",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(reads),
            "reads": reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def read_array_burst_columns(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
    ) -> dict[str, object]:
        total = (row_end - row_start + 1) * (col_end - col_start + 1)
        all_reads: list[dict[str, object]] = []
        self._ensure_saleae_burst_script()
        for col in range(col_start, col_end + 1):
            column_total = row_end - row_start + 1
            packet = packet_for_cell(CellAddress(row_start, col), 0)
            self._append_progress("read-array", f"Column {col}: preparing burst", cells=len(all_reads), total=total)
            bitstream = self._ensure_array_bitstream(row_start, col)
            if self.config.dry_run:
                for row in range(row_start, row_end + 1):
                    all_reads.append(
                        {
                            "cell": asdict(CellAddress(row, col)),
                            "operation": "read",
                            "packet": f"0x{packet_for_cell(CellAddress(row, col), 0):04x}",
                            "rails": asdict(self.config.read_rails),
                            "current_uA": None,
                            "decoded_packet": "",
                            "ok": True,
                            "dry_run": True,
                        }
                    )
                self._append_progress("read-array", f"Column {col}: dry-run complete", cells=len(all_reads), total=total)
                continue
            index = self._next_index()
            attempts = max(1, self.config.attempts)
            local_output_dir: Path | None = None
            remote_output_dir = ""
            failures: list[str] = []
            for attempt in range(1, attempts + 1):
                attempt_note = f" attempt {attempt}" if attempts > 1 else ""
                self._append_progress("read-array", f"Column {col}: starting Saleae burst{attempt_note}", cells=len(all_reads), total=total)
                remote_output_dir = self._capture_array_burst(
                    packet,
                    self.config.read_rails,
                    bitstream,
                    index,
                    column_total,
                    row_start,
                    col,
                    f"column {col}",
                    cells_done=len(all_reads),
                    total_cells=total,
                )
                self._append_progress("read-array", f"Column {col}: copying capture", cells=len(all_reads), total=total)
                local_output_dir = self._copy_capture(remote_output_dir, index, f"read_array_col{col:02d}_burst", self.config.read_rails)
                self._append_progress("read-array", f"Column {col}: checking capture", cells=len(all_reads), total=total)
                validation_error = self._validate_burst_manifest(local_output_dir, column_total)
                if not validation_error:
                    break
                failures.append(f"attempt={attempt} {validation_error}")
                if attempt >= attempts:
                    raise RuntimeError(
                        f"Column {col} capture failed validation after {attempts} attempts: {validation_error}; "
                        f"see {local_output_dir / 'manifest.csv'}"
                    )
                if self._saleae_needs_restart(validation_error):
                    self._append_progress(
                        "read-array",
                        f"Column {col}: restarting capture service after validation error",
                        cells=len(all_reads),
                        total=total,
                    )
                    restart_log = self._restart_saleae_automation(index, f"read_array_col{col:02d}", attempt)
                    failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                self._append_progress("read-array", f"Column {col}: retrying capture", cells=len(all_reads), total=total)
                time.sleep(2.0)
            if local_output_dir is None:
                raise RuntimeError(f"Column {col} capture did not produce a local output directory")
            self._append_progress("read-array", f"Column {col}: decoding reads", cells=len(all_reads), total=total)
            reads = self._append_burst_manifest(local_output_dir, remote_output_dir, bitstream)
            all_reads.extend(reads)
            self._append_progress("read-array", f"Column {col}: decoded", cells=len(all_reads), total=total)
        summary = {
            "operation": "read-array",
            "mode": "burst-columns",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(all_reads),
            "rails": asdict(self.config.read_rails),
            "reads": all_reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def read_array_burst(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
    ) -> dict[str, object]:
        if (row_start, row_end, col_start, col_end) != (0, 31, 0, 31):
            raise ValueError("burst array read currently supports the full 32x32 array only; use mode='serial' for sub-ranges")
        cells = self._array_sweep_cells(row_start, col_start)
        packet = packet_for_cell(CellAddress(row_start, col_start), 0)
        self._append_progress("read-array", "Preparing burst bitstream", cells=0, total=len(cells))
        bitstream = self._ensure_array_bitstream(row_start, col_start)
        if self.config.dry_run:
            summary = {
                "operation": "read-array",
                "mode": "burst",
                "row_start": row_start,
                "row_end": row_end,
                "col_start": col_start,
                "col_end": col_end,
                "count": len(cells),
                "start_packet": f"0x{packet:04x}",
                "bitstream": bitstream,
                "rails": asdict(self.config.read_rails),
                "dry_run": True,
            }
            self._append_jsonl("array_reads.jsonl", summary)
            return summary

        self._ensure_saleae_burst_script()
        index = self._next_index()
        self._append_progress("read-array", "Starting Saleae burst capture", mode="burst")
        remote_output_dir = self._capture_array_burst(packet, self.config.read_rails, bitstream, index, len(cells), row_start, col_start, "full-array burst")
        self._append_progress("read-array", "Copying burst capture", mode="burst")
        local_output_dir = self._copy_capture(remote_output_dir, index, "read_array_burst", self.config.read_rails)
        self._append_progress("read-array", "Decoding burst reads", mode="burst")
        reads = self._append_burst_manifest(local_output_dir, remote_output_dir, bitstream)
        self._append_progress("read-array", "Burst read decoded", cells=len(reads), total=len(cells))
        summary = {
            "operation": "read-array",
            "mode": "burst",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(reads),
            "start_packet": f"0x{packet:04x}",
            "bitstream": bitstream,
            "remote_output_dir": remote_output_dir,
            "local_output_dir": str(local_output_dir),
            "reads": reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def set_cell(self, row: int, col: int = 0) -> dict[str, object]:
        return self._ramp_until(CellAddress(row, col), "set", self.config.set_sweep)

    def reset_cell(self, row: int, col: int = 0) -> dict[str, object]:
        return self._ramp_until(CellAddress(row, col), "reset", self.config.reset_sweep)

    def cycle_cell(self, row: int, col: int = 0) -> dict[str, object]:
        cell = CellAddress(row, col)
        initial = self.read(row, col)
        set_result = self.set_cell(row, col)
        reset_result = self.reset_cell(row, col)
        result = {
            "cell": asdict(cell),
            "initial_read_uA": initial.current_uA,
            "set": set_result,
            "reset": reset_result,
        }
        self._append_jsonl("cell_cycles.jsonl", result)
        return result

    def _ramp_until(self, cell: CellAddress, operation: Operation, sweep: SweepConfig) -> dict[str, object]:
        if operation not in ("set", "reset"):
            raise ValueError("ramp operation must be set or reset")
        results: list[dict[str, object]] = []
        best: CellOperationResult | None = None
        target_hit = False
        for vcc_set_v in sweep.vcc_set_v:
            for vcc_wl_set_v in sweep.vcc_wl_set_v:
                rails = RailVoltages(vcc_set_v, vcc_wl_set_v)
                pre_read: CellOperationResult | None = None
                if operation in ("set", "reset"):
                    pre_read = self._pulse_and_capture(cell, "read", self.config.read_rails, f"read_before_{operation}")
                    if pre_read.current_uA is not None and self._passes(pre_read.current_uA, sweep.threshold_uA, sweep.direction):
                        confirms = self.confirm_reads(cell, sweep.confirm_reads, sweep.threshold_uA, sweep.direction)
                        target_hit = all(
                            item.current_uA is not None and self._passes(item.current_uA, sweep.threshold_uA, sweep.direction)
                            for item in confirms
                        )
                        entry = {
                            "pre_read": asdict(pre_read),
                            "pulse": None,
                            "verify": asdict(pre_read),
                            "confirm_reads": [asdict(item) for item in confirms],
                            "threshold_uA": sweep.threshold_uA,
                            "direction": sweep.direction,
                            "skipped_pulse": target_hit,
                        }
                        results.append(entry)
                        best = pre_read
                        if target_hit and sweep.stop_on_threshold:
                            break
                pulse = self._pulse_and_capture(cell, operation, rails, f"{operation}_pulse")
                verify = self._pulse_and_capture(cell, "read", self.config.read_rails, f"read_after_{operation}")
                entry = {
                    "pre_read": asdict(pre_read) if pre_read else None,
                    "pulse": asdict(pulse),
                    "verify": asdict(verify),
                    "threshold_uA": sweep.threshold_uA,
                    "direction": sweep.direction,
                }
                results.append(entry)
                if verify.current_uA is not None and self._passes(verify.current_uA, sweep.threshold_uA, sweep.direction):
                    confirms = self.confirm_reads(cell, sweep.confirm_reads, sweep.threshold_uA, sweep.direction)
                    entry["confirm_reads"] = [asdict(item) for item in confirms]
                    target_hit = all(
                        item.current_uA is not None and self._passes(item.current_uA, sweep.threshold_uA, sweep.direction)
                        for item in confirms
                    )
                    best = verify
                    if target_hit and sweep.stop_on_threshold:
                        break
                if best is None or self._is_better(verify, best, sweep.direction):
                    best = verify
            if target_hit and sweep.stop_on_threshold:
                break
        summary = {
            "cell": asdict(cell),
            "operation": operation,
            "target_hit": target_hit,
            "best_read_uA": best.current_uA if best else None,
            "best_packet": best.packet if best else "",
            "steps": results,
        }
        self._append_jsonl("cell_operations.jsonl", summary)
        return summary

    def confirm_reads(
        self,
        cell: CellAddress,
        count: int,
        threshold_uA: float | None = None,
        direction: Literal["above", "below"] = "above",
    ) -> list[CellOperationResult]:
        out: list[CellOperationResult] = []
        for _ in range(count):
            result = self._pulse_and_capture(cell, "read", self.config.read_rails, "confirm_read")
            out.append(result)
            if threshold_uA is not None and (
                result.current_uA is None or not self._passes(result.current_uA, threshold_uA, direction)
            ):
                break
        return out

    def _pulse_and_capture(
        self,
        cell: CellAddress,
        operation: Operation,
        rails: RailVoltages,
        stage: str,
    ) -> CellOperationResult:
        cell.validate()
        op_set = 1 if operation == "set" else 0
        packet = packet_for_cell(cell, op_set)
        bitstream = self._ensure_bitstream(cell, op_set)
        index = self._next_index()
        kind = f"r{cell.row:02d}c{cell.col:02d}_{stage}_vcc{rails.vcc_set_v:.3f}_wl{rails.vcc_wl_set_v:.3f}".replace(".", "p")

        if self.config.dry_run:
            result = CellOperationResult(
                cell=cell,
                operation=operation,
                packet=f"0x{packet:04x}",
                rails=rails,
                current_uA=None,
                decoded_packet=f"0x{packet:04x}",
                ok=True,
                local_output_dir="DRY_RUN",
            )
            self._append_manifest(index, stage, kind, result, bitstream, bits_lsb(packet))
            return result

        summary_errors: list[str] = []
        for attempt in range(1, max(1, self.config.attempts) + 1):
            remote_output_dir = self._capture_remote(packet, rails, bitstream, index, kind)
            local_output_dir = self._copy_capture(remote_output_dir, index, kind, rails)
            try:
                summary = self._summarize_capture(index, stage, kind, packet, rails, remote_output_dir, local_output_dir)
                break
            except RuntimeError as exc:
                summary_errors.append(f"attempt={attempt}: {exc}")
                if attempt >= max(1, self.config.attempts):
                    raise RuntimeError(
                        f"capture summary failed index={index} kind={kind} after {attempt} attempts:\n"
                        + "\n".join(summary_errors)
                    ) from exc
                time.sleep(2.0)
        result = CellOperationResult(
            cell=cell,
            operation=operation,
            packet=f"0x{packet:04x}",
            rails=rails,
            current_uA=self._float_or_none(summary.get("la_set_window_mean_uA")),
            decoded_packet=str(summary.get("decoded_packet", "")),
            ok=str(summary.get("ok")) == "True",
            local_output_dir=str(local_output_dir),
            error=str(summary.get("error", "")),
        )
        self._append_manifest(index, stage, kind, result, bitstream, bits_lsb(packet))
        if not result.ok:
            raise RuntimeError(f"capture decoded incorrectly: expected 0x{packet:04x}, got {result.decoded_packet}: {result.error}")
        return result

    def _ensure_bitstream(self, cell: CellAddress, op_set: int) -> str:
        packet = packet_for_cell(cell, op_set)
        mode = "set" if op_set else "read"
        bit_name = f"caravel_scan_debug_fpga_{mode}{packet:04x}_fpga_reset_delay_repeat.bit"
        if self.config.dry_run:
            return bit_name

        if self._remote_file_exists(bit_name):
            return bit_name

        tcl_name = f"build_scan_debug_{mode}{packet:04x}_fpga_reset_delay_repeat.tcl"
        tcl = self._build_tcl(cell, op_set, bit_name)
        self._write_remote_text(tcl_name, tcl)
        self._run_zynq(f"{self.config.vivado_cmd} -mode batch -source {tcl_name}", timeout_s=900)
        return bit_name

    def _build_tcl(self, cell: CellAddress, op_set: int, bit_name: str) -> str:
        packet = packet_for_cell(cell, op_set)
        mode = "set" if op_set else "read"
        return f"""set script_dir [file dirname [file normalize [info script]]]
set part_name "xc7z020clg400-2"
set project_name "vivado_project_{mode}{packet:04x}_fpga_reset_delay_repeat"
set project_dir [file join $script_dir $project_name]
set bit_name "{bit_name}"
set xdc_file [file join $script_dir "caravel_scan_debug_fpga.xdc"]

if {{[file exists $project_dir]}} {{
    file delete -force $project_dir
}}

create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_scan_debug_fpga.v"]
set_property top caravel_scan_debug_fpga [current_fileset]
add_files -fileset constrs_1 $xdc_file

synth_design -top caravel_scan_debug_fpga -part $part_name -generic [list \\
    OP_SET={op_set} \\
    WL_SEL={cell.row} \\
    BL_SEL={cell.col} \\
    SL_SEL={cell.row} \\
    SEQUENCE_MODE=1 \\
    INITIAL_SEQUENCE_DELAY_CYCLES=10000000 \\
    SEQ_START_ROW={cell.row} \\
    SEQ_START_COL={cell.col} \\
    MANUAL_RESET_MODE=0 \\
    FPGA_RESET_ASSERT_CYCLES=240000 \\
    POST_RESET_WAIT_CYCLES=1000000 \\
    POST_DR_TM_HOLD_CYCLES=100 \\
    REPEAT_AFTER_DONE_CYCLES=0 \\
]
opt_design
place_design
route_design
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name fpga-reset delayed packet=0x{packet:04x}"
exit
"""

    def _ensure_array_bitstream(self, row_start: int, col_start: int) -> str:
        bit_name = f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col_start:02d}_burst.bit"
        if self.config.dry_run:
            return bit_name
        if self._remote_file_exists(bit_name):
            return bit_name
        cached = self._cached_array_bitstream(row_start, col_start)
        if cached.exists():
            self._write_remote_binary(bit_name, cached.read_bytes())
            return bit_name
        tcl_name = f"build_scan_debug_array_read_r{row_start:02d}c{col_start:02d}_burst.tcl"
        tcl = self._build_array_tcl(row_start, col_start, bit_name)
        self._write_remote_text(tcl_name, tcl)
        proc = self._run_zynq(f"{self.config.vivado_cmd} -mode batch -source {tcl_name}", timeout_s=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        return bit_name

    def prebuild_array_column_bitstreams(
        self,
        row_start: int = 0,
        col_start: int = 0,
        col_end: int = 31,
        *,
        force: bool = False,
    ) -> dict[str, object]:
        if not 0 <= row_start <= 31:
            raise ValueError(f"row_start must be 0..31, got {row_start}")
        if not 0 <= col_start <= col_end <= 31:
            raise ValueError(f"col range must be 0..31, got {col_start}..{col_end}")
        FPGA_BITSTREAM_DIR.mkdir(parents=True, exist_ok=True)
        built: list[str] = []
        cached: list[str] = []
        for col in range(col_start, col_end + 1):
            local_path = self._cached_array_bitstream(row_start, col)
            if local_path.exists() and not force:
                cached.append(local_path.name)
                continue
            bit_name = f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col:02d}_burst.bit"
            if self.config.dry_run:
                built.append(bit_name)
                continue
            if force and self._remote_file_exists(bit_name):
                self._remove_remote_file(bit_name)
            self._ensure_array_bitstream(row_start, col)
            self._copy_remote_binary_to_local(bit_name, local_path)
            built.append(local_path.name)
        return {
            "operation": "build-array-bitstreams",
            "row_start": row_start,
            "col_start": col_start,
            "col_end": col_end,
            "built": built,
            "cached": cached,
            "bitstream_dir": str(FPGA_BITSTREAM_DIR.relative_to(ROOT)),
        }

    @staticmethod
    def _cached_array_bitstream(row_start: int, col_start: int) -> Path:
        return FPGA_BITSTREAM_DIR / f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col_start:02d}_burst.bit"

    @staticmethod
    def _array_sweep_cells(row_start: int = 0, col_start: int = 0) -> list[CellAddress]:
        cells = [CellAddress(row, 0) for row in range(32)]
        for col in range(1, 32):
            cells.extend(CellAddress(row, col) for row in range(32))
        start = CellAddress(row_start, col_start)
        try:
            start_index = cells.index(start)
        except ValueError as exc:
            raise ValueError(f"start cell ({row_start},{col_start}) is not in the array sweep order") from exc
        return cells[start_index:]

    def _build_array_tcl(self, row_start: int, col_start: int, bit_name: str) -> str:
        return f"""set script_dir [file dirname [file normalize [info script]]]
set part_name "xc7z020clg400-2"
set project_name "vivado_project_array_read_r{row_start:02d}c{col_start:02d}_burst"
set project_dir [file join $script_dir $project_name]
set bit_name "{bit_name}"
set xdc_file [file join $script_dir "caravel_scan_debug_fpga.xdc"]

if {{[file exists $project_dir]}} {{
    file delete -force $project_dir
}}

create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_scan_debug_fpga.v"]
set_property top caravel_scan_debug_fpga [current_fileset]
add_files -fileset constrs_1 $xdc_file

synth_design -top caravel_scan_debug_fpga -part $part_name -generic [list \\
    OP_SET=0 \\
    SEQUENCE_MODE=1 \\
    INITIAL_SEQUENCE_DELAY_CYCLES={self.config.burst_initial_delay_cycles} \\
    REPEAT_AFTER_DONE_CYCLES={self.config.burst_repeat_after_done_cycles} \\
    SEQ_START_ROW={row_start} \\
    SEQ_START_COL={col_start} \\
]
opt_design
place_design
route_design
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name array-read burst start=({row_start},{col_start})"
exit
"""

    def _ensure_saleae_burst_script(self) -> None:
        script_path = ROOT / "api_v1/prerequisites/saleae_ubuntu/run_full_array_burst_capture.py"
        text = script_path.read_text()
        target = "run_full_array_burst_capture.py"
        if self.config.saleae_host:
            self._write_remote_saleae_text(target, text)
            return
        saleae_dir = Path(self.config.saleae_dir)
        saleae_dir.mkdir(parents=True, exist_ok=True)
        target_path = saleae_dir / target
        if not target_path.exists() or target_path.read_text() != text:
            target_path.write_text(text)

    def _write_remote_saleae_text(self, filename: str, text: str) -> None:
        encoded = base64.b64encode(text.encode()).decode()
        proc = self._run_saleae(
            f"python3 - <<'PY'\n"
            f"import base64, pathlib\n"
            f"pathlib.Path({filename!r}).write_bytes(base64.b64decode({encoded!r}))\n"
            f"PY",
            timeout_s=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _run_saleae(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        full_command = f"cd {self.config.saleae_dir} && {command}"
        if self.config.saleae_host:
            return self.runner.ssh(self.config.saleae_host, full_command, timeout_s=timeout_s)
        return self.runner.run(self._local_shell_command(full_command), timeout_s=timeout_s)

    def _capture_array_burst(
        self,
        packet: int,
        rails: RailVoltages,
        bitstream: str,
        index: int,
        max_cells: int,
        row_start: int,
        col_start: int,
        burst_label: str = "burst read",
        cells_done: int | None = None,
        total_cells: int | None = None,
    ) -> str:
        env = {
            "ADC_DAC_PORT": self.config.adc_dac_port,
            "DIGITAL_SAMPLE_RATE": str(self.config.digital_sample_rate),
            "ANALOG_SAMPLE_RATE": str(self.config.analog_sample_rate),
            "DIGITAL_THRESHOLD_VOLTS": str(self.config.digital_threshold_volts),
            "SHUNT_OHMS": str(self.config.shunt_ohms),
            "RAIL_COMMAND": rails.command,
            "VCC_SET_V": str(rails.vcc_set_v),
            "VCC_WL_SET_V": str(rails.vcc_wl_set_v),
            "ENABLE_ADC_MONITOR": "1" if self.config.enable_adc_monitor else "0",
            "START_ROW": str(row_start),
            "START_COL": str(col_start),
            "MAX_CELLS": str(max_cells),
            "AFTER_TRIGGER_SECONDS": str(self.config.burst_after_trigger_seconds),
            "TRIM_DATA_SECONDS": str(self.config.burst_trim_data_seconds),
            "STOP_ON_MISMATCH": "0",
        }
        env_text = " ".join(f"{k}={self._sh_quote(v)}" for k, v in env.items())
        capture_cmd = f"env {env_text} {self.config.saleae_burst_capture_script}"
        capture_log = self.config.run_dir / f"capture_{index}_read_array_burst.log"
        attempts = max(1, self.config.attempts)
        failures: list[str] = []
        restarted_saleae = False
        progress_kwargs = {
            "cells": cells_done,
            "total": total_cells,
        } if cells_done is not None and total_cells is not None else {}
        for attempt in range(1, attempts + 1):
            attempt_log = capture_log if attempts == 1 else self.config.run_dir / f"capture_{index}_read_array_burst_attempt{attempt}.log"
            capture_proc = self._popen_saleae(capture_cmd)
            time.sleep(2.0)
            self._append_progress("read-array", f"Programming FPGA for {burst_label}", mode="burst")
            program_rc = self._program_fpga(bitstream)
            self._append_progress("read-array", f"Saleae capturing {burst_label}", mode="burst")
            timed_out = False
            try:
                output, _ = capture_proc.communicate(timeout=self.config.burst_capture_timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                capture_proc.kill()
                output, _ = capture_proc.communicate()
                output = (output or "") + f"\nTIMEOUT after {self.config.burst_capture_timeout_seconds:.0f}s waiting for Saleae burst capture\n"
            attempt_log.write_text(output or "")
            remote_output_dir = ""
            for line in (output or "").splitlines():
                if line.startswith("OUTPUT_ROOT="):
                    remote_output_dir = line.split("=", 1)[1].strip()
                elif line.startswith("DONE output_root="):
                    remote_output_dir = line.split("output_root=", 1)[1].split()[0].strip()
            capture_rc = capture_proc.returncode
            if capture_rc == 0 and program_rc == 0 and remote_output_dir:
                if attempts > 1:
                    capture_log.write_text(f"SUCCESS attempt={attempt}; see {attempt_log}\n")
                return remote_output_dir

            reason = (
                f"attempt={attempt} capture_rc={capture_rc} program_rc={program_rc} "
                f"remote_output_dir={remote_output_dir or '<missing>'} log={attempt_log}"
            )
            if timed_out:
                reason += " timeout=true"
            failures.append(reason)
            if attempt < attempts:
                should_restart = timed_out or self._saleae_needs_restart(output or "")
                if should_restart:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: restarting capture service after attempt {attempt}",
                        mode="burst",
                        **progress_kwargs,
                    )
                    restart_log = self._restart_saleae_automation(index, "read_array_burst", attempt)
                    failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                    restarted_saleae = True
                elif restarted_saleae:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: retrying capture after restart",
                        mode="burst",
                        **progress_kwargs,
                    )
                else:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: retrying capture after attempt {attempt}",
                        mode="burst",
                        **progress_kwargs,
                    )
                time.sleep(2.0)

        capture_log.write_text("\n".join(failures) + "\n")
        raise RuntimeError(
            f"burst capture/program failed {burst_label} after {attempts} attempts; "
            f"see {capture_log}"
        )

    def _append_burst_manifest(self, local_output_dir: Path, remote_output_dir: str, bitstream: str) -> list[dict[str, object]]:
        burst_manifest = local_output_dir / "manifest.csv"
        if not burst_manifest.exists():
            raise RuntimeError(f"burst manifest missing: {burst_manifest}")
        reads: list[dict[str, object]] = []
        next_index = self._next_index()
        with burst_manifest.open(newline="") as handle:
            for offset, row in enumerate(csv.DictReader(handle)):
                cell = CellAddress(int(row["row"]), int(row["col"]))
                packet_text = str(row["packet"])
                current = self._float_or_none(row.get("la_set_mean_uA"))
                result = CellOperationResult(
                    cell=cell,
                    operation="read",
                    packet=packet_text,
                    rails=self.config.read_rails,
                    current_uA=current,
                    decoded_packet=str(row.get("decoded_packet", "")),
                    ok=str(row.get("ok")) == "True",
                    local_output_dir=str(local_output_dir),
                    error=str(row.get("error", "")),
                )
                packet = int(packet_text, 16)
                self._append_manifest(next_index + offset, "array_burst", "read", result, bitstream, bits_lsb(packet))
                reads.append(
                    {
                        "cell": asdict(cell),
                        "operation": "read",
                        "packet": packet_text,
                        "rails": asdict(self.config.read_rails),
                        "current_uA": current,
                        "decoded_packet": result.decoded_packet,
                        "ok": result.ok,
                        "local_output_dir": str(local_output_dir),
                        "remote_output_dir": remote_output_dir,
                        "error": result.error,
                    }
                )
                if (offset + 1) % 64 == 0:
                    self._append_progress("read-array", "Publishing burst reads", cells=offset + 1)
        return reads

    def _validate_burst_manifest(self, local_output_dir: Path, expected_count: int) -> str:
        burst_manifest = local_output_dir / "manifest.csv"
        if not burst_manifest.exists():
            return f"burst manifest missing: {burst_manifest}"
        with burst_manifest.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) < expected_count:
            return f"burst manifest has {len(rows)} rows, expected {expected_count}"
        saleae_errors = [
            str(row.get("error", ""))
            for row in rows
            if row.get("error") and self._saleae_needs_restart(str(row.get("error", "")))
        ]
        if saleae_errors:
            return saleae_errors[0][:220]
        return ""

    def _capture_remote(self, packet: int, rails: RailVoltages, bitstream: str, index: int, kind: str) -> str:
        env = {
            "ADC_DAC_PORT": self.config.adc_dac_port,
            "RESET_MODE": "none",
            "PRE_RESET_DELAY_SECONDS": "0",
            "TRIGGER_CHANNEL": str(self.config.trigger_channel),
            "TRIGGER_EDGE": self.config.trigger_edge,
            "AFTER_TRIGGER_SECONDS": str(self.config.after_trigger_seconds),
            "TRIM_DATA_SECONDS": str(self.config.trim_data_seconds),
            "DIGITAL_SAMPLE_RATES": str(self.config.digital_sample_rate),
            "ANALOG_SAMPLE_RATE": str(self.config.analog_sample_rate),
            "ANALOG_CHANNELS": self.config.analog_channels,
            "DIGITAL_THRESHOLD_VOLTS": str(self.config.digital_threshold_volts),
            "SHUNT_OHMS": str(self.config.shunt_ohms),
            "ENABLE_ADC_MONITOR": "1" if self.config.enable_adc_monitor else "0",
            "SCAN_REQUEST": f"0x{packet:04x}",
            "SCAN_RAIL_COMMAND": rails.command,
            "VCC_SET_V": str(rails.vcc_set_v),
            "VCC_WL_SET_V": str(rails.vcc_wl_set_v),
        }
        env_text = " ".join(f"{k}={self._sh_quote(v)}" for k, v in env.items())
        capture_cmd = f"cd {self.config.saleae_dir} && env {env_text} {self.config.saleae_capture_script}"
        capture_log = self.config.run_dir / f"capture_{index}_{kind}.log"
        attempts = max(1, self.config.attempts)
        failures: list[str] = []
        restarted_saleae = False
        for attempt in range(1, attempts + 1):
            attempt_log = capture_log if attempts == 1 else self.config.run_dir / f"capture_{index}_{kind}_attempt{attempt}.log"
            capture_proc = self._popen_saleae(capture_cmd)
            time.sleep(2.0)
            program_rc = self._program_fpga(bitstream)
            output, _ = capture_proc.communicate()
            attempt_log.write_text(output or "")
            remote_output_dir = ""
            for line in (output or "").splitlines():
                if line.startswith("OUTPUT_DIR="):
                    remote_output_dir = line.split("=", 1)[1].strip()
            if capture_proc.returncode == 0 and program_rc == 0 and remote_output_dir:
                if attempts > 1:
                    capture_log.write_text(f"SUCCESS attempt={attempt}; see {attempt_log}\n")
                return remote_output_dir

            failures.append(
                f"attempt={attempt} capture_rc={capture_proc.returncode} program_rc={program_rc} "
                f"remote_output_dir={remote_output_dir or '<missing>'} log={attempt_log}"
            )
            if attempt < attempts:
                if not restarted_saleae and self._saleae_needs_restart(output or ""):
                    restart_log = self._restart_saleae_automation(index, kind, attempt)
                    failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                    restarted_saleae = True
                time.sleep(2.0)

        capture_log.write_text("\n".join(failures) + "\n")
        raise RuntimeError(f"capture/program failed index={index} kind={kind} after {attempts} attempts; see {capture_log}")

    def _saleae_needs_restart(self, output: str) -> bool:
        restart_markers = (
            "Failed to connect to remote host: Connection refused",
            "Connection refused",
            "DeviceSetupFailure",
            "failed to connect to all addresses",
            "StatusCode.UNAVAILABLE",
            "_InactiveRpcError",
        )
        return any(marker in output for marker in restart_markers)

    def _restart_saleae_automation(self, index: int, kind: str, attempt: int) -> Path:
        restart_log = self.config.run_dir / f"saleae_restart_{index}_{kind}_after_attempt{attempt}.log"
        command = self.config.saleae_restart_script
        if self.config.saleae_host:
            proc = self.runner.ssh(
                self.config.saleae_host,
                f"cd {self.config.saleae_dir} && {command}; sleep {self.config.saleae_restart_wait_seconds}; ss -ltnp | grep 10430 || true",
                timeout_s=60,
            )
        else:
            proc = self.runner.run(
                self._local_shell_command(
                    f"cd {self.config.saleae_dir} && {command}; sleep {self.config.saleae_restart_wait_seconds}; ss -ltnp | grep 10430 || true"
                ),
                timeout_s=60,
            )
        restart_log.write_text(proc.stdout or "")
        return restart_log

    def _program_fpga(self, bitstream: str) -> int:
        if self.config.zynq_os == "windows":
            command = (
                f"Copy-Item -Force {bitstream} caravel_scan_debug_fpga.bit; "
                f"{self.config.vivado_cmd} -mode batch -source program_scan_debug_zynq7020.tcl"
            )
            proc = self._run_zynq_powershell(command, timeout_s=180)
        else:
            proc = self._run_zynq(
                f"cp -f {self._sh_quote(bitstream)} caravel_scan_debug_fpga.bit && "
                f"{self.config.vivado_cmd} -mode batch -source program_scan_debug_zynq7020.tcl",
                timeout_s=180,
            )
        return proc.returncode

    def _copy_capture(self, remote_output_dir: str, index: int, kind: str, rails: RailVoltages) -> Path:
        local = self.config.run_dir / "raw" / f"{index}_{kind}_wl{round(rails.vcc_wl_set_v * 1000):.0f}_{Path(remote_output_dir).name}"
        if local.exists():
            shutil.rmtree(local)
        if self.config.saleae_host:
            proc = self.runner.run(["rsync", "-a", f"{self.config.saleae_host}:{remote_output_dir}/", f"{local}/"])
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout)
        else:
            shutil.copytree(remote_output_dir, local)
        return local

    def _summarize_capture(
        self,
        index: int,
        stage: str,
        kind: str,
        packet: int,
        rails: RailVoltages,
        remote_output_dir: str,
        local_output_dir: Path,
    ) -> dict[str, str]:
        tmp = self.config.run_dir / f"manifest_tmp_{index}_{kind}.csv"
        tmp.write_text(
            "index,phase,vcc_set_V,vcc_wl_set_V,packet,bits_lsb_first,remote_output_dir,local_output_dir,"
            "ok,decoded_packet,la_set_window_mean_uA,la_reset_window_mean_uA,adc_read_uA,adc_set_uA,adc_reset_uA,error\n"
        )
        proc = self.runner.run(
            [
                sys.executable,
                str(self.config.summarizer),
                "--index",
                str(index),
                "--phase",
                stage,
                "--packet",
                f"0x{packet:04x}",
                "--bits",
                bits_lsb(packet),
                "--vcc-set-v",
                str(rails.vcc_set_v),
                "--vcc-wl-set-v",
                str(rails.vcc_wl_set_v),
                "--remote-output-dir",
                remote_output_dir,
                "--local-output-dir",
                str(local_output_dir),
                "--manifest",
                str(tmp),
            ]
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        with tmp.open(newline="") as handle:
            return list(csv.DictReader(handle))[-1]

    def _ensure_manifest(self) -> None:
        if self.manifest.exists():
            return
        with self.manifest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()

    def _append_manifest(
        self,
        index: int,
        stage: str,
        kind: str,
        result: CellOperationResult,
        bitstream: str,
        bits: str,
    ) -> None:
        with self.manifest.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writerow(
                {
                    "index": index,
                    "stage": stage,
                    "kind": kind,
                    "cell": result.cell.label,
                    "operation": result.operation,
                    "packet": result.packet,
                    "vcc_set_V": result.rails.vcc_set_v,
                    "vcc_wl_set_V": result.rails.vcc_wl_set_v,
                    "bits_lsb_first": bits,
                    "bitstream": bitstream,
                    "ok": result.ok,
                    "decoded_packet": result.decoded_packet,
                    "la_set_window_mean_uA": result.current_uA,
                    "local_output_dir": result.local_output_dir,
                    "error": result.error,
                }
            )

    def _next_index(self) -> int:
        with self.manifest.open(newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))

    def _append_jsonl(self, filename: str, item: dict[str, object]) -> None:
        with (self.config.run_dir / filename).open("a") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")

    def _append_progress(self, operation: str, message: str, **extra: object) -> None:
        item = {
            "operation": operation,
            "message": message,
            "time": time.time(),
            **extra,
        }
        self._append_jsonl("progress.jsonl", item)
        print(json.dumps({"progress": item}, sort_keys=True), flush=True)

    def _remote_file_exists(self, filename: str) -> bool:
        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(f"if (Test-Path '{filename}') {{ exit 0 }} else {{ exit 1 }}", timeout_s=60)
        else:
            proc = self._run_zynq(f"test -f {self._sh_quote(filename)}", timeout_s=60)
        return proc.returncode == 0

    def _write_remote_text(self, filename: str, text: str) -> None:
        encoded = base64.b64encode(text.encode()).decode()
        if self.config.zynq_os == "windows":
            cmd = (
                f"$b='{encoded}'; "
                f"[IO.File]::WriteAllText('{filename}', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)))"
            )
            proc = self._run_zynq_powershell(cmd, timeout_s=60)
        else:
            proc = self._run_zynq(
                f"python3 - <<'PY'\n"
                f"import base64, pathlib\n"
                f"pathlib.Path({filename!r}).write_bytes(base64.b64decode({encoded!r}))\n"
                f"PY",
                timeout_s=60,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _write_remote_binary(self, filename: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode()
        if self.config.zynq_os == "windows":
            cmd = f"$b='{encoded}'; [IO.File]::WriteAllBytes('{filename}', [Convert]::FromBase64String($b))"
            proc = self._run_zynq_powershell(cmd, timeout_s=180)
        else:
            proc = self._run_zynq(
                f"python3 - <<'PY'\n"
                f"import base64, pathlib\n"
                f"pathlib.Path({filename!r}).write_bytes(base64.b64decode({encoded!r}))\n"
                f"PY",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _copy_remote_binary_to_local(self, filename: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config.zynq_host:
            source = Path(self.config.zynq_dir) / filename
            shutil.copy2(source, local_path)
            return
        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(
                f"Write-Output '__BITSTREAM_B64_BEGIN__'; "
                f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{filename}')); "
                f"Write-Output '__BITSTREAM_B64_END__'",
                timeout_s=180,
            )
        else:
            proc = self._run_zynq(
                f"echo __BITSTREAM_B64_BEGIN__; base64 {self._sh_quote(filename)}; echo __BITSTREAM_B64_END__",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        match = re.search(r"__BITSTREAM_B64_BEGIN__\s*(.*?)\s*__BITSTREAM_B64_END__", proc.stdout, re.S)
        if not match:
            raise RuntimeError(f"could not find bitstream payload in remote output for {filename}")
        payload = re.sub(r"[^A-Za-z0-9+/=]", "", match.group(1))
        payload += "=" * (-len(payload) % 4)
        local_path.write_bytes(base64.b64decode(payload))

    def _remove_remote_file(self, filename: str) -> None:
        if self.config.zynq_os == "windows":
            self._run_zynq_powershell(f"if (Test-Path '{filename}') {{ Remove-Item -Force '{filename}' }}", timeout_s=60)
        else:
            self._run_zynq(f"rm -f {self._sh_quote(filename)}", timeout_s=60)

    def _run_zynq_powershell(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(command.encode("utf-16le")).decode()
        return self._run_zynq(f"powershell -NoProfile -EncodedCommand {encoded}", timeout_s=timeout_s)

    def _run_zynq(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        full_command = f"cd {self.config.zynq_dir} && {command}"
        if self.config.zynq_host:
            if self.config.zynq_password:
                return self.runner.ssh_with_expect_password(
                    self.config.zynq_host,
                    self.config.zynq_password,
                    full_command,
                    timeout_s=timeout_s,
                )
            return self.runner.ssh(self.config.zynq_host, full_command, timeout_s=timeout_s)
        return self.runner.run(self._local_shell_command(full_command), timeout_s=timeout_s)

    def _popen_saleae(self, command: str) -> subprocess.Popen[str]:
        full_command = f"cd {self.config.saleae_dir} && {command}"
        if self.config.saleae_host:
            return subprocess.Popen(["ssh", self.config.saleae_host, full_command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return subprocess.Popen(self._local_shell_command(full_command), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @staticmethod
    def _local_shell_command(command: str) -> list[str]:
        if platform.system().lower().startswith("win"):
            return ["powershell", "-NoProfile", "-Command", command]
        return ["bash", "-lc", command]

    @staticmethod
    def _passes(value: float, threshold: float, direction: Literal["above", "below"]) -> bool:
        return value > threshold if direction == "above" else value < threshold

    @staticmethod
    def _is_better(candidate: CellOperationResult, current: CellOperationResult, direction: Literal["above", "below"]) -> bool:
        if candidate.current_uA is None:
            return False
        if current.current_uA is None:
            return True
        return candidate.current_uA > current.current_uA if direction == "above" else candidate.current_uA < current.current_uA

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        try:
            out = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    @staticmethod
    def _sh_quote(value: object) -> str:
        text = str(value)
        return "'" + text.replace("'", "'\"'\"'") + "'"
