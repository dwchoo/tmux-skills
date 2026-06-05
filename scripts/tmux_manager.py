#!/usr/bin/env python3
"""Visible manager pane loop for tmux-skills long tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import codex_app_server_client
import tmux_bridge
import tmux_state


MANAGER_VERSION = 1
DEFAULT_MANAGER_LOG_MAX_BYTES = 65536
MANAGER_STATUSES = {"starting", "idle", "queued", "running", "waiting_for_codex", "cancel_requested", "cancelled", "failed"}
MANAGER_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "stopped", "timeout", "cancelled", "stale"}


def manager_id_value(value: str | None) -> str:
    if not tmux_state.one_line_text(value):
        raise ValueError("manager requires nonblank --manager-id")
    return tmux_state.safe_id(str(value))


def manager_paths(workspace: str | None = None, state_dir: str | None = None) -> dict[str, Path]:
    paths = tmux_state.state_paths(workspace, state_dir)
    tmux_state.ensure_state_dirs(paths)
    return paths


def manager_record_path(paths: dict[str, Path], manager_id: str) -> Path:
    return paths["managers"] / f"{manager_id_value(manager_id)}.json"


def manager_command_request_path(paths: dict[str, Path], manager_id: str, job_id: str) -> Path:
    return paths["commands"] / f"{manager_id_value(manager_id)}-{tmux_state.safe_id(job_id)}.manager-command.sh"


def manager_dashboard_path(paths: dict[str, Path], manager_id: str) -> Path:
    return paths["managers"] / f"{manager_id_value(manager_id)}.dashboard.txt"


def write_dashboard_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def render_dashboard_to_pane(pane_id: str | None, dashboard_file: Path) -> dict[str, Any]:
    if not pane_exists(pane_id):
        return {"rendered": False, "reason": "manager pane is missing", "pane_id": pane_id}
    command = f"printf '\\033[2J\\033[H'; cat {shlex.quote(str(dashboard_file))} 2>/dev/null || true"
    try:
        proc = subprocess.run(
            [*tmux_command_prefix(), "send-keys", "-t", str(pane_id), command, "Enter"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {"rendered": False, "reason": "tmux is not installed", "pane_id": pane_id}
    if proc.returncode != 0:
        return {
            "rendered": False,
            "reason": proc.stderr.strip() or f"tmux send-keys exited {proc.returncode}",
            "pane_id": pane_id,
        }
    return {"rendered": True, "pane_id": pane_id}


def normalize_notify(mode: str, thread_id: str | None = None, endpoint: str | None = None) -> dict[str, Any]:
    if mode == "none":
        return {"mode": "none"}
    if mode != "bridge":
        raise ValueError(f"unsupported manager notify mode: {mode}")
    if not tmux_state.one_line_text(thread_id):
        raise ValueError("manager start --notify bridge requires --thread-id")
    if not tmux_state.one_line_text(endpoint):
        raise ValueError("manager start --notify bridge requires --endpoint")
    endpoint_text = str(endpoint)
    endpoint_info = codex_app_server_client.parse_endpoint(endpoint_text)
    return {
        "mode": "bridge",
        "thread_id": str(thread_id),
        "endpoint": endpoint_text,
        "socket_path": endpoint_info.socket_path,
    }


def normalize_manager_record(record: dict[str, Any], paths: dict[str, Path] | None = None, path: Path | None = None) -> dict[str, Any]:
    normalized = dict(record)
    manager_id = manager_id_value(str(path.stem if path else normalized.get("manager_id")))
    normalized["version"] = int(normalized.get("version") or MANAGER_VERSION)
    normalized["manager_id"] = manager_id
    normalized["status"] = tmux_state.token_text(normalized.get("status")) or "starting"
    if normalized["status"] not in MANAGER_STATUSES:
        normalized["status"] = "failed"
    normalized.setdefault("manager_pane_id", None)
    normalized.setdefault("worker_pane_id", None)
    normalized.setdefault("current_job_id", None)
    job_ids = normalized.get("job_ids")
    normalized["job_ids"] = [tmux_state.safe_id(str(value)) for value in job_ids] if isinstance(job_ids, list) else []
    normalized.setdefault("notify", {"mode": "none"})
    normalized.setdefault("heartbeat_at", None)
    normalized.setdefault("last_terminal_event_id", None)
    normalized.setdefault("workspace", str(paths["workspace"]) if paths else None)
    normalized.setdefault("state_dir", str(paths["root"]) if paths else None)
    normalized.setdefault("created_at", normalized.get("updated_at") or tmux_state.utc_now())
    normalized.setdefault("updated_at", normalized.get("created_at"))
    normalized.setdefault("pending_job", None)
    normalized.setdefault("jobs", {})
    normalized.setdefault("notified_event_ids", [])
    normalized.setdefault("last_notification", None)
    normalized.setdefault("last_error", None)
    normalized.setdefault("dashboard_path", str(manager_dashboard_path(paths, manager_id)) if paths else None)
    normalized.setdefault("manager_process_mode", "foreground")
    normalized.setdefault("manager_pid", None)
    normalized.setdefault("manager_process_started_at", None)
    try:
        log_max_bytes = int(normalized.get("log_max_bytes") or DEFAULT_MANAGER_LOG_MAX_BYTES)
    except (TypeError, ValueError):
        log_max_bytes = DEFAULT_MANAGER_LOG_MAX_BYTES
    normalized["log_max_bytes"] = max(1, log_max_bytes)
    if paths:
        normalized["workspace"] = str(paths["workspace"])
        normalized["state_dir"] = str(paths["root"])
        normalized["manager_path"] = str(manager_record_path(paths, manager_id))
        normalized["dashboard_path"] = str(manager_dashboard_path(paths, manager_id))
    else:
        normalized.setdefault("manager_path", str(path) if path else None)
    return normalized


def read_manager_record(paths: dict[str, Path], manager_id: str) -> tuple[dict[str, Any] | None, str | None]:
    path = manager_record_path(paths, manager_id)
    data, error = tmux_state.read_json(path)
    if error or data is None:
        return None, error
    try:
        return normalize_manager_record(data, paths, path), None
    except Exception as exc:
        return None, str(exc)


def write_manager_record(paths: dict[str, Path], record: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_manager_record(record, paths)
    normalized["updated_at"] = tmux_state.utc_now()
    tmux_state.atomic_write_json(manager_record_path(paths, normalized["manager_id"]), normalized)
    return normalized


def command_text_from_source(command_text: str | None, command_file: str | None) -> tuple[str | None, str | None]:
    try:
        if command_file is not None:
            if not tmux_state.one_line_text(command_file):
                return None, "command file path is blank"
            return Path(str(command_file)).expanduser().read_text(encoding="utf-8"), None
        return "" if command_text is None else str(command_text), None
    except Exception as exc:
        return None, f"could not read command file: {exc}"


def write_command_request(paths: dict[str, Path], manager_id: str, job_id: str, command_text: str) -> Path:
    path = manager_command_request_path(paths, manager_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(command_text, encoding="utf-8")
    return path


def build_pending_job(job_id: str, command_request_path: Path, cwd: str | None = None) -> dict[str, Any]:
    return {
        "job_id": tmux_state.safe_id(job_id),
        "command_file": str(command_request_path),
        "cwd": cwd,
        "queued_at": tmux_state.utc_now(),
    }


def build_manager_record(
    *,
    manager_id: str,
    manager_pane_id: str,
    worker_pane_id: str,
    pending_job: dict[str, Any] | None,
    notify: dict[str, Any],
    workspace: str,
    state_dir: str,
    attach_command: str | None = None,
    poll_seconds: float = 2.0,
    log_max_bytes: int = DEFAULT_MANAGER_LOG_MAX_BYTES,
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    now = tmux_state.utc_now()
    return normalize_manager_record(
        {
            "version": MANAGER_VERSION,
            "manager_id": manager_id,
            "status": "queued" if pending_job else "idle",
            "manager_pane_id": manager_pane_id,
            "worker_pane_id": worker_pane_id,
            "current_job_id": None,
            "job_ids": [],
            "notify": notify,
            "heartbeat_at": None,
            "last_terminal_event_id": None,
            "workspace": str(paths["workspace"]),
            "state_dir": str(paths["root"]),
            "created_at": now,
            "updated_at": now,
            "pending_job": pending_job,
            "jobs": {},
            "notified_event_ids": [],
            "last_notification": None,
            "last_error": None,
            "dashboard_path": str(manager_dashboard_path(paths, manager_id)),
            "manager_process_mode": "foreground",
            "manager_pid": os.getpid(),
            "manager_process_started_at": now,
            "attach_command": attach_command,
            "poll_seconds": poll_seconds,
            "log_max_bytes": log_max_bytes,
        },
        paths,
    )


def queue_manager_job(
    *,
    manager_id: str,
    job_id: str,
    command_text: str | None,
    command_file: str | None,
    workspace: str | None = None,
    state_dir: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    if not tmux_state.one_line_text(job_id):
        return {"manager_id": manager_id, "queued": False, "reason": "manager run-next requires nonblank --job-id"}
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, manager_id)
    item_id = tmux_state.safe_id(job_id)
    if error:
        return {"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": error}
    if record is None:
        return {"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": "manager record not found"}
    if record.get("pending_job"):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager already has a pending job"}
    if record.get("status") == "running":
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager is already running a job"}

    text, read_error = command_text_from_source(command_text, command_file)
    if read_error:
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": read_error}
    if not tmux_state.one_line_text(text):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "command is blank"}

    request_path = write_command_request(paths, record["manager_id"], item_id, str(text))
    record["pending_job"] = build_pending_job(item_id, request_path, cwd)
    record["status"] = "queued"
    record["last_error"] = None
    record = write_manager_record(paths, record)
    return {
        "manager_id": record["manager_id"],
        "job_id": item_id,
        "queued": True,
        "manager_path": record["manager_path"],
        "command_request_path": str(request_path),
        "record": record,
    }


def parse_json_output(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def start_pending_job(record: dict[str, Any]) -> dict[str, Any]:
    pending = record.get("pending_job")
    if not isinstance(pending, dict):
        return record
    job_id = tmux_state.safe_id(str(pending.get("job_id") or "job"))
    argv = [
        sys.executable,
        str(script_dir() / "tmux_control.py"),
        "run",
        "--pane",
        str(record.get("worker_pane_id") or ""),
        "--job-id",
        job_id,
        "--command-file",
        str(pending.get("command_file") or ""),
        "--workspace",
        str(record["workspace"]),
        "--state-dir",
        str(record["state_dir"]),
    ]
    if pending.get("cwd"):
        argv.extend(["--cwd", str(pending["cwd"])])
    started_at = tmux_state.utc_now()
    proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    result = parse_json_output(proc.stdout) or {
        "job_id": job_id,
        "sent": False,
        "reason": proc.stderr.strip() or proc.stdout.strip() or f"tmux_control.py run exited {proc.returncode}",
    }
    jobs = dict(record.get("jobs") or {})
    jobs[job_id] = {
        "job_id": job_id,
        "command_request_path": pending.get("command_file"),
        "status_path": result.get("status_path"),
        "log_path": result.get("log_path"),
        "started_at": started_at,
        "run_returncode": proc.returncode,
        "run_result": result,
    }
    record["jobs"] = jobs
    record["pending_job"] = None
    record["current_job_id"] = job_id
    record["last_terminal_event_id"] = None
    record["last_terminal_candidate"] = None
    record["last_notification"] = None
    record["last_log_trim"] = None
    job_ids = list(record.get("job_ids") or [])
    if job_id not in job_ids:
        job_ids.append(job_id)
    record["job_ids"] = job_ids
    record["status"] = "running"
    record["last_error"] = None if proc.returncode == 0 else result.get("reason") or proc.stderr.strip()
    return record


def tmux_command_prefix() -> list[str]:
    raw = os.environ.get("TMUX")
    if raw:
        socket_path = raw.split(",", 1)[0]
        if socket_path:
            return ["tmux", "-S", socket_path]
    return ["tmux"]


def pane_exists(pane_id: str | None) -> bool:
    if not tmux_state.one_line_text(pane_id):
        return False
    try:
        proc = subprocess.run(
            [*tmux_command_prefix(), "display-message", "-p", "-t", str(pane_id), "#{pane_id}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == str(pane_id)


def send_worker_interrupt(pane_id: str | None) -> dict[str, Any]:
    if not tmux_state.one_line_text(pane_id):
        return {"sent": False, "reason": "worker pane id is blank"}
    try:
        proc = subprocess.run(
            [*tmux_command_prefix(), "send-keys", "-t", str(pane_id), "C-c"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {"sent": False, "reason": "tmux is not installed or is not on PATH"}
    return {"sent": proc.returncode == 0, "returncode": proc.returncode, "stderr": proc.stderr.strip()}


def trim_log_tail(log_path: str | None, max_bytes: int) -> dict[str, Any]:
    if not tmux_state.one_line_text(log_path):
        return {"trimmed": False, "reason": "log path is blank"}
    path = Path(str(log_path))
    if max_bytes <= 0:
        return {"trimmed": False, "reason": "max bytes must be positive", "log_path": str(path)}
    try:
        size_before = path.stat().st_size
    except FileNotFoundError:
        return {"trimmed": False, "reason": "log file not found", "log_path": str(path)}
    except OSError as exc:
        return {"trimmed": False, "reason": str(exc), "log_path": str(path)}
    if size_before <= max_bytes:
        return {"trimmed": False, "log_path": str(path), "size_before": size_before, "size_after": size_before}
    try:
        with path.open("r+b") as handle:
            handle.seek(-max_bytes, os.SEEK_END)
            tail = handle.read(max_bytes)
            handle.seek(0)
            handle.write(tail)
            handle.truncate()
        size_after = path.stat().st_size
    except OSError as exc:
        return {"trimmed": False, "reason": str(exc), "log_path": str(path), "size_before": size_before}
    return {
        "trimmed": True,
        "log_path": str(path),
        "size_before": size_before,
        "size_after": size_after,
        "max_bytes": max_bytes,
        "trimmed_at": tmux_state.utc_now(),
    }


def enforce_log_retention(record: dict[str, Any]) -> dict[str, Any]:
    job_id = str(record.get("current_job_id") or "")
    if not job_id:
        return record
    try:
        max_bytes = int(record.get("log_max_bytes") or DEFAULT_MANAGER_LOG_MAX_BYTES)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MANAGER_LOG_MAX_BYTES
    if max_bytes <= 0:
        return record
    job = (record.get("jobs") or {}).get(job_id, {})
    log_path = job.get("log_path") if isinstance(job, dict) else None
    result = trim_log_tail(str(log_path) if log_path else None, max_bytes)
    if result.get("trimmed"):
        result["job_id"] = job_id
        record["last_log_trim"] = result
    return record


def load_job_status(paths: dict[str, Path], job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    path = tmux_state.status_path(paths, job_id)
    data, error = tmux_state.read_json(path)
    if error or data is None:
        return None
    return tmux_state.normalize_status(data, path)


def task_path_for_job(paths: dict[str, Path], job_id: str | None) -> str | None:
    if not job_id:
        return None
    tasks, _errors = tmux_state.load_tasks(paths["root"])
    safe_job_id = tmux_state.safe_id(job_id)
    for task in tasks:
        if task.get("after_job_id") == safe_job_id:
            return str(task.get("task_path") or "")
    return None


def worker_missing_event_id(record: dict[str, Any]) -> str:
    payload = {
        "manager_id": record.get("manager_id"),
        "worker_pane_id": record.get("worker_pane_id"),
        "current_job_id": record.get("current_job_id"),
        "event": "worker_pane_missing",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_manager_wake_prompt(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    return "\n".join(
        [
            "tmux-control manager observed a terminal event.",
            "",
            f"Workspace: {record.get('workspace')}",
            f"Manager path: {record.get('manager_path') or 'none'}",
            f"Job ID: {candidate.get('job_id') or 'unknown'}",
            f"Status path: {candidate.get('status_path') or 'none'}",
            f"Task path: {candidate.get('task_path') or 'none'}",
            f"Log path: {candidate.get('log_path') or 'none'}",
            "",
            "Please inspect these paths and continue the requested work.",
        ]
    )


def notify_terminal_event(record: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    event_id = str(candidate["event_id"])
    notified = list(record.get("notified_event_ids") or [])
    if event_id in notified:
        return record
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    now = tmux_state.utc_now()
    if notify.get("mode") == "none":
        record["last_notification"] = {"event_id": event_id, "mode": "none", "attempted_at": now, "delivered": False}
        notified.append(event_id)
        record["notified_event_ids"] = notified
        return record
    prompt = build_manager_wake_prompt(record, candidate)
    bridge_record = {
        "endpoint": notify.get("endpoint"),
        "thread_id": notify.get("thread_id"),
        "workspace": record.get("workspace"),
    }
    try:
        delivery = tmux_bridge.deliver_bridge_candidate(bridge_record, candidate, prompt)
        record["last_notification"] = {
            "event_id": event_id,
            "mode": "bridge",
            "attempted_at": now,
            "delivered": True,
            "prompt_sha256": delivery.get("prompt_sha256"),
            "delivery": delivery,
        }
        notified.append(event_id)
        record["notified_event_ids"] = notified
    except Exception as exc:
        record["last_notification"] = {
            "event_id": event_id,
            "mode": "bridge",
            "attempted_at": now,
            "delivered": False,
            "error": str(exc),
        }
    return record


def transition_terminal(
    record: dict[str, Any],
    *,
    paths: dict[str, Path],
    status: dict[str, Any] | None,
    worker_missing: bool = False,
) -> dict[str, Any]:
    job_id = str(record.get("current_job_id") or "")
    if worker_missing:
        event_id = worker_missing_event_id(record)
        candidate = {
            "source": "manager_worker_missing",
            "event_id": event_id,
            "job_id": job_id,
            "status_path": str(tmux_state.status_path(paths, job_id)) if job_id else None,
            "task_path": task_path_for_job(paths, job_id),
            "log_path": str(tmux_state.log_path(paths, job_id)) if job_id else None,
        }
    elif status is not None and tmux_state.is_terminal(status):
        event_id = str(status.get("event_id") or tmux_state.terminal_event_id(status))
        candidate = {
            "source": "manager_terminal",
            "event_id": event_id,
            "job_id": status.get("id") or job_id,
            "status_path": status.get("status_path"),
            "task_path": task_path_for_job(paths, str(status.get("id") or job_id)),
            "log_path": status.get("log_path"),
        }
    else:
        return record
    record["last_terminal_event_id"] = candidate["event_id"]
    record["last_terminal_candidate"] = candidate
    record = notify_terminal_event(record, candidate)
    record["status"] = "waiting_for_codex"
    return record


def manager_cycle(record: dict[str, Any], *, paths: dict[str, Path]) -> dict[str, Any]:
    record["heartbeat_at"] = tmux_state.utc_now()
    if record.get("status") == "cancel_requested":
        record["status"] = "cancelled"
        return record
    if record.get("pending_job"):
        record = start_pending_job(record)
    elif record.get("status") == "waiting_for_codex":
        record = enforce_log_retention(record)
        candidate = record.get("last_terminal_candidate")
        notify = record.get("notify") if isinstance(record.get("notify"), dict) else {}
        event_id = str(candidate.get("event_id") or "") if isinstance(candidate, dict) else ""
        notified = list(record.get("notified_event_ids") or [])
        if notify.get("mode") == "bridge" and event_id and event_id not in notified:
            record = notify_terminal_event(record, candidate)
        return record
    if record.get("current_job_id") and not pane_exists(str(record.get("worker_pane_id") or "")):
        record = enforce_log_retention(record)
        return transition_terminal(record, paths=paths, status=None, worker_missing=True)
    record = enforce_log_retention(record)
    status = load_job_status(paths, str(record.get("current_job_id") or ""))
    if status and tmux_state.is_terminal(status):
        return transition_terminal(record, paths=paths, status=status)
    if record.get("current_job_id"):
        record["status"] = "running"
    elif record.get("status") not in {"cancel_requested", "cancelled", "failed"}:
        record["status"] = "idle"
    return record


def merge_external_manager_update(record: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    if not latest:
        return record
    if latest.get("status") == "cancel_requested" and record.get("status") != "cancelled":
        merged = dict(latest)
        merged["heartbeat_at"] = record.get("heartbeat_at")
        return merged
    if latest.get("pending_job") and not record.get("pending_job") and record.get("status") in {"waiting_for_codex", "idle"}:
        merged = dict(latest)
        merged["heartbeat_at"] = record.get("heartbeat_at")
        return merged
    return record


def manager_status(manager_id: str, workspace: str | None = None, state_dir: str | None = None) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    item_id = manager_id_value(manager_id)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "found": False, "reason": error, "manager_path": str(manager_record_path(paths, item_id))}
    if record is None:
        return {"manager_id": item_id, "found": False, "reason": "manager record not found", "manager_path": str(manager_record_path(paths, item_id))}
    job_status = load_job_status(paths, str(record.get("current_job_id") or ""))
    return {"manager_id": item_id, "found": True, "record": record, "current_job_status": job_status}


def cancel_manager(
    manager_id: str,
    *,
    workspace: str | None = None,
    state_dir: str | None = None,
    stop_worker: bool = False,
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    item_id = manager_id_value(manager_id)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "cancelled": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "cancelled": False, "reason": "manager record not found"}
    worker_stop_result = None
    if stop_worker:
        worker_stop_result = send_worker_interrupt(str(record.get("worker_pane_id") or ""))
    record["status"] = "cancel_requested"
    record["cancel_requested_at"] = tmux_state.utc_now()
    record["stop_worker_requested"] = bool(stop_worker)
    record["worker_stop_result"] = worker_stop_result
    record = write_manager_record(paths, record)
    return {
        "manager_id": item_id,
        "cancelled": True,
        "stop_worker": bool(stop_worker),
        "worker_stop_result": worker_stop_result,
        "manager_path": record["manager_path"],
        "record": record,
    }


def cleanup_path_allowed(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def cleanup_manager(
    manager_id: str,
    *,
    workspace: str | None = None,
    state_dir: str | None = None,
    include_jobs: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    item_id = manager_id_value(manager_id)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "cleaned": False, "reason": error}
    if record is None:
        return {
            "manager_id": item_id,
            "cleaned": False,
            "reason": "manager record not found",
            "manager_path": str(manager_record_path(paths, item_id)),
        }
    job_ids = [str(value) for value in record.get("job_ids") or []]
    current_job_id = str(record.get("current_job_id") or "")
    if current_job_id and current_job_id not in job_ids:
        job_ids.append(current_job_id)
    if include_jobs and not force:
        for job_id in job_ids:
            status = load_job_status(paths, job_id)
            if status and not tmux_state.is_terminal(status):
                return {
                    "manager_id": item_id,
                    "cleaned": False,
                    "reason": f"manager cleanup refuses non-terminal job without --force: {job_id}",
                    "job_id": job_id,
                }

    candidates: list[Path] = [manager_record_path(paths, item_id), manager_dashboard_path(paths, item_id)]
    if include_jobs:
        pending = record.get("pending_job") if isinstance(record.get("pending_job"), dict) else {}
        if pending.get("command_file"):
            candidates.append(Path(str(pending["command_file"])))
        jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
        for job_id in job_ids:
            candidates.extend([tmux_state.command_path(paths, job_id), tmux_state.status_path(paths, job_id), tmux_state.log_path(paths, job_id)])
            job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
            for key in ("command_request_path", "status_path", "log_path"):
                if job.get(key):
                    candidates.append(Path(str(job[key])))
            run_result = job.get("run_result") if isinstance(job.get("run_result"), dict) else {}
            for key in ("command_path", "status_path", "log_path"):
                if run_result.get(key):
                    candidates.append(Path(str(run_result[key])))

    removed: list[str] = []
    missing: list[str] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = candidate.expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not cleanup_path_allowed(path, paths["root"]):
            skipped.append({"path": str(path), "reason": "outside state directory"})
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            missing.append(str(path))
        except IsADirectoryError:
            skipped.append({"path": str(path), "reason": "is a directory"})
        except OSError as exc:
            skipped.append({"path": str(path), "reason": str(exc)})

    return {
        "manager_id": item_id,
        "cleaned": not skipped,
        "manager_path": str(manager_record_path(paths, item_id)),
        "include_jobs": bool(include_jobs),
        "removed": removed,
        "missing": missing,
        "skipped": skipped,
    }


def dashboard_text(record: dict[str, Any], job_status: dict[str, Any] | None = None) -> str:
    current_job_id = record.get("current_job_id") or "none"
    job = (record.get("jobs") or {}).get(current_job_id, {}) if current_job_id != "none" else {}
    status_path = (job_status or {}).get("status_path") or job.get("status_path") or "none"
    log_path = (job_status or {}).get("log_path") or job.get("log_path") or "none"
    task_path = "none"
    try:
        paths = manager_paths(str(record.get("workspace")), str(record.get("state_dir")))
        task_path = task_path_for_job(paths, str(current_job_id)) or "none"
    except Exception:
        task_path = "none"
    lines = [
        "tmux-skills manager",
        f"manager_id: {record.get('manager_id')}",
        f"status: {record.get('status')}",
        f"manager_pane_id: {record.get('manager_pane_id')}",
        f"worker_pane_id: {record.get('worker_pane_id')}",
        f"current_job_id: {current_job_id}",
        f"job_status: {(job_status or {}).get('status') or 'unknown'}",
        f"heartbeat_at: {record.get('heartbeat_at') or 'none'}",
        f"manager_path: {record.get('manager_path')}",
        f"status_path: {status_path}",
        f"log_path: {log_path}",
        f"log_max_bytes: {record.get('log_max_bytes')}",
        f"task_path: {task_path}",
        f"last_terminal_event_id: {record.get('last_terminal_event_id') or 'none'}",
    ]
    last_log_trim = record.get("last_log_trim") if isinstance(record.get("last_log_trim"), dict) else {}
    if last_log_trim:
        lines.append(
            "last_log_trim: "
            f"job={last_log_trim.get('job_id') or 'none'} "
            f"size_before={last_log_trim.get('size_before') or 'none'} "
            f"size_after={last_log_trim.get('size_after') or 'none'}"
        )
    notification = record.get("last_notification")
    if isinstance(notification, dict):
        lines.append(f"last_notification: {notification.get('mode')} delivered={notification.get('delivered')}")
        delivery = notification.get("delivery") if isinstance(notification.get("delivery"), dict) else {}
        if notification.get("delivered"):
            lines.append(f"last_notification_response_id: {delivery.get('response_id') or 'none'}")
            lines.append(f"last_notification_turn_id: {delivery.get('turn_id') or 'none'}")
        elif notification.get("error"):
            lines.append(f"last_notification_error: {notification.get('error')}")
    if record.get("last_error"):
        lines.append(f"last_error: {record.get('last_error')}")
    return "\n".join(lines)


def dashboard_loop(args: argparse.Namespace) -> int:
    paths = manager_paths(args.workspace, args.state_dir)
    item_id = manager_id_value(args.manager_id)
    dashboard_file = Path(args.dashboard_file).expanduser().resolve() if getattr(args, "dashboard_file", None) else None
    while True:
        record, error = read_manager_record(paths, item_id)
        if error:
            print(f"manager state read error: {error}", flush=True)
            time.sleep(args.poll_seconds)
            continue
        if record is None:
            print(f"manager record not found: {manager_record_path(paths, item_id)}", flush=True)
            return 2
        record["manager_pid"] = os.getpid()
        if not record.get("manager_process_started_at"):
            record["manager_process_started_at"] = tmux_state.utc_now()
        record = manager_cycle(record, paths=paths)
        latest, _latest_error = read_manager_record(paths, item_id)
        merged = merge_external_manager_update(record, latest)
        if merged is not record:
            record = manager_cycle(merged, paths=paths)
        record = write_manager_record(paths, record)
        job_status = load_job_status(paths, str(record.get("current_job_id") or ""))
        text = dashboard_text(record, job_status)
        if dashboard_file:
            write_dashboard_file(dashboard_file, text)
            render_dashboard_to_pane(str(record.get("manager_pane_id") or ""), dashboard_file)
        else:
            print("\033[2J\033[H" + text, flush=True)
        if record.get("status") == "cancelled":
            return 0
        time.sleep(args.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Visible tmux manager dashboard")
    subparsers = parser.add_subparsers(dest="action", required=True)
    loop_parser = subparsers.add_parser("loop", help="Run the visible manager dashboard loop")
    loop_parser.add_argument("--manager-id", required=True)
    loop_parser.add_argument("--workspace")
    loop_parser.add_argument("--state-dir")
    loop_parser.add_argument("--poll-seconds", type=float, default=2.0)
    loop_parser.add_argument("--dashboard-file")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.action == "loop":
        try:
            raise SystemExit(dashboard_loop(args))
        except KeyboardInterrupt:
            paths = manager_paths(args.workspace, args.state_dir)
            record, _error = read_manager_record(paths, args.manager_id)
            if record is not None:
                record["status"] = "cancelled"
                write_manager_record(paths, record)
            raise SystemExit(130)
    parser.error(f"unknown command: {args.action}")


if __name__ == "__main__":
    main()
