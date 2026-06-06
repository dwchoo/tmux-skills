#!/usr/bin/env python3
"""Curses viewer for tmux-skills manager dashboard snapshots."""

from __future__ import annotations

import argparse
import curses
import os
import time
from pathlib import Path
from typing import Any

import tmux_manager
import tmux_state


MODE_KEYS = {
    ord("1"): "summary",
    ord("2"): "jobs",
    ord("3"): "events",
}


def pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def parse_pid(value: Any) -> int | None:
    try:
        pid = int(str(value))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def read_manager_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    data, error = tmux_state.read_json(path)
    if error or data is None:
        return None, error
    try:
        return tmux_manager.normalize_manager_record(data), None
    except Exception as exc:
        return None, str(exc)


def read_mode(state_file: Path) -> str:
    state, _error = tmux_state.read_json(state_file)
    if isinstance(state, dict) and state.get("mode") in tmux_manager.DASHBOARD_MODES:
        return str(state["mode"])
    return "summary"


def write_viewer_state(args: argparse.Namespace, mode: str, record: dict[str, Any] | None) -> None:
    state = {
        "manager_id": args.manager_id,
        "pid": os.getpid(),
        "pane_id": args.pane_id,
        "mode": mode,
        "heartbeat_at": tmux_state.utc_now(),
        "dashboard_file": str(args.dashboard_file),
        "manager_file": str(args.manager_file),
        "manager_pid": record.get("manager_pid") if isinstance(record, dict) else None,
        "updated_at": tmux_state.utc_now(),
    }
    tmux_state.atomic_write_json(Path(args.state_file), state)


def delete_terminal_jobs(record: dict[str, Any] | None, args: argparse.Namespace) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    return tmux_manager.delete_terminal_jobs(
        str(args.manager_id),
        workspace=tmux_state.one_line_text(record.get("workspace")),
        state_dir=tmux_state.one_line_text(record.get("state_dir")),
    )


def should_exit(record: dict[str, Any] | None) -> bool:
    if record is None:
        return True
    if record.get("status") == "cancelled":
        return True
    manager_pid = parse_pid(record.get("manager_pid"))
    return bool(manager_pid and not pid_is_running(manager_pid))


def next_mode(mode: str) -> str:
    modes = list(tmux_manager.DASHBOARD_MODES)
    index = modes.index(mode) if mode in modes else 0
    return modes[(index + 1) % len(modes)]


def draw(stdscr: Any, text: str) -> None:
    rows, cols = stdscr.getmaxyx()
    stdscr.erase()
    for row, line in enumerate(text.splitlines()[:rows]):
        try:
            stdscr.addnstr(row, 0, line, max(0, cols - 1))
        except curses.error:
            pass
    stdscr.refresh()


def run_viewer(stdscr: Any, args: argparse.Namespace) -> int:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(False)
    stdscr.timeout(200)

    mode = read_mode(Path(args.state_file))
    last_render: tuple[str, tuple[int, int], str] | None = None
    last_record: dict[str, Any] | None = None
    next_poll = 0.0

    while True:
        key = stdscr.getch()
        if key in {ord("q"), ord("Q")}:
            return 0
        if key == 9:
            mode = next_mode(mode)
            next_poll = 0.0
        elif key in {ord("d"), ord("D"), curses.KEY_DC}:
            delete_terminal_jobs(last_record, args)
            next_poll = 0.0
            last_render = None
        elif key in MODE_KEYS:
            mode = MODE_KEYS[key]
            next_poll = 0.0

        now = time.monotonic()
        rows, cols = stdscr.getmaxyx()
        if now >= next_poll:
            record, error = read_manager_record(Path(args.manager_file))
            if should_exit(record):
                return 0
            if error:
                text = tmux_manager.clip_dashboard_lines([f"manager state read error: {error}"], cols, rows)
                rendered = "\n".join(text)
            else:
                last_record = record
                write_viewer_state(args, mode, record)
                rendered = tmux_manager.dashboard_text(record or {}, mode=mode, width=cols, height=rows)
            render_key = (mode, (rows, cols), rendered)
            if render_key != last_render:
                draw(stdscr, rendered)
                last_render = render_key
            next_poll = now + max(0.2, float(args.poll_seconds))
        elif last_record is not None:
            rendered = tmux_manager.dashboard_text(last_record, mode=mode, width=cols, height=rows)
            render_key = (mode, (rows, cols), rendered)
            if render_key != last_render:
                draw(stdscr, rendered)
                last_render = render_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a tmux-skills manager dashboard")
    parser.add_argument("--manager-id", required=True)
    parser.add_argument("--manager-file", type=Path, required=True)
    parser.add_argument("--dashboard-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(curses.wrapper(run_viewer, args))


if __name__ == "__main__":
    main()
