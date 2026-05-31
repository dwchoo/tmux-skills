#!/usr/bin/env python3
"""Single-trigger tmux pane monitor for tmux-skills."""

from __future__ import annotations

import argparse
import math
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import tmux_state
from tmux_text import prompt_like, strip_ansi


class MonitorTerminatedBySignal(BaseException):
    """Raised from SIGTERM so the monitor can write a terminal status."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


def _raise_monitor_terminated(signum: int, _frame: object) -> None:
    raise MonitorTerminatedBySignal(signum)


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def capture_pane(pane: str, lines: int) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane, "-S", f"-{lines}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux capture-pane failed")
    return result.stdout.rstrip("\n")


def validate_monitor_args(args: argparse.Namespace) -> None:
    if not tmux_state.one_line_text(getattr(args, "monitor_id", None)):
        raise ValueError("monitor requires nonblank --monitor-id")
    if not tmux_state.one_line_text(getattr(args, "pane", None)):
        raise ValueError("monitor requires nonblank --pane")


def run_monitor(args: argparse.Namespace) -> int:
    try:
        validate_monitor_args(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    status_file = tmux_state.status_path(paths, args.monitor_id)
    log_file = tmux_state.log_path(paths, args.monitor_id)
    started = time.monotonic()

    running = tmux_state.build_status(
        kind="monitor",
        item_id=args.monitor_id,
        attempt=1,
        name=args.name,
        status="running",
        pane_id=args.pane,
        command_preview_text=args.match_regex or ("idle-shell" if args.idle_shell else "monitor"),
        cwd=str(paths["workspace"]),
        status_file=status_file,
        log_file=log_file,
    )
    tmux_state.write_status(status_file, running)

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_monitor_terminated)
    try:
        try:
            regex = re.compile(args.match_regex) if args.match_regex else None
            while True:
                output = capture_pane(args.pane, args.lines)
                stripped = strip_ansi(output)
                try:
                    log_file.write_text(stripped, encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(f"could not write monitor log file {log_file}: {exc}") from exc

                if regex and regex.search(stripped):
                    terminal = "matched"
                    break
                if args.idle_shell and prompt_like(stripped):
                    terminal = "matched"
                    break
                if args.timeout_seconds is not None and time.monotonic() - started >= args.timeout_seconds:
                    terminal = "timeout"
                    break
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            terminal = "stopped"
            stripped = ""
        except MonitorTerminatedBySignal:
            terminal = "stopped"
            stripped = ""
        except Exception as exc:
            terminal = "failed"
            stripped = str(exc)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    finished = dict(running)
    finished.update({"status": terminal, "last_output": tmux_state.tail_text(stripped), "exit_code": 0 if terminal == "matched" else 1})
    tmux_state.write_status(status_file, finished)
    return 0 if terminal == "matched" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor a tmux pane until one condition triggers")
    parser.add_argument("--monitor-id", required=True)
    parser.add_argument("--name")
    parser.add_argument("--pane", required=True)
    parser.add_argument("--match-regex")
    parser.add_argument("--idle-shell", action="store_true")
    parser.add_argument("--timeout-seconds", type=positive_float)
    parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    parser.add_argument("--lines", type=positive_int, default=200)
    parser.add_argument("--workspace")
    parser.add_argument("--state-dir")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.match_regex and not args.idle_shell and args.timeout_seconds is None:
        print("monitor requires --match-regex, --idle-shell, or --timeout-seconds", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run_monitor(args))


if __name__ == "__main__":
    main()
