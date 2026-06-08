#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_app_server_client
import tmux_state


CANONICAL_SUCCESS_SIGNALS = {"turn_started_notification", "turn_start_response", "both"}
MANUAL_CONFIRMATION = "confirmed_same_thread"
EXPECTED_OUTBOUND_SEQUENCE = ["initialize", "initialized", "thread/resume", "turn/start"]
ALLOWED_REQUEST_METHODS = {"initialize", "thread/resume", "turn/start"}
ALLOWED_NOTIFICATION_METHODS = {"initialized"}
BRIDGE_VERSION = 1
BRIDGE_OBSERVED_LIMIT = 500
BRIDGE_STATUSES = {"registered", "starting", "active", "failed", "cancelled"}
BRIDGE_TERMINAL_RANK = 1
BRIDGE_READY_RANK = 0
BRIDGE_TURN_COMPLETION_TIMEOUT_SECONDS = 20.0
BRIDGE_TERMINAL_TURN_COMPLETION_TIMEOUT_SECONDS = 45.0


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bridge_paths(workspace: str, state_dir: str | None = None) -> dict[str, Path]:
    paths = tmux_state.state_paths(workspace, state_dir)
    tmux_state.ensure_state_dirs(paths)
    return paths


def bridge_id_value(value: str) -> str:
    item_id = tmux_state.safe_id(value)
    if not item_id:
        raise ValueError("bridge id must be nonblank")
    return item_id


def default_bridge_id(thread_id: str) -> str:
    return f"bridge-{tmux_state.safe_id(thread_id)}"


def bridge_record_path(paths: dict[str, Path], bridge_id: str) -> Path:
    return paths["bridge"] / f"{bridge_id_value(bridge_id)}.json"


def bridge_lock_path(paths: dict[str, Path], bridge_id: str) -> Path:
    return paths["bridge"] / f"{bridge_id_value(bridge_id)}.lock"


@contextlib.contextmanager
def bridge_record_lock(paths: dict[str, Path], bridge_id: str) -> Any:
    lock_path = bridge_lock_path(paths, bridge_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_bridge_record(paths: dict[str, Path], bridge_id: str) -> dict[str, Any] | None:
    path = bridge_record_path(paths, bridge_id)
    data, error = tmux_state.read_json(path)
    if error:
        raise ValueError(f"could not read bridge record {path}: {error}")
    if not data:
        return None
    return normalize_bridge_record(data, path)


def write_bridge_record(paths: dict[str, Path], record: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_bridge_record(record)
    normalized["updated_at"] = tmux_state.utc_now()
    path = bridge_record_path(paths, normalized["bridge_id"])
    stored = dict(normalized)
    stored.pop("bridge_path", None)
    tmux_state.atomic_write_json(path, stored)
    normalized["bridge_path"] = str(path)
    return normalized


def normalize_bridge_record(record: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    bridge_id = bridge_id_value(str(path.stem if path else record.get("bridge_id") or record.get("id") or "bridge"))
    normalized = dict(record)
    normalized["version"] = int(normalized.get("version") or BRIDGE_VERSION)
    normalized["bridge_id"] = bridge_id
    normalized["id"] = bridge_id
    normalized["status"] = tmux_state.token_text(normalized.get("status")) or "registered"
    if normalized["status"] not in BRIDGE_STATUSES:
        normalized["status"] = "failed"
    normalized.setdefault("workspace", None)
    normalized.setdefault("state_dir", None)
    normalized.setdefault("thread_id", "")
    normalized.setdefault("endpoint", "")
    normalized.setdefault("socket_path", None)
    try:
        normalized["pid"] = int(normalized.get("pid") or 0)
    except (TypeError, ValueError):
        normalized["pid"] = 0
    normalized.setdefault("created_at", normalized.get("updated_at") or tmux_state.utc_now())
    normalized.setdefault("updated_at", normalized["created_at"])
    normalized.setdefault("heartbeat_at", None)
    normalized.setdefault("last_wake_at", None)
    normalized.setdefault("last_error", None)
    normalized["poll_interval_seconds"] = float(normalized.get("poll_interval_seconds") or 2.0)
    normalized["quiet_seconds"] = float(normalized.get("quiet_seconds") or 10.0)
    observed = normalized.get("observed_event_ids")
    normalized["observed_event_ids"] = [str(item) for item in observed] if isinstance(observed, list) else []
    normalized.setdefault("last_delivery", None)
    normalized.setdefault("pending_delivery", None)
    normalized.setdefault("observed_event_cutoff", None)
    if path:
        normalized["bridge_path"] = str(path)
    else:
        normalized.setdefault("bridge_path", None)
    return normalized


def build_bridge_record(
    *,
    bridge_id: str,
    thread_id: str,
    endpoint: str,
    workspace: str,
    state_dir: str,
    poll_seconds: float,
    quiet_seconds: float,
) -> dict[str, Any]:
    if not tmux_state.one_line_text(thread_id):
        raise ValueError("bridge register requires nonblank --thread-id")
    endpoint_info = codex_app_server_client.parse_endpoint(endpoint)
    now = tmux_state.utc_now()
    return normalize_bridge_record(
        {
            "version": BRIDGE_VERSION,
            "bridge_id": bridge_id_value(bridge_id),
            "status": "registered",
            "workspace": workspace,
            "state_dir": state_dir,
            "thread_id": thread_id,
            "endpoint": endpoint,
            "socket_path": endpoint_info.socket_path,
            "pid": 0,
            "created_at": now,
            "updated_at": now,
            "heartbeat_at": None,
            "last_wake_at": None,
            "last_error": None,
            "poll_interval_seconds": poll_seconds,
            "quiet_seconds": quiet_seconds,
            "observed_event_ids": [],
            "last_delivery": None,
            "pending_delivery": None,
            "observed_event_cutoff": None,
        }
    )


def process_command_line(pid: int) -> str | None:
    if pid <= 0:
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def command_matches_bridge(record: dict[str, Any], command: str | None) -> bool:
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not any(part.endswith("tmux_bridge.py") for part in parts):
        return False
    if "daemon" not in parts:
        return False
    try:
        bridge_index = parts.index("--bridge-id")
        workspace_index = parts.index("--workspace")
    except ValueError:
        return False
    return (
        bridge_index + 1 < len(parts)
        and workspace_index + 1 < len(parts)
        and parts[bridge_index + 1] == str(record.get("bridge_id"))
        and Path(parts[workspace_index + 1]).expanduser().resolve() == Path(str(record.get("workspace"))).expanduser().resolve()
    )


def bridge_process_state(record: dict[str, Any]) -> dict[str, Any]:
    pid = int(record.get("pid") or 0)
    command = process_command_line(pid)
    running = bool(command)
    matches = bool(running and command_matches_bridge(record, command))
    state = "verified_daemon" if matches else "dead_pid"
    if running and not matches:
        state = "foreign_pid"
    if not pid:
        state = "missing_pid"
    return {
        "pid": pid,
        "pid_running": running,
        "pid_matches": matches,
        "process_state": state,
        "process_command": command,
    }


def register_bridge(
    *,
    thread_id: str,
    endpoint: str,
    workspace: str,
    state_dir: str | None = None,
    bridge_id: str | None = None,
    poll_seconds: float = 2.0,
    quiet_seconds: float = 10.0,
    replace: bool = False,
) -> dict[str, Any]:
    if not tmux_state.one_line_text(thread_id):
        raise ValueError("bridge register requires nonblank --thread-id")
    codex_app_server_client.parse_endpoint(endpoint)
    paths = bridge_paths(workspace, state_dir)
    item_id = bridge_id_value(bridge_id or default_bridge_id(thread_id))
    with bridge_record_lock(paths, item_id):
        existing = read_bridge_record(paths, item_id)
        if existing:
            process = bridge_process_state(existing)
            if process["pid_matches"]:
                return {"bridge_id": item_id, "registered": False, "reason": "bridge daemon is active", "existing": existing | process}
            if not replace:
                return {"bridge_id": item_id, "registered": False, "reason": "bridge record already exists", "existing": existing | process}
            if str(existing.get("status")) not in {"failed", "cancelled", "registered"} and process["pid_running"]:
                return {"bridge_id": item_id, "registered": False, "reason": "existing pid is running and cannot be replaced", "existing": existing | process}
        record = build_bridge_record(
            bridge_id=item_id,
            thread_id=thread_id,
            endpoint=endpoint,
            workspace=str(paths["workspace"]),
            state_dir=str(paths["root"]),
            poll_seconds=poll_seconds,
            quiet_seconds=quiet_seconds,
        )
        record = write_bridge_record(paths, record)
    return {
        "bridge_id": item_id,
        "registered": True,
        "record": record,
        "bridge_path": record["bridge_path"],
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }


def event_sort_key(timestamp: str, source_rank: int, event_id: str) -> list[Any]:
    return [timestamp, source_rank, event_id]


def sort_key_leq(left: Any, right: Any) -> bool:
    if not isinstance(left, list) or not isinstance(right, list):
        return False
    return left <= right


def normalized_event_timestamp(*values: Any) -> str | None:
    for value in values:
        parsed = tmux_state.parse_time(value)
        if parsed:
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def candidate_is_observed(record: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if candidate["event_id"] in set(record.get("observed_event_ids") or []):
        return True
    cutoff = record.get("observed_event_cutoff")
    return bool(cutoff and sort_key_leq(candidate.get("event_sort_key"), cutoff))


def terminal_status_candidate(status: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    event_id = str(status.get("event_id") or "")
    if not event_id:
        return None, f"terminal status missing event_id: {status.get('status_path')}"
    timestamp = normalized_event_timestamp(status.get("ended_at"), status.get("updated_at"))
    if not timestamp:
        return None, f"terminal status missing event timestamp: {status.get('status_path')}"
    job_id = str(status.get("id") or status.get("job_id") or "unknown")
    status_path = status.get("status_path")
    log_path = status.get("log_path")
    return (
        {
            "source": "terminal",
            "event_id": event_id,
            "event_sort_key": event_sort_key(timestamp, BRIDGE_TERMINAL_RANK, event_id),
            "event_timestamp": timestamp,
            "job_id": job_id,
            "status_path": str(status_path) if status_path else None,
            "task_path": None,
            "log_path": str(log_path) if log_path else None,
            "job_path": None,
        },
        None,
    )


def ready_task_candidate(task: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    task_id = str(task.get("task_id") or "")
    if not task_id:
        return None, "ready task missing task_id"
    matched = task.get("matched_status") if isinstance(task.get("matched_status"), dict) else None
    matched_event_id = str((matched or {}).get("event_id") or task.get("updated_at") or "")
    event_id = f"ready-task:{task_id}:{matched_event_id}"
    timestamp = normalized_event_timestamp(
        (matched or {}).get("ended_at"),
        (matched or {}).get("updated_at"),
        task.get("updated_at"),
    )
    if not timestamp:
        return None, f"ready task missing event timestamp: {task.get('task_path')}"
    evidence = [str(path) for path in task.get("evidence_paths") or [] if path]
    status_path = str((matched or {}).get("status_path") or "") or next((path for path in evidence if "/status/" in path), None)
    log_path = str((matched or {}).get("log_path") or "") or next((path for path in evidence if "/logs/" in path), None)
    return (
        {
            "source": "ready_task",
            "event_id": event_id,
            "event_sort_key": event_sort_key(timestamp, BRIDGE_READY_RANK, event_id),
            "event_timestamp": timestamp,
            "job_id": str((matched or {}).get("id") or (matched or {}).get("job_id") or task.get("after_job_id") or "unknown"),
            "status_path": status_path,
            "task_path": str(task.get("task_path") or ""),
            "log_path": log_path,
            "job_path": None,
        },
        None,
    )


def detect_bridge_candidates(paths: dict[str, Path], record: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    last_error: str | None = None
    candidates: list[dict[str, Any]] = []
    statuses, status_errors = tmux_state.load_statuses_normalized(paths["root"])
    for error in status_errors:
        last_error = f"{error.get('path')}: {error.get('error')}"
    for status in statuses:
        if not tmux_state.is_terminal(status):
            continue
        candidate, error = terminal_status_candidate(status)
        if error:
            last_error = error
            continue
        if candidate and not candidate_is_observed(record, candidate):
            candidates.append(candidate)

    task_state = tmux_state.load_task_state(paths)
    for error in task_state.get("errors", []):
        last_error = f"{error.get('path')}: {error.get('error')}"
    ready_tasks = tmux_state.classify_task_state(task_state, max_items=500)["ready_tasks"]
    for task in ready_tasks:
        candidate, error = ready_task_candidate(task)
        if error:
            last_error = error
            continue
        if candidate and not candidate_is_observed(record, candidate):
            candidates.append(candidate)
    return candidates, last_error


def select_bridge_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    ready = [candidate for candidate in candidates if candidate.get("source") == "ready_task"]
    if ready:
        return sorted(ready, key=lambda item: item["event_sort_key"])[0]
    terminal = [candidate for candidate in candidates if candidate.get("source") == "terminal"]
    if terminal:
        return sorted(terminal, key=lambda item: item["event_sort_key"], reverse=True)[0]
    return None


def build_wake_prompt(workspace: str, candidate: dict[str, Any]) -> str:
    first_line = (
        "tmux-manager observed a ready task."
        if candidate.get("source") == "ready_task"
        else "tmux-manager observed a terminal event."
    )
    return "\n".join(
        [
            first_line,
            "",
            f"Workspace: {workspace}",
            f"Job ID: {candidate.get('job_id') or 'unknown'}",
            f"Job path: {candidate.get('job_path') or 'none'}",
            f"Status path: {candidate.get('status_path') or 'none'}",
            f"Task path: {candidate.get('task_path') or 'none'}",
            f"Log path: {candidate.get('log_path') or 'none'}",
            "",
            "Please use the tmux-manager MCP to inspect manager status or request a bounded observe grant, then continue the requested work.",
        ]
    )


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def should_throttle(record: dict[str, Any], candidate: dict[str, Any]) -> bool:
    quiet_seconds = float(record.get("quiet_seconds") or 0)
    if quiet_seconds <= 0:
        return False
    pending = record.get("pending_delivery") if isinstance(record.get("pending_delivery"), dict) else None
    if pending and pending.get("event_id") == candidate.get("event_id"):
        age = tmux_state.age_seconds(pending.get("attempted_at"))
        return age is not None and age < quiet_seconds
    age = tmux_state.age_seconds(record.get("last_wake_at"))
    return age is not None and age < quiet_seconds


def deliver_bridge_candidate(record: dict[str, Any], candidate: dict[str, Any], prompt: str) -> dict[str, Any]:
    client = codex_app_server_client.AppServerClient(str(record["endpoint"]))
    resume_error: str | None = None
    resume_thread_id: str | None = None
    turn_response: dict[str, Any] | None = None
    try:
        client.connect()
        client.initialize()
        try:
            resume_response = client.resume_thread(str(record["thread_id"]))
            resume_thread_id = codex_app_server_client.response_thread_id(resume_response)
        except codex_app_server_client.AppServerClientError as exc:
            if "no rollout found" not in str(exc).lower():
                raise
            resume_error = str(exc)
        turn_response = client.start_turn(str(record["thread_id"]), prompt, str(record["workspace"]))
        turn_id = codex_app_server_client.response_turn_id(turn_response)
        wait_seconds = (
            BRIDGE_TERMINAL_TURN_COMPLETION_TIMEOUT_SECONDS
            if candidate.get("source") == "manager_terminal"
            else BRIDGE_TURN_COMPLETION_TIMEOUT_SECONDS
        )
        turn_completion = client.wait_for_turn_completed(turn_id, wait_seconds)
    finally:
        client.close()
    turn_thread_id = codex_app_server_client.response_thread_id(turn_response, str(record["thread_id"]))
    if turn_thread_id != record["thread_id"]:
        raise codex_app_server_client.PermanentAppServerError(
            f"turn/start returned mismatched thread id: requested {record['thread_id']!r}, got {turn_thread_id!r}"
        )
    return {
        "event_id": candidate["event_id"],
        "delivered_at": tmux_state.utc_now(),
        "prompt_sha256": prompt_sha256(prompt),
        "response_id": codex_app_server_client.response_id(turn_response),
        "turn_id": codex_app_server_client.response_turn_id(turn_response),
        "turn_completion": turn_completion,
        "resume_thread_id": resume_thread_id,
        "resume_error": resume_error,
    }


def append_observed_event(record: dict[str, Any], candidate: dict[str, Any]) -> None:
    observed = [event_id for event_id in record.get("observed_event_ids") or [] if event_id != candidate["event_id"]]
    observed.append(candidate["event_id"])
    if len(observed) > BRIDGE_OBSERVED_LIMIT:
        evicted_count = len(observed) - BRIDGE_OBSERVED_LIMIT
        observed = observed[evicted_count:]
        cutoff = record.get("observed_event_cutoff")
        if cutoff is None or sort_key_leq(cutoff, candidate["event_sort_key"]):
            record["observed_event_cutoff"] = candidate["event_sort_key"]
    record["observed_event_ids"] = observed


def bridge_daemon_cycle(record: dict[str, Any]) -> dict[str, Any]:
    paths = bridge_paths(str(record["workspace"]), str(record["state_dir"]) if record.get("state_dir") else None)
    candidates, detection_error = detect_bridge_candidates(paths, record)
    candidate = select_bridge_candidate(candidates)
    updated = dict(record)
    updated.update({"status": "active", "pid": os.getpid(), "heartbeat_at": tmux_state.utc_now()})
    if detection_error:
        updated["last_error"] = detection_error
    if not candidate:
        return updated
    prompt = build_wake_prompt(str(record["workspace"]), candidate)
    if should_throttle(record, candidate):
        return updated
    sha = prompt_sha256(prompt)
    attempted_at = tmux_state.utc_now()
    try:
        delivery = deliver_bridge_candidate(record, candidate, prompt)
    except codex_app_server_client.AppServerClientError as exc:
        updated["last_error"] = str(exc)
        updated["pending_delivery"] = {
            "event_id": candidate["event_id"],
            "attempted_at": attempted_at,
            "prompt_sha256": sha,
            "failure_class": exc.failure_class,
            "error": str(exc),
        }
        if exc.failure_class == "permanent_failure":
            updated["status"] = "failed"
            updated["pending_delivery"] = None
        return updated
    updated["last_error"] = None
    updated["pending_delivery"] = None
    updated["last_delivery"] = delivery
    updated["last_wake_at"] = delivery["delivered_at"]
    append_observed_event(updated, candidate)
    return updated


DAEMON_UPDATE_FIELDS = {
    "status",
    "pid",
    "heartbeat_at",
    "updated_at",
    "last_error",
    "pending_delivery",
    "last_delivery",
    "last_wake_at",
    "observed_event_ids",
    "observed_event_cutoff",
}


def merge_daemon_update(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key in DAEMON_UPDATE_FIELDS:
        if key in update:
            merged[key] = update[key]
    return merged


def run_daemon(*, bridge_id: str, workspace: str, state_dir: str | None = None, max_cycles: int | None = None) -> dict[str, Any]:
    paths = bridge_paths(workspace, state_dir)
    item_id = bridge_id_value(bridge_id)
    cycles = 0
    while True:
        with bridge_record_lock(paths, item_id):
            record = read_bridge_record(paths, item_id)
            if not record:
                raise ValueError(f"bridge record does not exist: {item_id}")
            if record.get("status") == "cancelled":
                return {"bridge_id": item_id, "stopped": True, "reason": "cancelled"}
            heartbeat = dict(record)
            heartbeat.update({"status": "active", "pid": os.getpid(), "heartbeat_at": tmux_state.utc_now()})
            record = write_bridge_record(paths, heartbeat)

        updated = bridge_daemon_cycle(record)

        with bridge_record_lock(paths, item_id):
            current = read_bridge_record(paths, item_id)
            if not current:
                raise ValueError(f"bridge record does not exist: {item_id}")
            if current.get("status") == "cancelled":
                return {"bridge_id": item_id, "stopped": True, "reason": "cancelled"}
            updated = write_bridge_record(paths, merge_daemon_update(current, updated))
            if updated.get("status") == "failed":
                return {"bridge_id": item_id, "stopped": True, "reason": "failed", "record": updated}
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return {"bridge_id": item_id, "stopped": False, "cycles": cycles, "record": updated}
        time.sleep(float(updated.get("poll_interval_seconds") or 2.0))


def start_bridge(
    *,
    bridge_id: str,
    workspace: str,
    state_dir: str | None = None,
    foreground: bool = True,
    replace: bool = False,
) -> dict[str, Any]:
    paths = bridge_paths(workspace, state_dir)
    item_id = bridge_id_value(bridge_id)
    with bridge_record_lock(paths, item_id):
        record = read_bridge_record(paths, item_id)
        if not record:
            raise ValueError(f"bridge record does not exist: {item_id}")
        process = bridge_process_state(record)
        if process["pid_matches"]:
            return {"bridge_id": item_id, "started": False, "duplicate": True, "reason": "bridge daemon is already active", "record": record | process}
        if record["status"] != "registered" and not replace:
            return {"bridge_id": item_id, "started": False, "reason": "bridge is not registered; use --replace for failed/cancelled/stale records", "record": record | process}
        if process["pid_running"] and not process["pid_matches"]:
            record["last_error"] = "stored pid is running but does not match tmux_bridge.py daemon"
        record["status"] = "starting"
        record["pid"] = os.getpid() if foreground else 0
        record = write_bridge_record(paths, record)
    if foreground:
        return run_daemon(bridge_id=item_id, workspace=workspace, state_dir=state_dir)

    log_path = paths["bridge"] / f"{item_id}.daemon.log"
    script_path = Path(__file__).resolve()
    argv = [sys.executable, str(script_path), "daemon", "--bridge-id", item_id, "--workspace", str(paths["workspace"])]
    if state_dir:
        argv.extend(["--state-dir", str(paths["root"])])
    with log_path.open("ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    with bridge_record_lock(paths, item_id):
        record = read_bridge_record(paths, item_id)
        if not record or record.get("status") == "cancelled":
            return {"bridge_id": item_id, "started": False, "reason": "bridge was cancelled during start"}
        record["pid"] = proc.pid
        record["status"] = "starting"
        record["last_error"] = None
        record = write_bridge_record(paths, record)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        current = read_bridge_record(paths, item_id)
        if current and current.get("status") == "active" and current.get("heartbeat_at"):
            return {"bridge_id": item_id, "started": True, "pid": proc.pid, "record": current, "daemon_log_path": str(log_path)}
        time.sleep(0.1)
    with bridge_record_lock(paths, item_id):
        current = read_bridge_record(paths, item_id) or {}
        current["status"] = "failed"
        current["last_error"] = "bridge daemon did not write an active heartbeat before startup timeout"
        current["pid"] = proc.pid
        current = write_bridge_record(paths, current)
    return {"bridge_id": item_id, "started": False, "pid": proc.pid, "record": current, "daemon_log_path": str(log_path)}


def bridge_status(*, bridge_id: str, workspace: str, state_dir: str | None = None) -> dict[str, Any]:
    paths = bridge_paths(workspace, state_dir)
    record = read_bridge_record(paths, bridge_id)
    if not record:
        raise ValueError(f"bridge record does not exist: {bridge_id}")
    return {"bridge_id": record["bridge_id"], "record": record | bridge_process_state(record)}


def cancel_bridge(*, bridge_id: str, workspace: str, state_dir: str | None = None) -> dict[str, Any]:
    paths = bridge_paths(workspace, state_dir)
    item_id = bridge_id_value(bridge_id)
    signal_sent = False
    process: dict[str, Any]
    with bridge_record_lock(paths, item_id):
        record = read_bridge_record(paths, item_id)
        if not record:
            raise ValueError(f"bridge record does not exist: {item_id}")
        process = bridge_process_state(record)
        record["status"] = "cancelled"
        record["last_error"] = None
        record = write_bridge_record(paths, record)
    if process["pid_matches"]:
        try:
            os.kill(int(process["pid"]), signal.SIGTERM)
            signal_sent = True
        except ProcessLookupError:
            signal_sent = False
    return {"bridge_id": item_id, "cancelled": True, "signal_sent": signal_sent, "record": record | process}


def poc_artifact_paths(
    workspace: str,
    timestamp: str,
    state_dir: str | None = None,
    fixture_root: Path | None = None,
) -> dict[str, Path]:
    paths = bridge_paths(workspace, state_dir)
    fixture_dir = fixture_root or repo_root() / "tests" / "fixtures" / "app_server_unix_ws"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    return {
        "runtime": paths["bridge"] / f"poc-{timestamp}.json",
        "fixture": fixture_dir / f"poc-{timestamp}.json",
        "manual": paths["bridge"] / f"poc-{timestamp}.manual.md",
    }


def scrub_outbound_for_fixture(outbound: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scrubbed: list[dict[str, Any]] = []
    for message in outbound:
        cloned = json.loads(json.dumps(message))
        if "id" in cloned:
            cloned["id"] = "<request-id>"
        scrubbed.append(cloned)
    return scrubbed


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmux_state.atomic_write_json(path, payload)


def write_manual_note(path: Path, *, thread_id: str, prompt: str) -> None:
    first_line = prompt.splitlines()[0] if prompt.splitlines() else ""
    if path.exists():
        return
    path.write_text(
        "\n".join(
            [
                "# tmux-manager bridge PoC manual confirmation",
                "",
                f"main_cli_thread_id: {thread_id}",
                "received_prompt_timestamp: ",
                f"received_prompt_first_line: {first_line}",
                "operator_confirmation: ",
                "",
            ]
        ),
        encoding="utf-8",
    )


def protocol_fixture_payload(
    client: codex_app_server_client.AppServerClient,
    *,
    canonical_success_signal: str,
) -> dict[str, Any]:
    return {
        "requests": scrub_outbound_for_fixture(client.transcript["outbound"]),
        "responses": client.transcript["responses"],
        "notifications": client.transcript["notifications"],
        "canonical_success_signal": canonical_success_signal,
        "protocol_evidence": {
            "command": "codex app-server generate-ts --experimental",
            "observed_at": utc_timestamp(),
            "initialize_params": {
                "clientInfo": {
                    "name": "tmux-manager-bridge",
                    "title": codex_app_server_client.CLIENT_TITLE,
                    "version": codex_app_server_client.CLIENT_VERSION,
                },
                "capabilities": codex_app_server_client.INITIALIZE_CAPABILITIES,
            },
            "turn_start_text_input": {
                "type": "text",
                "text": "<wake prompt>",
                "text_elements": [],
            },
        },
    }


def run_poc(
    *,
    thread_id: str,
    endpoint: str,
    workspace: str,
    prompt: str,
    state_dir: str | None = None,
    codex_bin: str = "codex",
    fixture_root: Path | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    if not thread_id.strip():
        raise codex_app_server_client.PermanentAppServerError("thread id must be nonblank")
    codex_app_server_client.parse_endpoint(endpoint)
    stamp = timestamp or local_timestamp_for_filename()
    artifacts = poc_artifact_paths(workspace, stamp, state_dir, fixture_root)
    client = codex_app_server_client.AppServerClient(endpoint=endpoint, codex_bin=codex_bin)
    resume_response: dict[str, Any] | None = None
    resume_error: str | None = None
    turn_response: dict[str, Any] | None = None
    failure: dict[str, str] | None = None
    try:
        client.connect()
        client.initialize()
        try:
            resume_response = client.resume_thread(thread_id)
        except codex_app_server_client.AppServerClientError as exc:
            if "no rollout found" not in str(exc).lower():
                raise
            resume_error = str(exc)
        turn_response = client.start_turn(thread_id, prompt, workspace)
    except codex_app_server_client.AppServerClientError as exc:
        failure = {
            "failure_class": exc.failure_class,
            "error": str(exc),
        }
    finally:
        client.close()

    resume_thread_id = (
        codex_app_server_client.response_thread_id(resume_response, None) if resume_response is not None else None
    )
    turn_start_thread_id = (
        codex_app_server_client.response_thread_id(turn_response, thread_id) if turn_response is not None else None
    )
    turn_id = codex_app_server_client.response_turn_id(turn_response) if turn_response is not None else None
    response_id = codex_app_server_client.response_id(turn_response) if turn_response is not None else ""
    provisional_delivery = failure is None and turn_start_thread_id == thread_id and turn_response is not None

    fixture = protocol_fixture_payload(client, canonical_success_signal="turn_start_response")
    write_json(artifacts["fixture"], fixture)
    write_manual_note(artifacts["manual"], thread_id=thread_id, prompt=prompt)

    runtime: dict[str, Any] = {
        "endpoint": endpoint,
        "supplied_thread_id": thread_id,
        "resume_thread_id": resume_thread_id,
        "resume_error": resume_error,
        "turn_start_thread_id": turn_start_thread_id,
        "delivered": False,
        "provisional_delivery": provisional_delivery,
        "response_id": response_id,
        "turn_id": turn_id,
        "request_sequence": [message.get("method") for message in client.transcript["outbound"]],
        "protocol_fixture_path": str(artifacts["fixture"]),
        "manual_confirmation_note_path": str(artifacts["manual"]),
        "created_at": utc_timestamp(),
    }
    if failure is not None:
        runtime.update(failure)
    write_json(artifacts["runtime"], runtime)
    return runtime | {"runtime_path": str(artifacts["runtime"])}


def read_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def parse_manual_note(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def fixture_has_success_signal(fixture: dict[str, Any]) -> bool:
    signal = fixture.get("canonical_success_signal")
    if signal not in CANONICAL_SUCCESS_SIGNALS:
        return False
    if signal in {"turn_start_response", "both"} and not fixture.get("responses"):
        return False
    if signal in {"turn_started_notification", "both"} and not fixture.get("notifications"):
        return False
    return True


def validate_protocol_fixture_shape(fixture: dict[str, Any], runtime: dict[str, Any]) -> None:
    for key in ("requests", "responses", "notifications"):
        if not isinstance(fixture.get(key), list):
            raise ValueError(f"protocol fixture field must be a list: {key}")
    evidence = fixture.get("protocol_evidence")
    if not isinstance(evidence, dict):
        raise ValueError("protocol fixture missing required field: protocol_evidence")
    for key in ("command", "observed_at", "initialize_params", "turn_start_text_input"):
        if key not in evidence:
            raise ValueError(f"protocol_evidence missing required field: {key}")

    methods = [message.get("method") for message in fixture["requests"] if isinstance(message, dict)]
    if methods != EXPECTED_OUTBOUND_SEQUENCE:
        raise ValueError(f"protocol fixture outbound sequence must be {EXPECTED_OUTBOUND_SEQUENCE}, got {methods}")
    if runtime.get("request_sequence") != EXPECTED_OUTBOUND_SEQUENCE:
        raise ValueError("runtime request_sequence does not match the required PoC outbound sequence")

    initialize = fixture["requests"][0]
    initialized = fixture["requests"][1]
    turn_start = fixture["requests"][3]
    if initialize.get("id") is None:
        raise ValueError("initialize fixture request must include a scrubbed request id")
    for message in fixture["requests"]:
        if not isinstance(message, dict):
            raise ValueError("protocol fixture requests must contain JSON objects")
        if "jsonrpc" in message:
            raise ValueError("protocol fixture requests must omit jsonrpc on the wire")
        method = message.get("method")
        if "id" in message:
            if method not in ALLOWED_REQUEST_METHODS:
                raise ValueError(f"forbidden app-server request method in fixture: {method}")
        elif method not in ALLOWED_NOTIFICATION_METHODS:
            raise ValueError(f"forbidden app-server notification method in fixture: {method}")
    if initialized.get("method") != "initialized" or "id" in initialized:
        raise ValueError("initialized must be recorded as a JSON-RPC notification")

    init_params = initialize.get("params")
    if not isinstance(init_params, dict):
        raise ValueError("initialize params must be an object")
    client_info = init_params.get("clientInfo")
    capabilities = init_params.get("capabilities")
    if not isinstance(client_info, dict):
        raise ValueError("initialize clientInfo must be an object")
    for key in ("name", "title", "version"):
        if not client_info.get(key):
            raise ValueError(f"initialize clientInfo.{key} must be present")
    if client_info["name"] != "tmux-manager-bridge":
        raise ValueError("initialize clientInfo.name must be tmux-manager-bridge")
    if not isinstance(capabilities, dict):
        raise ValueError("initialize capabilities must be an object")
    for key in ("experimentalApi", "requestAttestation"):
        if capabilities.get(key) is not False:
            raise ValueError(f"initialize capabilities.{key} must be false")
    if capabilities.get("optOutNotificationMethods") != []:
        raise ValueError("initialize capabilities.optOutNotificationMethods must be []")

    turn_params = turn_start.get("params")
    if not isinstance(turn_params, dict):
        raise ValueError("turn/start params must be an object")
    text_items = turn_params.get("input")
    if not isinstance(text_items, list) or len(text_items) != 1:
        raise ValueError("turn/start must include exactly one text input item")
    text_item = text_items[0]
    if not isinstance(text_item, dict):
        raise ValueError("turn/start text input item must be an object")
    if text_item.get("type") != "text" or not isinstance(text_item.get("text"), str):
        raise ValueError("turn/start input must be a text item")
    if text_item.get("text_elements") != []:
        raise ValueError("turn/start text input must include text_elements: []")
    if turn_params.get("threadId") != runtime.get("supplied_thread_id"):
        raise ValueError("turn/start threadId must match supplied_thread_id")


def validate_poc_artifacts(runtime_json: Path) -> dict[str, Any]:
    runtime = read_json_object(runtime_json)
    required = {
        "endpoint",
        "supplied_thread_id",
        "resume_thread_id",
        "resume_error",
        "turn_start_thread_id",
        "delivered",
        "response_id",
        "turn_id",
        "request_sequence",
        "protocol_fixture_path",
        "manual_confirmation_note_path",
        "created_at",
    }
    missing = sorted(key for key in required if key not in runtime)
    if missing:
        raise ValueError(f"runtime evidence missing required fields: {', '.join(missing)}")
    fixture_path = Path(str(runtime["protocol_fixture_path"]))
    manual_path = Path(str(runtime["manual_confirmation_note_path"]))
    if not fixture_path.exists():
        raise ValueError(f"protocol fixture does not exist: {fixture_path}")
    if not manual_path.exists():
        raise ValueError(f"manual confirmation note does not exist: {manual_path}")
    fixture = read_json_object(fixture_path)
    for key in ("requests", "responses", "notifications", "canonical_success_signal", "protocol_evidence"):
        if key not in fixture:
            raise ValueError(f"protocol fixture missing required field: {key}")
    validate_protocol_fixture_shape(fixture, runtime)
    if not fixture_has_success_signal(fixture):
        raise ValueError("protocol fixture lacks the canonical success signal")
    manual = parse_manual_note(manual_path)
    for key in (
        "main_cli_thread_id",
        "received_prompt_timestamp",
        "received_prompt_first_line",
        "operator_confirmation",
    ):
        if not manual.get(key):
            raise ValueError(f"manual confirmation note missing required field: {key}")
    supplied = runtime["supplied_thread_id"]
    ids = {
        "supplied_thread_id": supplied,
        "turn_start_thread_id": runtime["turn_start_thread_id"],
        "main_cli_thread_id": manual["main_cli_thread_id"],
    }
    if runtime["resume_thread_id"] is not None:
        ids["resume_thread_id"] = runtime["resume_thread_id"]
    elif not runtime.get("resume_error"):
        raise ValueError("runtime evidence must include resume_error when resume_thread_id is null")
    if len(set(ids.values())) != 1:
        raise ValueError(f"thread id mismatch in PoC artifacts: {ids}")
    if manual["operator_confirmation"] != MANUAL_CONFIRMATION:
        raise ValueError("operator_confirmation must be exactly confirmed_same_thread")
    if not str(runtime["response_id"]):
        raise ValueError("runtime evidence response_id must be present")

    runtime["delivered"] = True
    runtime["validated_at"] = utc_timestamp()
    write_json(runtime_json, runtime)
    return {
        "valid": True,
        "runtime_json": str(runtime_json),
        "protocol_fixture_path": str(fixture_path),
        "manual_confirmation_note_path": str(manual_path),
        "prompt_sha256": hashlib.sha256(manual["received_prompt_first_line"].encode("utf-8")).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="tmux-manager bridge helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    poc = subparsers.add_parser("poc", help="run the app-server same-thread wake PoC")
    poc.add_argument("--thread-id", required=True)
    poc.add_argument("--endpoint", required=True)
    poc.add_argument("--workspace", required=True)
    poc.add_argument("--prompt", required=True)
    poc.add_argument("--state-dir")

    validate = subparsers.add_parser("validate-poc", help="validate PoC runtime, fixture, and manual artifacts")
    validate.add_argument("--runtime-json", required=True)

    daemon = subparsers.add_parser("daemon", help="run a registered bridge daemon")
    daemon.add_argument("--bridge-id", required=True)
    daemon.add_argument("--workspace", required=True)
    daemon.add_argument("--state-dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "poc":
            result = run_poc(
                thread_id=args.thread_id,
                endpoint=args.endpoint,
                workspace=args.workspace,
                prompt=args.prompt,
                state_dir=args.state_dir,
            )
        elif args.command == "validate-poc":
            result = validate_poc_artifacts(Path(args.runtime_json))
        elif args.command == "daemon":
            result = run_daemon(bridge_id=args.bridge_id, workspace=args.workspace, state_dir=args.state_dir)
        else:
            raise AssertionError(args.command)
    except (codex_app_server_client.AppServerClientError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
