#!/usr/bin/env python3
"""Run a command file under a pseudo-terminal and record tmux-skills status."""

from __future__ import annotations

import argparse
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import tmux_state


class JobTerminatedBySignal(BaseException):
    """Raised from SIGTERM so the wrapper can stop its child process group."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


def _raise_job_terminated(signum: int, _frame: object) -> None:
    raise JobTerminatedBySignal(signum)


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_process_group(proc: subprocess.Popen[bytes], *, grace: float = 2.0) -> None:
    if proc.poll() is not None:
        return

    pgid: int | None = None
    try:
        pgid = os.getpgid(proc.pid)
    except (AttributeError, OSError, ProcessLookupError):
        pass

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError, PermissionError):
            pass
    else:
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass

    deadline = time.monotonic() + grace
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ProcessLookupError):
        pass

    if pgid is not None:
        while time.monotonic() < deadline:
            if not _process_group_exists(pgid):
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if _process_group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError, PermissionError):
                pass
    elif proc.poll() is None:
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    try:
        proc.wait()
    except (OSError, ProcessLookupError):
        pass


def read_command_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def require_nonblank(value: object, message: str) -> None:
    if not tmux_state.one_line_text(value):
        raise ValueError(message)


def run_command_file(command_file: Path, cwd: Path, log_file: Path) -> tuple[int, str, str]:
    master_fd, slave_fd = pty.openpty()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    tail = bytearray()
    status = "failed"
    proc: subprocess.Popen[bytes] | None = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_job_terminated)

    try:
        try:
            log_handle_context = log_file.open("ab")
        except OSError as exc:
            raise RuntimeError(f"could not open job log file {log_file}: {exc}") from exc

        with log_handle_context as log_handle:
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
            _terminate_process_group(proc)
        return 130, status, tail.decode("utf-8", errors="replace")
    except JobTerminatedBySignal as exc:
        status = "stopped"
        if proc and proc.poll() is None:
            _terminate_process_group(proc)
        return 128 + exc.signum, status, tail.decode("utf-8", errors="replace")
    except Exception:
        if proc and proc.poll() is None:
            _terminate_process_group(proc)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
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
    try:
        require_nonblank(args.job_id, "job wrapper requires nonblank --job-id")
        require_nonblank(args.command_file, "job wrapper requires nonblank --command-file")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)

    command_file = Path(args.command_file).expanduser().resolve()
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths["workspace"]
    status_file = tmux_state.status_path(paths, args.job_id)
    log_file = tmux_state.log_path(paths, args.job_id)
    try:
        command_text = read_command_text(command_file)
    except Exception as exc:
        failed = tmux_state.build_status(
            kind="job",
            item_id=args.job_id,
            attempt=args.attempt,
            name=args.name,
            status="failed",
            pane_id=args.pane,
            command_preview_text=str(command_file),
            cwd=str(cwd),
            status_file=status_file,
            log_file=log_file,
            exit_code=1,
            last_output=f"could not read command file: {exc}",
        )
        tmux_state.write_status(status_file, failed)
        return 1
    if not tmux_state.one_line_text(command_text):
        failed = tmux_state.build_status(
            kind="job",
            item_id=args.job_id,
            attempt=args.attempt,
            name=args.name,
            status="failed",
            pane_id=args.pane,
            command_preview_text=str(command_file),
            cwd=str(cwd),
            status_file=status_file,
            log_file=log_file,
            exit_code=1,
            last_output="command is blank",
        )
        tmux_state.write_status(status_file, failed)
        return 1

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

    try:
        exit_code, terminal_status, last_output = run_command_file(command_file, cwd, log_file)
    except Exception as exc:
        exit_code = 1
        terminal_status = "failed"
        last_output = f"job wrapper failed before command completed: {exc}"
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
    exec_parser.add_argument("--attempt", type=positive_int, required=True)
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
