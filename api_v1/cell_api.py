#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import math
import os
import platform
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
    read_rails: RailVoltages = field(default_factory=lambda: RailVoltages(1.2, 2.5))
    set_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=(2.0, 2.3, 2.4, 2.5),
            vcc_wl_set_v=(0.5, 0.8, 1.1, 1.4, 1.7, 2.0),
            threshold_uA=150.0,
            direction="above",
        )
    )
    reset_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=(2.0, 2.3, 2.6, 2.9, 3.2, 3.5),
            vcc_wl_set_v=(0.5, 0.8, 1.1, 1.4, 1.7, 2.0),
            threshold_uA=100.0,
            direction="below",
        )
    )
    attempts: int = 3
    shunt_ohms: float = 1000.0
    dry_run: bool = False

    zynq_host: str | None = "geethika@100.116.216.70"
    zynq_password: str | None = None
    zynq_os: Literal["windows", "posix"] = "windows"
    zynq_dir: str = "C:/Users/geethika/zynq_scan_debug"
    vivado_cmd: str = "C:/Xilinx/Vivado/2019.1/bin/vivado.bat"

    saleae_host: str | None = "ubuntu-24-04@100.98.132.51"
    saleae_dir: str = "/home/ubuntu-24-04/saleae-api"
    saleae_capture_script: str = ".venv/bin/python run_fpga_scan0000_la12_15_capture.py"
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
                pulse = self._pulse_and_capture(cell, operation, rails, f"{operation}_pulse")
                verify = self._pulse_and_capture(cell, "read", self.config.read_rails, f"read_after_{operation}")
                entry = {
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

        remote_output_dir = self._capture_remote(packet, rails, bitstream, index, kind)
        local_output_dir = self._copy_capture(remote_output_dir, index, kind, rails)
        summary = self._summarize_capture(index, stage, kind, packet, rails, remote_output_dir, local_output_dir)
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
        capture_proc = self._popen_saleae(capture_cmd)
        time.sleep(2.0)
        program_rc = self._program_fpga(bitstream)
        output, _ = capture_proc.communicate()
        capture_log.write_text(output or "")
        remote_output_dir = ""
        for line in (output or "").splitlines():
            if line.startswith("OUTPUT_DIR="):
                remote_output_dir = line.split("=", 1)[1].strip()
        if capture_proc.returncode != 0 or program_rc != 0 or not remote_output_dir:
            raise RuntimeError(f"capture/program failed index={index} kind={kind}; see {capture_log}")
        return remote_output_dir

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
