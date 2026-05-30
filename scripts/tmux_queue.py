#!/usr/bin/env python3
"""Managed watch and queue workers for tmux-skills."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import tmux_control
import tmux_state


STOP_REQUESTED = False


def request_stop(signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def command_hash(command_text: str | None) -> str | None:
    if command_text is None:
        return None
    return hashlib.sha256(command_text.encode("utf-8")).hexdigest()[:16]


def read_command(args: argparse.Namespace) -> str:
    if args.command_file:
        return Path(args.command_file).expanduser().read_text(encoding="utf-8")
    return args.command_text or ""


def resolve_status_file(paths: dict[str, Path], status_file: str | None) -> Path | None:
    if not status_file:
        return None
    path = Path(status_file).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (paths["workspace"] / path).resolve()


def write_worker_record(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    kind: str,
    status: str,
    command_text: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = tmux_state.utc_now()
    job_id = tmux_state.safe_id(args.job_id)
    record_path = tmux_state.job_path(paths, job_id)
    previous, _error = tmux_state.read_json(record_path)
    created_at = previous.get("created_at") if previous else now
    check_interval = getattr(args, "interval", None) or getattr(args, "poll_seconds", None)
    record: dict[str, Any] = {
        "version": 1,
        "job_id": job_id,
        "kind": kind,
        "status": status,
        "pid": os.getpid(),
        "pane_id": getattr(args, "pane", None),
        "command_hash": command_hash(command_text),
        "command_path": getattr(args, "command_file", None),
        "status_path": str(tmux_state.status_path(paths, job_id)),
        "log_path": str(tmux_state.log_path(paths, job_id)),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "created_at": created_at,
        "updated_at": now,
        "heartbeat_at": now,
    }
    for key in ("dedupe_key", "dedupe_payload", "owner", "duplicate_allowed", "duplicate_of", "argv"):
        if previous and key in previous:
            record[key] = previous[key]
    if previous and "check_interval_seconds" in previous:
        record["check_interval_seconds"] = previous["check_interval_seconds"]
    elif check_interval is not None:
        record["check_interval_seconds"] = float(check_interval)
    if extra:
        record.update(extra)
    tmux_state.atomic_write_json(record_path, record)
    return record


def write_worker_status(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    kind: str,
    status: str,
    started_at: str,
    command_text: str | None = None,
    last_output: str = "",
    exit_code: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_id = tmux_state.safe_id(args.job_id)
    status_file = tmux_state.status_path(paths, job_id)
    log_file = tmux_state.log_path(paths, job_id)
    data = tmux_state.build_status(
        kind=kind,
        item_id=job_id,
        attempt=1,
        name=getattr(args, "name", None),
        status=status,
        pane_id=getattr(args, "pane", None),
        command_preview_text=tmux_state.command_preview(command_text) if command_text else kind,
        cwd=str(paths["workspace"]),
        status_file=status_file,
        log_file=log_file,
        started_at=started_at,
        exit_code=exit_code,
        last_output=last_output,
    )
    data["heartbeat_at"] = tmux_state.utc_now()
    if extra:
        data.update(extra)
    tmux_state.write_status(status_file, data)
    return data


def sleep_interruptibly(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not STOP_REQUESTED and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def status_rows(status_file: Path) -> list[str]:
    if not status_file.exists():
        return []
    return [line.rstrip("\n") for line in status_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def row_matches_spec(line: str, spec: str) -> bool:
    if ":" not in spec:
        return spec in line
    key, expected = spec.rsplit(":", 1)
    fields = [field.strip() for field in line.split("\t")]
    return (key in fields and expected in fields) or (key in line and expected in line)


def parse_assignment_spec(spec: str) -> dict[str, str] | None:
    if "=" not in spec:
        return None
    pairs: dict[str, str] = {}
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part or "=" not in part:
            return None
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            return None
        pairs[key] = value.strip()
    return pairs or None


def header_rows_match_spec(rows: list[str], spec: str) -> bool:
    assignments = parse_assignment_spec(spec)
    if not assignments or len(rows) < 2:
        return False
    headers = [field.strip() for field in rows[0].split("\t")]
    if not headers:
        return False
    for line in rows[1:]:
        values = [field.strip() for field in line.split("\t")]
        row = dict(zip(headers, values))
        if all(row.get(key) == value for key, value in assignments.items()):
            return True
    return False


def specs_matching(rows: list[str], specs: list[str]) -> list[str]:
    matches: list[str] = []
    for spec in specs:
        if header_rows_match_spec(rows, spec) or any(row_matches_spec(row, spec) for row in rows):
            matches.append(spec)
    return matches


def fail_worker(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    started_at: str,
    kind: str,
    command_text: str | None,
    last_output: str,
    extra: dict[str, Any] | None = None,
) -> int:
    write_worker_record(paths, args, kind=kind, status="failed", command_text=command_text, extra=extra)
    write_worker_status(
        paths,
        args,
        kind=kind,
        status="failed",
        started_at=started_at,
        command_text=command_text,
        last_output=last_output,
        exit_code=1,
        extra=extra,
    )
    return 1


def pane_missing(guard: dict[str, Any]) -> bool:
    return guard.get("ok") is False and str(guard.get("reason") or "") == "pane could not be resolved"


def submit_command(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    started_at: str,
    kind: str,
    command_text: str,
    last_output: str,
    extra: dict[str, Any] | None = None,
) -> int:
    send_args = argparse.Namespace(
        pane=args.pane,
        command_text=command_text,
        enter=True,
        no_enter=False,
        require_idle_shell=args.require_idle_shell,
        strict_preflight=args.strict_preflight,
        bash_if_not_executable=args.bash_if_not_executable,
        cwd=str(paths["workspace"]),
    )
    try:
        result = tmux_control.send(send_args)
    except Exception as exc:
        error = str(exc)
        return fail_worker(
            paths,
            args,
            started_at=started_at,
            kind=kind,
            command_text=command_text,
            last_output=error,
            extra={"error": error, **(extra or {})},
        )
    status = "submitted" if result.get("sent_to_pane") else "failed"
    merged_extra = {
        "send_result": result,
        "command_hash": command_hash(command_text),
        **(extra or {}),
    }
    write_worker_record(paths, args, kind=kind, status=status, command_text=command_text, extra=merged_extra)
    write_worker_status(
        paths,
        args,
        kind=kind,
        status=status,
        started_at=started_at,
        command_text=command_text,
        last_output=last_output or str(result.get("reason") or ""),
        exit_code=0 if status == "submitted" else 1,
        extra=merged_extra,
    )
    return 0 if status == "submitted" else 1


def run_queue_after_idle(args: argparse.Namespace) -> int:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    command_text = read_command(args)
    started = time.monotonic()
    kind = "queue-after-idle"
    write_worker_record(paths, args, kind=kind, status="waiting_pane_idle", command_text=command_text)

    while not STOP_REQUESTED:
        try:
            guard = tmux_control.idle_shell_check(args.pane)
        except Exception as exc:
            error = str(exc)
            return fail_worker(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=command_text,
                last_output=error,
                extra={"error": error},
            )
        if pane_missing(guard):
            return fail_worker(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=command_text,
                last_output=str(guard.get("reason")),
                extra={"idle_shell_check": guard},
            )
        if guard.get("ok"):
            return submit_command(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=command_text,
                last_output="pane is idle; submitted command",
                extra={"idle_shell_check": guard},
            )
        if args.timeout_seconds is not None and time.monotonic() - started >= args.timeout_seconds:
            write_worker_record(paths, args, kind=kind, status="timeout", command_text=command_text, extra={"idle_shell_check": guard})
            write_worker_status(
                paths,
                args,
                kind=kind,
                status="timeout",
                started_at=started_at,
                command_text=command_text,
                last_output=str(guard.get("reason") or "timed out waiting for idle shell"),
                exit_code=1,
                extra={"idle_shell_check": guard},
            )
            return 1
        write_worker_record(paths, args, kind=kind, status="waiting_pane_idle", command_text=command_text, extra={"idle_shell_check": guard})
        write_worker_status(
            paths,
            args,
            kind=kind,
            status="waiting_pane_idle",
            started_at=started_at,
            command_text=command_text,
            last_output=str(guard.get("reason") or "waiting for idle shell"),
            extra={"idle_shell_check": guard},
        )
        sleep_interruptibly(args.poll_seconds)

    write_worker_record(paths, args, kind=kind, status="cancelled", command_text=command_text)
    write_worker_status(
        paths,
        args,
        kind=kind,
        status="cancelled",
        started_at=started_at,
        command_text=command_text,
        last_output="cancelled before command submission",
        exit_code=1,
    )
    return 1


def run_queue_after_status(args: argparse.Namespace) -> int:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    command_text = read_command(args)
    started = time.monotonic()
    kind = "queue-after-status"
    observed_file = resolve_status_file(paths, args.status_file)
    required = args.require_row or []
    fail_rows = args.fail_row or []
    write_worker_record(paths, args, kind=kind, status="waiting_status", command_text=command_text, extra={"observed_status_file": str(observed_file)})

    while not STOP_REQUESTED:
        rows = status_rows(observed_file) if observed_file else []
        matched_required = specs_matching(rows, required)
        matched_failed = specs_matching(rows, fail_rows)
        extra = {
            "observed_status_file": str(observed_file),
            "required_rows": required,
            "matched_required_rows": matched_required,
            "fail_rows": fail_rows,
            "matched_fail_rows": matched_failed,
        }

        if matched_failed:
            write_worker_record(paths, args, kind=kind, status="failed", command_text=command_text, extra=extra)
            write_worker_status(
                paths,
                args,
                kind=kind,
                status="failed",
                started_at=started_at,
                command_text=command_text,
                last_output=f"fail row matched: {matched_failed[0]}",
                exit_code=1,
                extra=extra,
            )
            return 1

        if required and len(matched_required) == len(required):
            try:
                guard = tmux_control.idle_shell_check(args.pane) if args.require_idle_shell else {"ok": True}
            except Exception as exc:
                error = str(exc)
                return fail_worker(
                    paths,
                    args,
                    started_at=started_at,
                    kind=kind,
                    command_text=command_text,
                    last_output=error,
                    extra={**extra, "error": error},
                )
            if pane_missing(guard):
                return fail_worker(
                    paths,
                    args,
                    started_at=started_at,
                    kind=kind,
                    command_text=command_text,
                    last_output=str(guard.get("reason")),
                    extra={**extra, "idle_shell_check": guard},
                )
            if guard.get("ok"):
                return submit_command(
                    paths,
                    args,
                    started_at=started_at,
                    kind=kind,
                    command_text=command_text,
                    last_output="status requirements met; submitted command",
                    extra={**extra, "idle_shell_check": guard},
            )
            extra["idle_shell_check"] = guard
            last_output = str(guard.get("reason") or "status met; waiting for idle shell")
            waiting_status = "waiting_pane_idle"
        else:
            last_output = f"matched {len(matched_required)}/{len(required)} required rows"
            waiting_status = "waiting_status"

        if args.timeout_seconds is not None and time.monotonic() - started >= args.timeout_seconds:
            write_worker_record(paths, args, kind=kind, status="timeout", command_text=command_text, extra=extra)
            write_worker_status(
                paths,
                args,
                kind=kind,
                status="timeout",
                started_at=started_at,
                command_text=command_text,
                last_output=last_output,
                exit_code=1,
                extra=extra,
            )
            return 1

        write_worker_record(paths, args, kind=kind, status=waiting_status, command_text=command_text, extra=extra)
        write_worker_status(
            paths,
            args,
            kind=kind,
            status=waiting_status,
            started_at=started_at,
            command_text=command_text,
            last_output=last_output,
            extra=extra,
        )
        sleep_interruptibly(args.poll_seconds)

    write_worker_record(paths, args, kind=kind, status="cancelled", command_text=command_text)
    write_worker_status(
        paths,
        args,
        kind=kind,
        status="cancelled",
        started_at=started_at,
        command_text=command_text,
        last_output="cancelled before command submission",
        exit_code=1,
    )
    return 1


def run_watch(args: argparse.Namespace) -> int:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    started = time.monotonic()
    kind = "watch"
    observed_file = resolve_status_file(paths, args.status_file)
    log_file = tmux_state.log_path(paths, args.job_id)
    write_worker_record(paths, args, kind=kind, status="running", extra={"observed_status_file": str(observed_file) if observed_file else None})

    while not STOP_REQUESTED:
        try:
            output = tmux_control.capture_text(args.pane, args.capture_lines, strip=True)
        except Exception as exc:
            output = str(exc)
            terminal = "failed"
            exit_code = 1
            write_worker_record(paths, args, kind=kind, status=terminal, extra={"error": output})
            write_worker_status(
                paths,
                args,
                kind=kind,
                status=terminal,
                started_at=started_at,
                last_output=output,
                exit_code=exit_code,
                extra={"error": output},
            )
            return exit_code

        observed_tail = ""
        if observed_file and observed_file.exists():
            observed_tail = tmux_state.tail_text(observed_file.read_text(encoding="utf-8", errors="replace"))
        log_file.write_text(output, encoding="utf-8")
        extra = {
            "capture_lines": args.capture_lines,
            "observed_status_file": str(observed_file) if observed_file else None,
            "observed_status_tail": observed_tail,
        }
        write_worker_record(paths, args, kind=kind, status="running", extra=extra)
        write_worker_status(
            paths,
            args,
            kind=kind,
            status="running",
            started_at=started_at,
            last_output=output,
            extra=extra,
        )

        if args.timeout_seconds is not None and time.monotonic() - started >= args.timeout_seconds:
            write_worker_record(paths, args, kind=kind, status="timeout", extra=extra)
            write_worker_status(
                paths,
                args,
                kind=kind,
                status="timeout",
                started_at=started_at,
                last_output=output,
                exit_code=1,
                extra=extra,
            )
            return 1
        sleep_interruptibly(args.interval)

    write_worker_record(paths, args, kind=kind, status="cancelled")
    write_worker_status(
        paths,
        args,
        kind=kind,
        status="cancelled",
        started_at=started_at,
        last_output="watch cancelled",
        exit_code=1,
    )
    return 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--state-dir")
    parser.add_argument("--name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Managed tmux-skills worker")
    subparsers = parser.add_subparsers(dest="action", required=True)

    idle_parser = subparsers.add_parser("queue-after-idle")
    add_common(idle_parser)
    idle_parser.add_argument("--command", dest="command_text")
    idle_parser.add_argument("--command-file")
    idle_parser.add_argument("--poll-seconds", type=float, default=2.0)
    idle_parser.add_argument("--timeout-seconds", type=float)
    idle_parser.add_argument("--require-idle-shell", action="store_true", default=True)
    idle_parser.add_argument("--strict-preflight", action="store_true")
    idle_parser.add_argument("--bash-if-not-executable", action="store_true")

    status_parser = subparsers.add_parser("queue-after-status")
    add_common(status_parser)
    status_parser.add_argument("--command", dest="command_text")
    status_parser.add_argument("--command-file")
    status_parser.add_argument("--status-file", required=True)
    status_parser.add_argument("--require-row", action="append", default=[])
    status_parser.add_argument("--fail-row", action="append", default=[])
    status_parser.add_argument("--poll-seconds", type=float, default=2.0)
    status_parser.add_argument("--timeout-seconds", type=float)
    status_parser.add_argument("--require-idle-shell", action="store_true", default=True)
    status_parser.add_argument("--no-require-idle-shell", dest="require_idle_shell", action="store_false")
    status_parser.add_argument("--strict-preflight", action="store_true")
    status_parser.add_argument("--bash-if-not-executable", action="store_true")

    watch_parser = subparsers.add_parser("watch")
    add_common(watch_parser)
    watch_parser.add_argument("--interval", type=float, default=180.0)
    watch_parser.add_argument("--capture-lines", type=int, default=80)
    watch_parser.add_argument("--status-file")
    watch_parser.add_argument("--timeout-seconds", type=float)

    return parser


def main() -> None:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    args = build_parser().parse_args()
    if args.action in {"queue-after-idle", "queue-after-status"} and not (args.command_text or args.command_file):
        print(f"{args.action} requires --command or --command-file", file=sys.stderr)
        raise SystemExit(2)
    if args.action == "queue-after-idle":
        raise SystemExit(run_queue_after_idle(args))
    if args.action == "queue-after-status":
        if not args.require_row:
            print("queue-after-status requires at least one --require-row", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(run_queue_after_status(args))
    if args.action == "watch":
        raise SystemExit(run_watch(args))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
