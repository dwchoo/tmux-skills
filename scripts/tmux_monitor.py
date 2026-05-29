#!/usr/bin/env python3
"""Single-trigger tmux pane monitor for tmux-skills."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import tmux_state


ANSI_RE = re.compile(
    r"(?:\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"
    r"|(?:\x1B[P^_].*?\x1B\\)"
    r"|(?:\x1B\[[0-?]*[ -/]*[@-~])"
    r"|(?:\x1B[@-Z\\-_])",
    re.DOTALL,
)
PROMPT_RE = re.compile("(?:[$#%]|\\u276f|\\u276e|\\u279c|\\u03bb)\\s*$")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def prompt_like(output: str) -> bool:
    for line in reversed(strip_ansi(output).splitlines()):
        stripped = line.strip()
        if stripped:
            return bool(PROMPT_RE.search(stripped))
    return False


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


def run_monitor(args: argparse.Namespace) -> int:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    status_file = tmux_state.status_path(paths, args.monitor_id)
    log_file = tmux_state.log_path(paths, args.monitor_id)
    started = time.monotonic()
    regex = re.compile(args.match_regex) if args.match_regex else None

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

    try:
        while True:
            output = capture_pane(args.pane, args.lines)
            stripped = strip_ansi(output)
            log_file.write_text(stripped, encoding="utf-8")

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
    except Exception as exc:
        terminal = "failed"
        stripped = str(exc)

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
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--lines", type=int, default=200)
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
