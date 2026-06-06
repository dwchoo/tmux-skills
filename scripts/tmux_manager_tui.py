#!/usr/bin/env python3
"""Textual manager TUI for tmux-skills.

This viewer is optional and launched only from a uv-managed venv.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def terminal_jobs(record: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    active = set(str(value) for value in (record.get("active_job_ids") or []))
    rows: list[dict[str, Any]] = []
    for job_id in record.get("job_ids") or []:
        job = jobs.get(str(job_id)) if isinstance(jobs.get(str(job_id)), dict) else {}
        rows.append(
            {
                "job_id": str(job_id),
                "pane": job.get("pane_id") or "-",
                "status": job.get("status") or ("running" if str(job_id) in active else "unknown"),
                "event": job.get("terminal_event_id") or "-",
            }
        )
    return rows


def events(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("events") if isinstance(record.get("events"), dict) else {}
    return [event for event in raw.values() if isinstance(event, dict)]


def run_textual(args: argparse.Namespace) -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.widgets import DataTable, Footer, Header, Static
    except Exception as exc:
        print(f"Textual viewer unavailable: {exc}", flush=True)
        return 2

    manager_file = Path(args.manager_file)
    state_file = Path(args.state_file)

    class ManagerApp(App[None]):
        CSS = """
        Screen { layout: vertical; }
        #summary { height: 3; padding: 0 1; }
        #tables { height: 1fr; }
        DataTable { width: 1fr; }
        """
        BINDINGS = [
            ("r", "refresh", "Refresh"),
            ("q", "quit", "Quit"),
        ]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="summary")
            with Horizontal(id="tables"):
                with Vertical():
                    yield Static("Jobs")
                    yield DataTable(id="jobs")
                with Vertical():
                    yield Static("Events")
                    yield DataTable(id="events")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#jobs", DataTable).add_columns("job", "pane", "status", "event")
            self.query_one("#events", DataTable).add_columns("event", "job", "status", "notify", "ack")
            self.set_interval(args.poll_seconds, self.refresh_tables)
            self.refresh_tables()

        def action_refresh(self) -> None:
            self.refresh_tables()

        def refresh_tables(self) -> None:
            record = read_json(manager_file)
            write_json(
                state_file,
                {
                    "pid": __import__("os").getpid(),
                    "pane_id": args.pane_id,
                    "manager_id": args.manager_id,
                    "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "backend": "textual",
                },
            )
            summary = self.query_one("#summary", Static)
            summary.update(
                f"manager {record.get('manager_id') or args.manager_id}  "
                f"status={record.get('status') or '-'}  "
                f"jobs={len(record.get('job_ids') or [])}  events={len(events(record))}"
            )
            jobs_table = self.query_one("#jobs", DataTable)
            jobs_table.clear()
            for row in terminal_jobs(record):
                jobs_table.add_row(row["job_id"], row["pane"], row["status"], row["event"])
            events_table = self.query_one("#events", DataTable)
            events_table.clear()
            for event in events(record)[-50:]:
                events_table.add_row(
                    str(event.get("event_id") or "-"),
                    str(event.get("job_id") or "-"),
                    str(event.get("status") or "-"),
                    str(event.get("notification_status") or "-"),
                    "yes" if event.get("acknowledged_by_codex") else "no",
                )

    ManagerApp().run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Textual tmux-skills manager TUI")
    parser.add_argument("--manager-id", required=True)
    parser.add_argument("--manager-file", required=True)
    parser.add_argument("--dashboard-file")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main() -> int:
    return run_textual(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
