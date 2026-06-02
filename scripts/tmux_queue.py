#!/usr/bin/env python3
"""Managed watch and queue workers for tmux-skills."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

import tmux_control
import tmux_state


RETRY_AFTER_IDLE_RECHECK = 75


class WorkerTerminatedBySignal(BaseException):
    """Raised from SIGTERM so worker cleanup can write terminal status."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


def raise_worker_terminated(signum: int, _frame: object) -> None:
    raise WorkerTerminatedBySignal(signum)


def exception_text(exc: BaseException) -> str:
    return repr(exc) if isinstance(exc, SystemExit) else str(exc)


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


def command_hash(command_text: str | None) -> str | None:
    if command_text is None:
        return None
    return hashlib.sha256(command_text.encode("utf-8")).hexdigest()[:16]


def read_command(args: argparse.Namespace) -> str:
    command_file_arg = getattr(args, "command_file", None)
    if command_file_arg is not None:
        if not tmux_state.one_line_text(command_file_arg):
            raise ValueError("command file path is blank")
        command_text = Path(str(command_file_arg)).expanduser().read_text(encoding="utf-8")
    else:
        command_text = "" if getattr(args, "command_text", None) is None else str(args.command_text)
    if not tmux_state.one_line_text(command_text):
        raise ValueError("command is blank")
    return command_text


def fail_command_read(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    started_at: str,
    kind: str,
    error: Exception,
) -> int:
    command_file_arg = getattr(args, "command_file", None)
    command_path = (
        str(Path(str(command_file_arg)).expanduser())
        if command_file_arg is not None and tmux_state.one_line_text(command_file_arg)
        else None
    )
    if isinstance(error, ValueError):
        detail = str(error)
    elif command_path:
        detail = f"could not read command file {command_path}: {error}"
    else:
        detail = f"could not read command: {error}"
    return fail_worker(
        paths,
        args,
        started_at=started_at,
        kind=kind,
        command_text=None,
        last_output=detail,
        extra={"error": detail},
    )


def resolve_status_file(paths: dict[str, Path], status_file: str | None) -> Path | None:
    if not status_file:
        return None
    path = Path(status_file).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (paths["workspace"] / path).resolve()


def required_status_file(paths: dict[str, Path], status_file: str | None) -> Path:
    if status_file is None or not tmux_state.one_line_text(status_file):
        raise ValueError("status file path is blank")
    resolved = resolve_status_file(paths, str(status_file))
    if resolved is None:
        raise ValueError("status file path is blank")
    return resolved


def nonblank_row_specs(rows: list[str] | None, flag_name: str) -> list[str]:
    specs: list[str] = []
    for row in rows or []:
        spec = str(row).strip()
        if not tmux_state.one_line_text(spec):
            raise ValueError(f"{flag_name} is blank")
        specs.append(spec)
    return specs


def optional_status_file(paths: dict[str, Path], status_file: str | None, command_name: str) -> Path | None:
    if status_file is None:
        return None
    if not tmux_state.one_line_text(status_file):
        raise ValueError(f"{command_name} requires nonblank --status-file when provided")
    return resolve_status_file(paths, status_file)


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
        "kind": tmux_state.token_text(kind) or "job",
        "status": tmux_state.token_text(status) or "unknown",
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
    for key in ("dedupe_key", "dedupe_payload", "owner", "duplicate_allowed", "duplicate_of", "argv", "low_token"):
        if previous and key in previous:
            record[key] = previous[key]
    if previous and "check_interval_seconds" in previous:
        record["check_interval_seconds"] = previous["check_interval_seconds"]
    elif check_interval is not None:
        record["check_interval_seconds"] = float(check_interval)
    if extra:
        record.update(extra)
    record["kind"] = tmux_state.token_text(kind) or "job"
    record["status"] = tmux_state.token_text(status) or "unknown"
    tmux_state.strip_managed_transient_fields(record)
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
    data["kind"] = tmux_state.token_text(kind) or "job"
    data["status"] = tmux_state.token_text(status) or "unknown"
    return tmux_state.write_status(status_file, data)


def sleep_interruptibly(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def timed_out(started: float, timeout_seconds: float | None) -> bool:
    return timeout_seconds is not None and time.monotonic() - started >= timeout_seconds


def status_rows(status_file: Path) -> list[str]:
    if not status_file.exists():
        return []
    return [line.rstrip("\n") for line in status_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def row_matches_spec(line: str, spec: str) -> bool:
    spec = str(spec).strip()
    if not tmux_state.one_line_text(spec):
        return False
    if ":" not in spec:
        return spec in line
    key, expected = spec.rsplit(":", 1)
    fields = [field.strip() for field in line.split("\t")]
    return key in fields and expected in fields


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


def active_worker_state(record: dict[str, Any] | None, status: dict[str, Any] | None) -> bool:
    record_state = tmux_state.token_text((record or {}).get("status"))
    if record_state in tmux_state.TERMINAL_STATUSES:
        return False
    if record_state in tmux_state.MANAGED_ACTIVE_STATUSES:
        return True

    status_state = tmux_state.token_text((status or {}).get("status"))
    if status_state in tmux_state.TERMINAL_STATUSES:
        return False
    return status_state in tmux_state.MANAGED_ACTIVE_STATUSES


def terminalize_active_worker(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    job_id: str,
    terminal_status: str,
    last_output: str,
    extra: dict[str, Any] | None = None,
) -> None:
    record_path = tmux_state.job_path(paths, job_id)
    status_file = tmux_state.status_path(paths, job_id)
    record, _record_error = tmux_state.read_json(record_path)
    status, _status_error = tmux_state.read_json(status_file)
    if not active_worker_state(record, status):
        return

    now = tmux_state.utc_now()
    merged_extra = extra or {}
    kind = tmux_state.token_text((record or {}).get("kind") or (status or {}).get("kind") or getattr(args, "action", None)) or "job"
    pane_id = (record or {}).get("pane_id") or (status or {}).get("pane_id") or getattr(args, "pane", None)
    log_file = tmux_state.log_path(paths, job_id)

    record_data = dict(record or {})
    if "id" in record_data:
        record_data["id"] = job_id
    record_data.update(
        {
            "version": record_data.get("version", 1),
            "job_id": job_id,
            "kind": kind,
            "status": terminal_status,
            "pid": os.getpid(),
            "pane_id": pane_id,
            "status_path": str(status_file),
            "log_path": str(log_file),
            "workspace": str(paths["workspace"]),
            "state_dir": str(paths["root"]),
            "updated_at": now,
            "heartbeat_at": now,
            **merged_extra,
        }
    )
    record_data["kind"] = kind
    record_data["status"] = tmux_state.token_text(terminal_status) or "unknown"
    record_data.setdefault("created_at", now)
    tmux_state.strip_managed_transient_fields(record_data)
    tmux_state.atomic_write_json(record_path, record_data)

    if status:
        status_data = tmux_state.normalize_status(status, status_file)
        status_data.update(
            {
                "status": terminal_status,
                "exit_code": 1,
                "updated_at": now,
                "ended_at": now,
                "last_output": tmux_state.tail_text(last_output),
                **merged_extra,
            }
        )
        status_data["kind"] = kind
        status_data["status"] = tmux_state.token_text(terminal_status) or "unknown"
    else:
        status_data = tmux_state.build_status(
            kind=kind,
            item_id=job_id,
            attempt=1,
            name=getattr(args, "name", None),
            status=terminal_status,
            pane_id=pane_id,
            command_preview_text=str((record or {}).get("command_path") or kind),
            cwd=str(paths["workspace"]),
            status_file=status_file,
            log_file=log_file,
            started_at=(record or {}).get("created_at"),
            exit_code=1,
            last_output=last_output,
        )
        status_data.update(merged_extra)
        status_data["kind"] = kind
        status_data["status"] = tmux_state.token_text(terminal_status) or "unknown"
    tmux_state.write_status(status_file, status_data)


def safety_cancel_last_output(args: argparse.Namespace) -> str:
    if getattr(args, "action", None) == "watch":
        return "watch cancelled"
    return "cancelled before command submission"


def cancel_active_worker_safely(paths: dict[str, Path], args: argparse.Namespace, job_id: str) -> None:
    try:
        terminalize_active_worker(
            paths,
            args,
            job_id=job_id,
            terminal_status="cancelled",
            last_output=safety_cancel_last_output(args),
        )
    except BaseException:
        pass


def run_worker_safely(
    args: argparse.Namespace,
    paths: dict[str, Path],
    job_id: str,
    worker: Callable[[argparse.Namespace], int],
) -> int:
    identity_error = reject_invalid_worker_identity(args)
    if identity_error is not None:
        return identity_error
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, raise_worker_terminated)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        return worker(args)
    except WorkerTerminatedBySignal:
        cancel_active_worker_safely(paths, args, job_id)
        return 1
    except KeyboardInterrupt:
        cancel_active_worker_safely(paths, args, job_id)
        return 1
    except BaseException as exc:
        try:
            last_output = f"worker aborted: {exc!r}"
            terminalize_active_worker(
                paths,
                args,
                job_id=job_id,
                terminal_status="failed",
                last_output=last_output,
                extra={"error": last_output},
            )
        except BaseException:
            pass
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


def pane_missing(guard: dict[str, Any]) -> bool:
    return guard.get("ok") is False and str(guard.get("reason") or "") == "pane could not be resolved"


def retryable_idle_send_result(result: dict[str, Any]) -> bool:
    guard = result.get("idle_shell_check")
    return (
        result.get("sent_to_pane") is False
        and isinstance(guard, dict)
        and guard.get("ok") is False
        and not pane_missing(guard)
    )


def submit_command(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    started_at: str,
    kind: str,
    command_text: str,
    last_output: str,
    extra: dict[str, Any] | None = None,
    retry_idle_recheck: bool = False,
) -> int:
    send_args = argparse.Namespace(
        pane=args.pane,
        command_text=command_text,
        enter=True,
        no_enter=False,
        require_idle_shell=args.require_idle_shell,
        strict_preflight=args.strict_preflight,
        bash_if_not_executable=args.bash_if_not_executable,
    )
    try:
        result = tmux_control.send(send_args)
    except (Exception, SystemExit) as exc:
        error = exception_text(exc)
        return fail_worker(
            paths,
            args,
            started_at=started_at,
            kind=kind,
            command_text=command_text,
            last_output=error,
            extra={**(extra or {}), "error": error},
        )
    merged_extra = {
        **(extra or {}),
        "send_result": result,
        "command_hash": command_hash(command_text),
    }
    if retry_idle_recheck and retryable_idle_send_result(result):
        reason = str(result.get("reason") or "pane became busy before command submission")
        retry_extra = {**merged_extra, "idle_shell_check": result.get("idle_shell_check")}
        write_worker_record(paths, args, kind=kind, status="waiting_pane_idle", command_text=command_text, extra=retry_extra)
        write_worker_status(
            paths,
            args,
            kind=kind,
            status="waiting_pane_idle",
            started_at=started_at,
            command_text=command_text,
            last_output=reason,
            extra=retry_extra,
        )
        return RETRY_AFTER_IDLE_RECHECK
    status = "submitted" if result.get("sent_to_pane") else "failed"
    status_token = tmux_state.token_text(status)
    write_worker_record(paths, args, kind=kind, status=status, command_text=command_text, extra=merged_extra)
    write_worker_status(
        paths,
        args,
        kind=kind,
        status=status,
        started_at=started_at,
        command_text=command_text,
        last_output=last_output or str(result.get("reason") or ""),
        exit_code=0 if status_token == "submitted" else 1,
        extra=merged_extra,
    )
    return 0 if status_token == "submitted" else 1


def run_queue_after_idle(args: argparse.Namespace) -> int:
    identity_error = reject_invalid_worker_identity(args)
    if identity_error is not None:
        return identity_error
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    kind = "queue-after-idle"
    try:
        command_text = read_command(args)
    except Exception as exc:
        return fail_command_read(paths, args, started_at=started_at, kind=kind, error=exc)
    started = time.monotonic()
    write_worker_record(paths, args, kind=kind, status="waiting_pane_idle", command_text=command_text)

    while True:
        try:
            guard = tmux_control.idle_shell_check(args.pane)
        except (Exception, SystemExit) as exc:
            error = exception_text(exc)
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
            if timed_out(started, args.timeout_seconds):
                write_worker_record(paths, args, kind=kind, status="timeout", command_text=command_text, extra={"idle_shell_check": guard})
                write_worker_status(
                    paths,
                    args,
                    kind=kind,
                    status="timeout",
                    started_at=started_at,
                    command_text=command_text,
                    last_output="timed out before command submission",
                    exit_code=1,
                    extra={"idle_shell_check": guard},
                )
                return 1
            code = submit_command(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=command_text,
                last_output="pane is idle; submitted command",
                extra={"idle_shell_check": guard},
                retry_idle_recheck=True,
            )
            if code == RETRY_AFTER_IDLE_RECHECK:
                sleep_interruptibly(args.poll_seconds)
                continue
            return code
        if timed_out(started, args.timeout_seconds):
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


def run_queue_after_status(args: argparse.Namespace) -> int:
    identity_error = reject_invalid_worker_identity(args)
    if identity_error is not None:
        return identity_error
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    kind = "queue-after-status"
    try:
        command_text = read_command(args)
    except Exception as exc:
        return fail_command_read(paths, args, started_at=started_at, kind=kind, error=exc)
    try:
        observed_file = required_status_file(paths, args.status_file)
        required = nonblank_row_specs(args.require_row, "--require-row")
        if not required:
            raise ValueError("queue-after-status requires at least one --require-row")
        fail_rows = nonblank_row_specs(args.fail_row, "--fail-row")
    except ValueError as exc:
        return fail_worker(
            paths,
            args,
            started_at=started_at,
            kind=kind,
            command_text=command_text,
            last_output=str(exc),
            extra={"error": str(exc)},
        )
    started = time.monotonic()
    write_worker_record(
        paths,
        args,
        kind=kind,
        status="waiting_status",
        command_text=command_text,
        extra={"observed_status_file": str(observed_file), "low_token": bool(getattr(args, "low_token", False))},
    )

    while True:
        try:
            rows = status_rows(observed_file) if observed_file else []
        except OSError as exc:
            error = f"could not read status file {observed_file}: {exc}"
            return fail_worker(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=command_text,
                last_output=error,
                extra={"observed_status_file": str(observed_file), "error": error},
            )
        matched_required = specs_matching(rows, required)
        matched_failed = specs_matching(rows, fail_rows)
        extra = {
            "observed_status_file": str(observed_file),
            "low_token": bool(getattr(args, "low_token", False)),
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
            except (Exception, SystemExit) as exc:
                error = exception_text(exc)
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
                if timed_out(started, args.timeout_seconds):
                    timeout_extra = {**extra, "idle_shell_check": guard}
                    write_worker_record(paths, args, kind=kind, status="timeout", command_text=command_text, extra=timeout_extra)
                    write_worker_status(
                        paths,
                        args,
                        kind=kind,
                        status="timeout",
                        started_at=started_at,
                        command_text=command_text,
                        last_output="timed out before command submission",
                        exit_code=1,
                        extra=timeout_extra,
                    )
                    return 1
                code = submit_command(
                    paths,
                    args,
                    started_at=started_at,
                    kind=kind,
                    command_text=command_text,
                    last_output="status requirements met; submitted command",
                    extra={**extra, "idle_shell_check": guard},
                    retry_idle_recheck=True,
                )
                if code == RETRY_AFTER_IDLE_RECHECK:
                    sleep_interruptibly(args.poll_seconds)
                    continue
                return code
            extra["idle_shell_check"] = guard
            last_output = str(guard.get("reason") or "status met; waiting for idle shell")
            waiting_status = "waiting_pane_idle"
        else:
            last_output = f"matched {len(matched_required)}/{len(required)} required rows"
            waiting_status = "waiting_status"

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

        if timed_out(started, args.timeout_seconds):
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

        sleep_interruptibly(args.poll_seconds)


def run_watch(args: argparse.Namespace) -> int:
    identity_error = reject_invalid_worker_identity(args)
    if identity_error is not None:
        return identity_error
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    started_at = tmux_state.utc_now()
    started = time.monotonic()
    kind = "watch"
    status_lines = getattr(args, "status_lines", tmux_state.DEFAULT_STATUS_LINES)
    status_max_chars = getattr(args, "status_max_chars", tmux_state.DEFAULT_STATUS_MAX_CHARS)
    status_tail_extra = {"status_lines": status_lines, "status_max_chars": status_max_chars}
    try:
        observed_file = optional_status_file(paths, args.status_file, "watch")
    except ValueError as exc:
        return fail_worker(
            paths,
            args,
            started_at=started_at,
            kind=kind,
            command_text=None,
            last_output=str(exc),
            extra={**status_tail_extra, "error": str(exc)},
        )
    log_file = tmux_state.log_path(paths, args.job_id)
    write_worker_record(
        paths,
        args,
        kind=kind,
        status="running",
        extra={
            "observed_status_file": str(observed_file) if observed_file else None,
            **status_tail_extra,
        },
    )

    while True:
        observed_tail = ""
        if observed_file and observed_file.exists():
            try:
                observed_tail = tmux_state.tail_text(observed_file.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                error = f"could not read observed status file {observed_file}: {exc}"
                return fail_worker(
                    paths,
                    args,
                    started_at=started_at,
                    kind=kind,
                    command_text=None,
                    last_output=error,
                    extra={**status_tail_extra, "observed_status_file": str(observed_file), "error": error},
                )
        elif getattr(args, "low_token", False):
            observed_tail = "status file not found; pane not captured"

        if getattr(args, "low_token", False):
            output = observed_tail
        else:
            try:
                output = tmux_control.capture_text(args.pane, args.capture_lines, strip=True)
            except (Exception, SystemExit) as exc:
                output = exception_text(exc)
                terminal = "failed"
                exit_code = 1
                error_extra = {**status_tail_extra, "error": output}
                write_worker_record(paths, args, kind=kind, status=terminal, extra=error_extra)
                write_worker_status(
                    paths,
                    args,
                    kind=kind,
                    status=terminal,
                    started_at=started_at,
                    last_output=tmux_state.status_tail(output, lines=status_lines, max_chars=status_max_chars),
                    exit_code=exit_code,
                    extra=error_extra,
                )
                return exit_code
        try:
            log_file.write_text(output, encoding="utf-8")
        except OSError as exc:
            error = f"could not write watch log file {log_file}: {exc}"
            return fail_worker(
                paths,
                args,
                started_at=started_at,
                kind=kind,
                command_text=None,
                last_output=error,
                extra={**status_tail_extra, "error": error},
            )
        extra = {
            "capture_lines": args.capture_lines,
            "low_token": bool(getattr(args, "low_token", False)),
            **status_tail_extra,
            "observed_status_file": str(observed_file) if observed_file else None,
            "observed_status_tail": observed_tail,
        }
        status_output = tmux_state.status_tail(output, lines=status_lines, max_chars=status_max_chars)
        write_worker_record(paths, args, kind=kind, status="running", extra=extra)
        write_worker_status(
            paths,
            args,
            kind=kind,
            status="running",
            started_at=started_at,
            last_output=status_output,
            extra=extra,
        )

        if timed_out(started, args.timeout_seconds):
            write_worker_record(paths, args, kind=kind, status="timeout", extra=extra)
            write_worker_status(
                paths,
                args,
                kind=kind,
                status="timeout",
                started_at=started_at,
                last_output=status_output,
                exit_code=1,
                extra=extra,
            )
            return 1
        sleep_interruptibly(args.interval)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--state-dir")
    parser.add_argument("--name")


def validate_worker_identity(args: argparse.Namespace) -> None:
    if not tmux_state.one_line_text(getattr(args, "job_id", None)):
        raise ValueError("managed worker requires nonblank --job-id")
    if not tmux_state.one_line_text(getattr(args, "pane", None)):
        raise ValueError("managed worker requires nonblank --pane")


def reject_invalid_worker_identity(args: argparse.Namespace) -> int | None:
    try:
        validate_worker_identity(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Managed tmux-skills worker")
    subparsers = parser.add_subparsers(dest="action", required=True)

    idle_parser = subparsers.add_parser("queue-after-idle")
    add_common(idle_parser)
    idle_command = idle_parser.add_mutually_exclusive_group(required=True)
    idle_command.add_argument("--command", dest="command_text")
    idle_command.add_argument("--command-file")
    idle_parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    idle_parser.add_argument("--timeout-seconds", type=positive_float)
    idle_parser.add_argument("--require-idle-shell", action="store_true", default=True)
    idle_parser.add_argument("--strict-preflight", action="store_true")
    idle_parser.add_argument("--bash-if-not-executable", action="store_true")

    status_parser = subparsers.add_parser("queue-after-status")
    add_common(status_parser)
    status_command = status_parser.add_mutually_exclusive_group(required=True)
    status_command.add_argument("--command", dest="command_text")
    status_command.add_argument("--command-file")
    status_parser.add_argument("--status-file", required=True)
    status_parser.add_argument("--require-row", action="append", default=[])
    status_parser.add_argument("--fail-row", action="append", default=[])
    status_parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    status_parser.add_argument("--timeout-seconds", type=positive_float)
    status_parser.add_argument("--require-idle-shell", action="store_true", default=True)
    status_parser.add_argument("--no-require-idle-shell", dest="require_idle_shell", action="store_false")
    status_parser.add_argument("--strict-preflight", action="store_true")
    status_parser.add_argument("--bash-if-not-executable", action="store_true")
    status_parser.add_argument("--low-token", action="store_true")

    watch_parser = subparsers.add_parser("watch")
    add_common(watch_parser)
    watch_parser.add_argument("--interval", type=positive_float, default=180.0)
    watch_parser.add_argument("--capture-lines", type=positive_int, default=80)
    watch_parser.add_argument("--status-lines", type=positive_int, default=tmux_state.DEFAULT_STATUS_LINES)
    watch_parser.add_argument("--status-max-chars", type=positive_int, default=tmux_state.DEFAULT_STATUS_MAX_CHARS)
    watch_parser.add_argument("--status-file")
    watch_parser.add_argument("--low-token", action="store_true")
    watch_parser.add_argument("--timeout-seconds", type=positive_float)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        validate_worker_identity(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    job_id = tmux_state.safe_id(args.job_id)
    if args.action == "queue-after-idle":
        raise SystemExit(run_worker_safely(args, paths, job_id, run_queue_after_idle))
    if args.action == "queue-after-status":
        try:
            args.require_row = nonblank_row_specs(args.require_row, "--require-row")
            if not args.require_row:
                raise ValueError("queue-after-status requires at least one --require-row")
            args.fail_row = nonblank_row_specs(args.fail_row, "--fail-row")
            required_status_file(paths, args.status_file)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(run_worker_safely(args, paths, job_id, run_queue_after_status))
    if args.action == "watch":
        try:
            if getattr(args, "low_token", False) and not tmux_state.one_line_text(getattr(args, "status_file", None)):
                raise ValueError("watch --low-token requires --status-file")
            optional_status_file(paths, args.status_file, "watch")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(run_worker_safely(args, paths, job_id, run_watch))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
