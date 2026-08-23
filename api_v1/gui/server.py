#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "api_v1" / "runs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
GRID_SIZE = 32

try:
    from api_v1.cell_api import ScanDebugConfig

    DEFAULT_THRESHOLDS_UA = {
        "set": ScanDebugConfig().set_sweep.threshold_uA,
        "reset": ScanDebugConfig().reset_sweep.threshold_uA,
    }
except Exception:
    DEFAULT_THRESHOLDS_UA = {"set": 166.7, "reset": 108.3}


@dataclass(frozen=True)
class ViewerConfig:
    runs_dir: Path
    allow_commands: bool


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_cell(value: str | None) -> dict[str, int] | None:
    if not value or "_" not in value:
        return None
    row_text, col_text = value.split("_", 1)
    row = _int_or_none(row_text)
    col = _int_or_none(col_text)
    if row is None or col is None:
        return None
    if not (0 <= row < GRID_SIZE and 0 <= col < GRID_SIZE):
        return None
    return {"row": row, "col": col}


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(newline="") as handle:
            for raw in csv.DictReader(handle):
                cell = _parse_cell(raw.get("cell"))
                row: dict[str, Any] = dict(raw)
                row["index"] = _int_or_none(raw.get("index"))
                row["cellAddress"] = cell
                row["current_uA"] = _float_or_none(raw.get("la_set_window_mean_uA"))
                row["vcc_set_V"] = _float_or_none(raw.get("vcc_set_V"))
                row["vcc_wl_set_V"] = _float_or_none(raw.get("vcc_wl_set_V"))
                row["ok"] = str(raw.get("ok", "")).lower() == "true"
                row["eventOrder"] = (row["index"] or 0) + 0.9
                rows.append(row)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    rows.sort(key=lambda item: item.get("index") if item.get("index") is not None else -1)
    return rows


def _manifest_for_run(run_dir: Path) -> Path:
    return run_dir / "manifest.csv"


def _run_updated_at(run_dir: Path) -> float:
    files = [path for path in run_dir.glob("manifest.csv")] + list(run_dir.glob("*.log")) + list(run_dir.glob("progress.jsonl"))
    if not files:
        return run_dir.stat().st_mtime
    return max(path.stat().st_mtime for path in files)


def _latest_run(runs_dir: Path) -> Path | None:
    run_dirs = sorted((path.parent for path in runs_dir.glob("*/manifest.csv")), key=_run_updated_at, reverse=True)
    return run_dirs[0] if run_dirs else None


def _recent_log_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log_path in sorted(run_dir.glob("*.log"), key=lambda path: path.stat().st_mtime)[-12:]:
        try:
            text = log_path.read_text(errors="replace")
        except OSError:
            continue
        message = _extract_error_message(text)
        if not message:
            continue
        events.append(
            {
                "source": "log",
                "index": log_path.stem,
                "ok": False,
                "message": message,
                "path": str(log_path.relative_to(ROOT)),
                "updated": log_path.stat().st_mtime,
                "eventOrder": log_path.stat().st_mtime * 1000,
            }
        )
    return events[-4:]


def _read_progress_events(run_dir: Path) -> list[dict[str, Any]]:
    progress_path = run_dir / "progress.jsonl"
    events: list[dict[str, Any]] = []
    try:
        lines = progress_path.read_text(errors="replace").splitlines()
    except OSError:
        return events
    for index, line in enumerate(lines[-12:]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_time = _float_or_none(item.get("time"))
        events.append(
            {
                "source": "progress",
                "index": f"progress_{index}",
                "operation": item.get("operation", "read-array"),
                "message": item.get("message", "Processing"),
                "cells": _int_or_none(item.get("cells")),
                "total": _int_or_none(item.get("total")),
                "ok": True,
                "updated": event_time or progress_path.stat().st_mtime,
                "eventOrder": (event_time or progress_path.stat().st_mtime) * 1000,
            }
        )
    return events[-4:]


def _log_event_order(path: Path) -> float:
    match = re.search(r"capture_(\d+).*?_attempt(\d+)", path.name)
    if match:
        return int(match.group(1)) + min(int(match.group(2)), 80) / 100
    match = re.search(r"capture_(\d+)", path.name)
    if match:
        return int(match.group(1)) + 0.1
    return 0.0


def _extract_error_message(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('{"progress":'):
            continue
        lines.append(line)
    priority = ("permission denied", "readtimeout", "deviceerror", "runtimeerror", "traceback", "exception", "error")
    for keyword in priority:
        for line in reversed(lines):
            if keyword in line.lower():
                return line[-220:]
    for line in reversed(lines):
        if "error" in line.lower() or "traceback" in line.lower() or "exception" in line.lower():
            return line[-220:]
    return ""


def _saleae_error_needs_restart(text: str) -> bool:
    markers = (
        "Failed to connect to remote host: Connection refused",
        "Connection refused",
        "DeviceSetupFailure",
        "failed to connect to all addresses",
        "StatusCode.UNAVAILABLE",
        "_InactiveRpcError",
    )
    return any(marker in text for marker in markers)


def _latest_manifest_mtime(run_dir: Path) -> float:
    manifest = _manifest_for_run(run_dir)
    return manifest.stat().st_mtime if manifest.exists() else 0.0


def _active_error(run_dir: Path, rows: list[dict[str, Any]], log_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not log_events:
        return None
    last_log_error = log_events[-1]
    last_row_order = float(rows[-1].get("eventOrder") or -1) if rows else -1
    last_error_order = float(last_log_error.get("eventOrder") or -1)
    if rows and rows[-1].get("ok") and last_row_order > last_error_order:
        return None
    return last_log_error


def _read_thresholds(run_dir: Path) -> dict[str, float]:
    thresholds = dict(DEFAULT_THRESHOLDS_UA)
    operations_path = run_dir / "cell_operations.jsonl"
    try:
        lines = operations_path.read_text().splitlines()
    except OSError:
        return thresholds
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        operation = item.get("operation")
        steps = item.get("steps") or []
        for step in steps:
            threshold = _float_or_none(str(step.get("threshold_uA")) if step.get("threshold_uA") is not None else None)
            if operation in thresholds and threshold is not None:
                thresholds[operation] = threshold
    return thresholds


def _parse_scan_debug_process(command: str) -> dict[str, Any]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    script_index = next((index for index, part in enumerate(parts) if part.endswith("scan_debug_cli.py")), -1)
    operation = parts[script_index + 1] if script_index >= 0 and script_index + 1 < len(parts) else "api"

    def flag_value(flag: str) -> str | None:
        if flag not in parts:
            return None
        index = parts.index(flag)
        return parts[index + 1] if index + 1 < len(parts) else None

    row = _int_or_none(flag_value("--row"))
    col = _int_or_none(flag_value("--col"))
    run_dir = flag_value("--run-dir")
    out: dict[str, Any] = {"operation": operation}
    if row is not None:
        out["row"] = row
    if col is not None:
        out["col"] = col
    if run_dir:
        try:
            out["runDir"] = str(Path(run_dir).resolve().relative_to(ROOT))
        except ValueError:
            out["runDir"] = run_dir
    return out


def _run_choices(runs_dir: Path) -> list[dict[str, Any]]:
    choices = []
    for manifest in sorted(runs_dir.glob("*/manifest.csv"), key=lambda path: _run_updated_at(path.parent), reverse=True):
        rows = _read_manifest(manifest)
        choices.append(
            {
                "id": manifest.parent.name,
                "path": str(manifest.parent.relative_to(ROOT)),
                "rows": len(rows),
                "updated": _run_updated_at(manifest.parent),
            }
        )
    return choices


def _cell_key(cell: dict[str, int]) -> str:
    return f"{cell['row']}_{cell['col']}"


def _is_read_row(row: dict[str, Any]) -> bool:
    return row.get("operation") == "read" or str(row.get("stage", "")).startswith("read")


def _array_resume_info(run_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    is_array_run = "read-array" in run_dir.name or any(str(row.get("stage", "")).startswith("array_") for row in rows)
    col_rows: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        cell = row.get("cellAddress")
        if not _is_read_row(row) or not isinstance(cell, dict) or not isinstance(cell.get("col"), int):
            continue
        col_rows.setdefault(cell["col"], []).append(row)
    complete_cols = {
        col
        for col, items in col_rows.items()
        if len(items) >= GRID_SIZE
        and not any(row.get("error") and _saleae_error_needs_restart(str(row.get("error", ""))) for row in items)
    }
    incomplete_cols = [col for col in range(GRID_SIZE) if col not in complete_cols]
    col_start = incomplete_cols[0] if incomplete_cols else None
    return {
        "isArrayRun": is_array_run,
        "canResume": is_array_run and col_start is not None and bool(col_rows),
        "colStart": col_start,
        "completedColumns": len(complete_cols),
        "incompleteColumns": incomplete_cols,
    }


def _summarize(run_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    log_events = _recent_log_events(run_dir)
    progress_events = _read_progress_events(run_dir)
    active_error = _active_error(run_dir, rows, log_events)
    thresholds = _read_thresholds(run_dir)
    latest_read_by_cell: dict[str, dict[str, Any]] = {}
    reads: list[dict[str, Any]] = []
    programs: list[dict[str, Any]] = []
    for row in rows:
        cell = row.get("cellAddress")
        if not cell:
            continue
        if _is_read_row(row):
            reads.append(row)
            latest_read_by_cell[_cell_key(cell)] = row
        else:
            programs.append(row)

    last = rows[-1] if rows else None
    last_cell = last.get("cellAddress") if last else None
    last_read = reads[-1] if reads else None
    last_read_cell = last_read.get("cellAddress") if last_read else None
    last_current = last_read.get("current_uA") if last_read else None
    previous_current = None
    if last_read_cell:
        same_cell = [
            item
            for item in reads[:-1]
            if item.get("cellAddress") == last_read_cell and item.get("current_uA") is not None
        ]
        if same_cell:
            previous_current = same_cell[-1].get("current_uA")
    delta = None
    trend = "flat"
    if isinstance(last_current, (int, float)) and isinstance(previous_current, (int, float)):
        delta = last_current - previous_current
        if delta > 0:
            trend = "rising"
        elif delta < 0:
            trend = "falling"

    values = [row["current_uA"] for row in latest_read_by_cell.values() if row.get("current_uA") is not None]
    min_current = min(values) if values else None
    max_current = max(values) if values else None

    return {
        "run": {
            "id": run_dir.name,
            "path": str(run_dir.relative_to(ROOT)),
            "updated": _run_updated_at(run_dir),
        },
        "last": last,
        "lastCell": last_cell,
        "lastRead": last_read,
        "lastReadCell": last_read_cell,
        "lastCurrent_uA": last_current,
        "previousCurrent_uA": previous_current,
        "currentDelta_uA": delta,
        "trend": trend,
        "counts": {
            "rows": len(rows),
            "read": len(reads),
            "program": len(programs),
            "ok": sum(1 for row in rows if row.get("ok")),
        },
        "scale": {"min_uA": min_current, "max_uA": max_current},
        "thresholds_uA": thresholds,
        "cells": list(latest_read_by_cell.values()),
        "history": rows[-160:],
        "readHistory": reads[-160:],
        "logEvents": log_events,
        "progressEvents": progress_events,
        "activeError": active_error,
        "arrayResume": _array_resume_info(run_dir, rows),
    }


class GuiHandler(SimpleHTTPRequestHandler):
    config: ViewerConfig
    running_commands: dict[str, dict[str, Any]] = {}

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/runs":
            self._send_json({"runs": _run_choices(self.config.runs_dir)})
            return
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            run_name = query.get("run", [""])[0]
            run_dir = self.config.runs_dir / run_name if run_name else _latest_run(self.config.runs_dir)
            if run_dir is None:
                self._send_json({"runs": [], "state": None})
                return
            rows = _read_manifest(_manifest_for_run(run_dir))
            self._send_json(
                {
                    "runs": _run_choices(self.config.runs_dir),
                    "state": _summarize(run_dir, rows),
                    "commandsEnabled": self.config.allow_commands,
                    "runningCommands": self._command_state(),
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed_path = urlparse(self.path).path
        if parsed_path == "/api/command/kill":
            self._kill_command()
            return
        if parsed_path != "/api/command":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.config.allow_commands:
            self._send_json({"error": "Commands are disabled. Start with --allow-commands to enable hardware actions."}, HTTPStatus.FORBIDDEN)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        operation = str(payload.get("operation", "read"))
        array_mode = "burst-columns"
        row = int(payload.get("row", 0))
        col = int(payload.get("col", 0))
        resume_run = str(payload.get("resumeRun", "")).strip()
        resume_col = _int_or_none(payload.get("resumeCol"))
        zynq_password = str(payload.get("zynqPassword", ""))
        dry_run = bool(payload.get("dryRun", False))
        confirmed = bool(payload.get("confirmHardware", False))
        if (
            operation not in {"read", "set", "reset", "cycle", "read-array"}
            or not (0 <= row < GRID_SIZE and 0 <= col < GRID_SIZE)
        ):
            self._send_json({"error": "Invalid operation or cell."}, HTTPStatus.BAD_REQUEST)
            return
        if not dry_run and not confirmed:
            self._send_json({"error": "Hardware command needs explicit confirmation."}, HTTPStatus.BAD_REQUEST)
            return
        if any(state["proc"].poll() is None for state in self.running_commands.values()):
            self._send_json({"error": "A GUI command is already processing."}, HTTPStatus.CONFLICT)
            return
        resume_info: dict[str, Any] | None = None
        if resume_run:
            if operation != "read-array":
                self._send_json({"error": "Only read-array can resume a previous run."}, HTTPStatus.BAD_REQUEST)
                return
            run_dir = (self.config.runs_dir / resume_run).resolve()
            try:
                run_dir.relative_to(self.config.runs_dir)
            except ValueError:
                self._send_json({"error": "Invalid resume run."}, HTTPStatus.BAD_REQUEST)
                return
            rows = _read_manifest(_manifest_for_run(run_dir))
            resume_info = _array_resume_info(run_dir, rows)
            if not resume_info["canResume"]:
                self._send_json({"error": "Selected run has no missing array columns to resume."}, HTTPStatus.BAD_REQUEST)
                return
            if resume_col is None:
                resume_col = int(resume_info["colStart"])
            if not 0 <= resume_col < GRID_SIZE:
                self._send_json({"error": "Resume column must be 0..31."}, HTTPStatus.BAD_REQUEST)
                return
        else:
            run_label = "array" if operation == "read-array" else f"r{row:02d}c{col:02d}"
            run_dir = ROOT / "api_v1" / "runs" / f"gui_{time.strftime('%Y%m%d_%H%M%S')}_{run_label}_{operation}"
            run_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(ROOT / "api_v1" / "scan_debug_cli.py"), operation, "--run-dir", str(run_dir)]
        if operation != "read-array":
            cmd.extend(["--row", str(row), "--col", str(col)])
        else:
            cmd.extend(["--array-mode", array_mode])
            if resume_info:
                cmd.extend(["--col-start", str(resume_col)])
        safe_cmd = list(cmd)
        if zynq_password:
            cmd.extend(["--zynq-password", zynq_password])
            safe_cmd.extend(["--zynq-password", "<redacted>"])
        if dry_run:
            cmd.append("--dry-run")
            safe_cmd.append("--dry-run")
        log_path = run_dir / ("gui_command.log" if not resume_info else f"gui_command_resume_{time.strftime('%Y%m%d_%H%M%S')}.log")
        log_handle = log_path.open("w")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), text=True, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True)
        key = f"{int(time.time() * 1000)}"
        self.running_commands[key] = {
            "proc": proc,
            "operation": operation,
            "arrayMode": array_mode if operation == "read-array" else "",
            "resume": bool(resume_info),
            "row": row,
            "col": col,
            "runDir": str(run_dir.relative_to(ROOT)),
            "logPath": str(log_path.relative_to(ROOT)),
            "started": time.time(),
            "logHandle": log_handle,
        }
        self._send_json(
            {
                "id": key,
                "runDir": str(run_dir.relative_to(ROOT)),
                "command": safe_cmd,
                "resume": resume_info,
            }
        )

    def _command_state(self) -> list[dict[str, Any]]:
        states = []
        tracked_pids = set()
        for key, item in list(self.running_commands.items()):
            proc = item["proc"]
            return_code = proc.poll()
            tracked_pids.add(proc.pid)
            states.append(
                {
                    "id": key,
                    "pid": proc.pid,
                    "running": return_code is None,
                    "canKill": return_code is None,
                    "external": False,
                    "returnCode": return_code,
                    "operation": item.get("operation"),
                    "arrayMode": item.get("arrayMode", ""),
                    "row": item.get("row"),
                    "col": item.get("col"),
                    "runDir": item.get("runDir"),
                    "started": item.get("started"),
                }
            )
            if return_code is not None:
                log_handle = item.get("logHandle")
                if log_handle:
                    log_handle.close()
                self.running_commands.pop(key, None)
        states.extend(self._external_command_state(tracked_pids))
        return states

    def _external_command_state(self, tracked_pids: set[int]) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(["ps", "-axo", "pid,command"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return []
        states: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if "scan_debug_cli.py" not in line:
                continue
            pid_text, _, command = line.partition(" ")
            pid = _int_or_none(pid_text)
            if pid is None or pid in tracked_pids or pid == os.getpid():
                continue
            parsed = _parse_scan_debug_process(command)
            states.append(
                {
                    "id": f"pid-{pid}",
                    "pid": pid,
                    "running": True,
                    "canKill": True,
                    "external": True,
                    "returnCode": None,
                    **parsed,
                }
            )
        return states

    def _kill_command(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        command_id = str(payload.get("id", ""))
        item = self.running_commands.get(command_id)
        if item:
            proc = item["proc"]
            if proc.poll() is not None:
                self._send_json({"error": "Command already finished."}, HTTPStatus.CONFLICT)
                return
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            item["killed"] = time.time()
            self._send_json({"ok": True, "id": command_id, "message": "Kill signal sent."})
            return
        if command_id.startswith("pid-"):
            pid = _int_or_none(command_id.removeprefix("pid-"))
            if pid is not None and self._is_external_scan_debug_pid(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                    return
                self._send_json({"ok": True, "id": command_id, "message": f"Kill signal sent to PID {pid}."})
                return
        self._send_json({"error": "No matching GUI/API command is running."}, HTTPStatus.NOT_FOUND)

    def _is_external_scan_debug_pid(self, pid: int) -> bool:
        try:
            proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        command = proc.stdout.strip()
        if "scan_debug_cli.py" not in command:
            return False
        parsed = _parse_scan_debug_process(command)
        run_dir = str(parsed.get("runDir", ""))
        return not run_dir or run_dir.startswith("api_v1/runs/")

    def _send_json(self, item: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(item, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local GUI for scan-debug cell API runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--allow-commands", action="store_true", help="enable GUI buttons that start scan_debug_cli.py child processes")
    args = parser.parse_args()

    GuiHandler.config = ViewerConfig(runs_dir=args.runs_dir.resolve(), allow_commands=args.allow_commands)
    os.chdir(STATIC_DIR)
    server = ThreadingHTTPServer((args.host, args.port), GuiHandler)
    print(f"Scan-debug GUI listening on http://{args.host}:{args.port}")
    print("View-only mode" if not args.allow_commands else "Command buttons enabled")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
