#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
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
                rows.append(row)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    rows.sort(key=lambda item: item.get("index") if item.get("index") is not None else -1)
    return rows


def _manifest_for_run(run_dir: Path) -> Path:
    return run_dir / "manifest.csv"


def _latest_run(runs_dir: Path) -> Path | None:
    manifests = sorted(runs_dir.glob("*/manifest.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    for manifest in manifests:
        if _read_manifest(manifest):
            return manifest.parent
    return manifests[0].parent if manifests else None


def _run_choices(runs_dir: Path) -> list[dict[str, Any]]:
    choices = []
    for manifest in sorted(runs_dir.glob("*/manifest.csv"), key=lambda path: path.stat().st_mtime, reverse=True):
        rows = _read_manifest(manifest)
        choices.append(
            {
                "id": manifest.parent.name,
                "path": str(manifest.parent.relative_to(ROOT)),
                "rows": len(rows),
                "updated": manifest.stat().st_mtime,
            }
        )
    return choices


def _cell_key(cell: dict[str, int]) -> str:
    return f"{cell['row']}_{cell['col']}"


def _is_read_row(row: dict[str, Any]) -> bool:
    return row.get("operation") == "read" or str(row.get("stage", "")).startswith("read")


def _summarize(run_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
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
            "updated": _manifest_for_run(run_dir).stat().st_mtime if _manifest_for_run(run_dir).exists() else None,
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
        "cells": list(latest_read_by_cell.values()),
        "history": rows[-160:],
        "readHistory": reads[-160:],
    }


class GuiHandler(SimpleHTTPRequestHandler):
    config: ViewerConfig
    running_commands: dict[str, subprocess.Popen[str]] = {}

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
        if urlparse(self.path).path != "/api/command":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.config.allow_commands:
            self._send_json({"error": "Commands are disabled. Start with --allow-commands to enable hardware actions."}, HTTPStatus.FORBIDDEN)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        operation = str(payload.get("operation", "read"))
        row = int(payload.get("row", 0))
        col = int(payload.get("col", 0))
        dry_run = bool(payload.get("dryRun", False))
        if operation not in {"read", "set", "reset", "cycle"} or not (0 <= row < GRID_SIZE and 0 <= col < GRID_SIZE):
            self._send_json({"error": "Invalid operation or cell."}, HTTPStatus.BAD_REQUEST)
            return
        run_dir = ROOT / "api_v1" / "runs" / f"gui_{time.strftime('%Y%m%d_%H%M%S')}_r{row:02d}c{col:02d}_{operation}"
        cmd = [sys.executable, str(ROOT / "api_v1" / "scan_debug_cli.py"), operation, "--row", str(row), "--col", str(col), "--run-dir", str(run_dir)]
        if dry_run:
            cmd.append("--dry-run")
        proc = subprocess.Popen(cmd, cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        key = f"{int(time.time() * 1000)}"
        self.running_commands[key] = proc
        self._send_json({"id": key, "runDir": str(run_dir.relative_to(ROOT)), "command": cmd})

    def _command_state(self) -> list[dict[str, Any]]:
        states = []
        for key, proc in list(self.running_commands.items()):
            return_code = proc.poll()
            states.append({"id": key, "pid": proc.pid, "running": return_code is None, "returnCode": return_code})
            if return_code is not None:
                self.running_commands.pop(key, None)
        return states

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
