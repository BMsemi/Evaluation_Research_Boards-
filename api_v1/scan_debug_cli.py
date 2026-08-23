#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

try:
    from .cell_api import RailVoltages, ScanDebugCellAPI, ScanDebugConfig, SweepConfig
except ImportError:
    from cell_api import RailVoltages, ScanDebugCellAPI, ScanDebugConfig, SweepConfig


def parse_sweep(text: str) -> tuple[float, ...]:
    """Parse `0.5:2.0:0.1` or `2.0,2.3,2.4` into voltages."""

    if ":" in text:
        start, stop, step = [float(part) for part in text.split(":")]
        values = []
        value = start
        while value <= stop + (step / 2):
            values.append(round(value, 6))
            value += step
        return tuple(values)
    return tuple(float(part) for part in text.split(",") if part)


def build_config(args: argparse.Namespace) -> ScanDebugConfig:
    return ScanDebugConfig(
        run_dir=Path(args.run_dir),
        dry_run=args.dry_run,
        attempts=args.attempts,
        read_rails=RailVoltages(args.read_vcc_set, args.read_vcc_wl_set),
        shunt_ohms=args.shunt_ohms,
        zynq_host=args.zynq_host or None,
        zynq_password=args.zynq_password or None,
        zynq_os=args.zynq_os,
        zynq_dir=args.zynq_dir,
        vivado_cmd=args.vivado_cmd,
        saleae_host=args.saleae_host or None,
        saleae_dir=args.saleae_dir,
        saleae_restart_script=args.saleae_restart_script,
        saleae_restart_wait_seconds=args.saleae_restart_wait_seconds,
        adc_dac_port=args.adc_dac_port,
        burst_initial_delay_cycles=args.burst_initial_delay_cycles,
        burst_repeat_after_done_cycles=args.burst_repeat_cycles,
        burst_capture_timeout_seconds=args.burst_capture_timeout_seconds,
        set_sweep=SweepConfig.from_ranges(
            vcc_set_v=parse_sweep(args.set_vcc_set),
            vcc_wl_set_v=parse_sweep(args.set_vcc_wl_set),
            threshold_uA=args.set_threshold,
            direction="above",
            confirm_reads=args.confirm_reads,
        ),
        reset_sweep=SweepConfig.from_ranges(
            vcc_set_v=parse_sweep(args.reset_vcc_set),
            vcc_wl_set_v=parse_sweep(args.reset_vcc_wl_set),
            threshold_uA=args.reset_threshold,
            direction="below",
            confirm_reads=args.confirm_reads,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan-debug cell read/set/reset API CLI")
    parser.add_argument("operation", choices=["read", "set", "reset", "cycle", "read-array", "build-array-bitstreams"])
    parser.add_argument("--row", type=int)
    parser.add_argument("--col", type=int, default=0)
    parser.add_argument("--row-start", type=int, default=0)
    parser.add_argument("--row-end", type=int, default=31)
    parser.add_argument("--col-start", type=int, default=0)
    parser.add_argument("--col-end", type=int, default=31)
    parser.add_argument("--array-mode", choices=["burst", "burst-columns", "serial"], default="burst-columns")
    parser.add_argument("--force-bitstreams", action="store_true")
    parser.add_argument("--run-dir", default=f"api_v1/runs/run_{time.strftime('%Y%m%d_%H%M%S_IST')}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--attempts", type=int, default=int(os.environ.get("SCAN_DEBUG_ATTEMPTS", "3")))
    parser.add_argument("--confirm-reads", type=int, default=10)
    parser.add_argument("--shunt-ohms", type=float, default=470.0)

    parser.add_argument("--read-vcc-set", type=float, default=1.0)
    parser.add_argument("--read-vcc-wl-set", type=float, default=2.5)
    parser.add_argument("--set-vcc-set", default="1.6,2.0,2.3,2.4,2.5,2.8")
    parser.add_argument("--set-vcc-wl-set", default="0.5,0.7,0.9,1.1,1.3,1.5,1.7,1.9,2.0")
    parser.add_argument("--reset-vcc-set", default="3.5")
    parser.add_argument("--reset-vcc-wl-set", default="0.5,0.7,0.9,1.1,1.3,1.5,1.7,1.9,2.0")
    parser.add_argument("--set-threshold", type=float, default=200.0)
    parser.add_argument("--reset-threshold", type=float, default=130.0)

    parser.add_argument("--zynq-host", default=os.environ.get("SCAN_DEBUG_ZYNQ_HOST", "geethika@100.116.216.70"))
    parser.add_argument("--zynq-password", default=os.environ.get("SCAN_DEBUG_ZYNQ_PASSWORD", ""))
    parser.add_argument("--zynq-os", choices=["windows", "posix"], default=os.environ.get("SCAN_DEBUG_ZYNQ_OS", "windows"))
    parser.add_argument("--zynq-dir", default=os.environ.get("SCAN_DEBUG_ZYNQ_DIR", "C:/Users/geethika/zynq_scan_debug"))
    parser.add_argument("--vivado-cmd", default=os.environ.get("SCAN_DEBUG_VIVADO_CMD", "C:/Xilinx/Vivado/2019.1/bin/vivado.bat"))
    parser.add_argument("--saleae-host", default=os.environ.get("SCAN_DEBUG_SALEAE_HOST", "ubuntu-24-04@100.98.132.51"))
    parser.add_argument("--saleae-dir", default=os.environ.get("SCAN_DEBUG_SALEAE_DIR", "/home/ubuntu-24-04/saleae-api"))
    parser.add_argument("--saleae-restart-script", default=os.environ.get("SCAN_DEBUG_SALEAE_RESTART_SCRIPT", "./start-logic2-automation.sh"))
    parser.add_argument("--saleae-restart-wait-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_SALEAE_RESTART_WAIT_SECONDS", "10")))
    parser.add_argument("--burst-initial-delay-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_INITIAL_DELAY_CYCLES", "40000000")))
    parser.add_argument("--burst-repeat-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_REPEAT_CYCLES", "10000000")))
    parser.add_argument(
        "--burst-capture-timeout-seconds",
        type=float,
        default=float(os.environ.get("SCAN_DEBUG_BURST_CAPTURE_TIMEOUT_SECONDS", "420")),
    )
    parser.add_argument(
        "--adc-dac-port",
        default=os.environ.get("SCAN_DEBUG_ADC_DAC_PORT", "/dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00"),
    )
    args = parser.parse_args()
    if args.operation not in {"read-array", "build-array-bitstreams"} and args.row is None:
        parser.error("--row is required unless operation is read-array or build-array-bitstreams")

    api = ScanDebugCellAPI(build_config(args))
    if args.operation == "read":
        result = api.read(args.row, args.col)
    elif args.operation == "set":
        result = api.set_cell(args.row, args.col)
    elif args.operation == "reset":
        result = api.reset_cell(args.row, args.col)
    elif args.operation == "cycle":
        result = api.cycle_cell(args.row, args.col)
    elif args.operation == "read-array":
        result = api.read_array(args.row_start, args.row_end, args.col_start, args.col_end, mode=args.array_mode)
    else:
        result = api.prebuild_array_column_bitstreams(
            row_start=args.row_start,
            col_start=args.col_start,
            col_end=args.col_end,
            force=args.force_bitstreams,
        )
    print(json.dumps(result if isinstance(result, dict) else result.__dict__, default=lambda item: item.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
