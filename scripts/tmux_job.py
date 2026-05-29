#!/usr/bin/env python3
"""Run a command file under a pseudo-terminal and record tmux-skills status."""

from __future__ import annotations

import argparse
import errno
import os
import pty
import select
import subprocess
import sys
from pathlib import Path

import tmux_state


def read_command_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def run_command_file(command_file: Path, cwd: Path, log_file: Path) -> tuple[int, str, str]:
    master_fd, slave_fd = pty.openpty()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    tail = bytearray()
    status = "failed"
    proc: subprocess.Popen[bytes] | None = None

    try:
        with log_file.open("ab") as log_handle:
            proc = subprocess.Popen(
                ["/bin/sh", str(command_file)],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            while True:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if chunk:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                        log_handle.write(chunk)
                        log_handle.flush()
                        tail.extend(chunk)
                        if len(tail) > 32768:
                            del tail[: len(tail) - 32768]
                    elif proc.poll() is not None:
                        break
                elif proc.poll() is not None:
                    break
    except KeyboardInterrupt:
        status = "stopped"
        if proc and proc.poll() is None:
            proc.terminate()
        return 130, status, tail.decode("utf-8", errors="replace")
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)

    return_code = proc.wait() if proc else 1
    if return_code == 0:
        status = "succeeded"
    elif return_code < 0:
        status = "stopped"
        return_code = 128 + abs(return_code)
    return return_code, status, tail.decode("utf-8", errors="replace")


def exec_job(args: argparse.Namespace) -> int:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)

    command_file = Path(args.command_file).expanduser().resolve()
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths["workspace"]
    status_file = tmux_state.status_path(paths, args.job_id)
    log_file = tmux_state.log_path(paths, args.job_id)
    command_text = read_command_text(command_file)

    running = tmux_state.build_status(
        kind="job",
        item_id=args.job_id,
        attempt=args.attempt,
        name=args.name,
        status="running",
        pane_id=args.pane,
        command_preview_text=tmux_state.command_preview(command_text),
        cwd=str(cwd),
        status_file=status_file,
        log_file=log_file,
    )
    tmux_state.write_status(status_file, running)

    exit_code, terminal_status, last_output = run_command_file(command_file, cwd, log_file)
    finished = dict(running)
    finished.update(
        {
            "status": terminal_status,
            "exit_code": exit_code,
            "last_output": tmux_state.tail_text(last_output),
        }
    )
    tmux_state.write_status(status_file, finished)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="tmux-skills long-running job wrapper")
    subparsers = parser.add_subparsers(dest="action", required=True)

    exec_parser = subparsers.add_parser("exec", help="Execute a recorded command file")
    exec_parser.add_argument("--job-id", required=True)
    exec_parser.add_argument("--attempt", type=int, required=True)
    exec_parser.add_argument("--name")
    exec_parser.add_argument("--pane")
    exec_parser.add_argument("--command-file", required=True)
    exec_parser.add_argument("--cwd")
    exec_parser.add_argument("--workspace")
    exec_parser.add_argument("--state-dir")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.action == "exec":
        raise SystemExit(exec_job(args))
    parser.error(f"unknown command: {args.action}")


if __name__ == "__main__":
    main()
