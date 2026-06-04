#!/usr/bin/env python3
"""Shared state helpers for tmux-skills scripts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_VERSION = 1
TASK_VERSION = 2
OBJECTIVE_VERSION = 1
MANAGED_ACTIVE_STATUSES = {"starting", "running", "waiting", "waiting_status", "waiting_pane_idle"}
MANAGED_TERMINAL_STATUSES = {"submitted", "failed", "timeout", "cancelled", "stale"}
TERMINAL_STATUSES = {"succeeded", "matched", "stopped"} | MANAGED_TERMINAL_STATUSES
FAILED_TRIGGER_STATUSES = {"failed", "timeout", "stopped", "cancelled", "stale"}
RUNNING_STATUSES = {"pending", "running", "starting", "waiting", "waiting_status", "waiting_pane_idle"}
TASK_STATUSES = {"waiting", "ready", "in_progress", "done", "blocked", "cancelled"}
OBJECTIVE_STATUSES = {"active", "repairing", "succeeded", "blocked", "cancelled"}
TASK_OPEN_STATUSES = {"waiting", "ready", "in_progress", "blocked"}
TASK_TRIGGERS = {"succeeded", "failed", "terminal"}
TASK_TRANSIENT_FIELDS = {"effective_status", "matched_status", "stale", "task_path"}
MANAGED_TRANSIENT_FIELDS = {"pid_running", "pid_matches", "stale", "effective_status", "process_state"}
MANAGED_WORKER_ACTIONS = {"queue-after-idle", "queue-after-status", "watch"}
DEFAULT_STALE_SECONDS = 30 * 60
TASK_DISPLAY_TEXT_LIMIT = 800
DEFAULT_STATUS_LINES = 10
DEFAULT_STATUS_MAX_CHARS = 1200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_workspace(workspace: str | None = None) -> Path:
    return Path(workspace or os.getcwd()).expanduser().resolve()


def resolve_state_dir(workspace: str | Path, state_dir: str | None = None) -> Path:
    workspace_path = Path(workspace).expanduser().resolve()
    if state_dir is None:
        return workspace_path / ".codex" / "tmux-skills"

    path = Path(state_dir).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace_path / path).resolve()


def state_paths(workspace: str | None = None, state_dir: str | None = None) -> dict[str, Path]:
    workspace_path = resolve_workspace(workspace)
    root = resolve_state_dir(workspace_path, state_dir)
    return {
        "workspace": workspace_path,
        "root": root,
        "commands": root / "commands",
        "logs": root / "logs",
        "status": root / "status",
        "acks": root / "acks",
        "tasks": root / "tasks",
        "jobs": root / "jobs",
        "objectives": root / "objectives",
        "bridge": root / "bridge",
    }


def ensure_state_dirs(paths: dict[str, Path]) -> None:
    for key in ("commands", "logs", "status", "acks", "tasks", "jobs", "objectives", "bridge"):
        paths[key].mkdir(parents=True, exist_ok=True)


def safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "job"


def status_path(paths: dict[str, Path], item_id: str) -> Path:
    return paths["status"] / f"{safe_id(item_id)}.json"


def command_path(paths: dict[str, Path], item_id: str) -> Path:
    return paths["commands"] / f"{safe_id(item_id)}.sh"


def log_path(paths: dict[str, Path], item_id: str) -> Path:
    return paths["logs"] / f"{safe_id(item_id)}.log"


def ack_path(paths: dict[str, Path], event_id: str) -> Path:
    return paths["acks"] / f"{safe_id(event_id)}.json"


def task_path(paths: dict[str, Path], task_id: str) -> Path:
    return paths["tasks"] / f"{safe_id(task_id)}.json"


def job_path(paths: dict[str, Path], job_id: str) -> Path:
    return paths["jobs"] / f"{safe_id(job_id)}.json"


def objective_path(paths: dict[str, Path], objective_id: str) -> Path:
    return paths["objectives"] / f"{safe_id(objective_id)}.json"


def job_registry_lock_path(paths: dict[str, Path]) -> Path:
    return paths["jobs"] / ".registry.lock"


def read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, str(exc)

    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def command_preview(command_text: str, limit: int = 200) -> str:
    compact = " ".join(command_text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def one_line_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def token_text(value: Any) -> str:
    return one_line_text(value).lower()


def bounded_one_line_text(value: Any, *, limit: int = TASK_DISPLAY_TEXT_LIMIT, keep_tail: bool = False) -> str:
    text = one_line_text(value)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    if keep_tail:
        return "..." + text[-(limit - 3) :]
    return text[: limit - 3] + "..."


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def status_tail(text: str, *, lines: int = DEFAULT_STATUS_LINES, max_chars: int = DEFAULT_STATUS_MAX_CHARS) -> str:
    if lines <= 0:
        raise ValueError("status tail lines must be positive")
    if max_chars <= 0:
        raise ValueError("status tail max chars must be positive")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    tail = "\n".join(normalized.splitlines()[-lines:])
    if len(tail) <= max_chars:
        return tail
    return tail[-max_chars:]


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None or result.tzinfo.utcoffset(result) is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def path_mtime_utc(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    timestamp = parse_time(value)
    if not timestamp:
        return None
    current = now or datetime.now(timezone.utc)
    try:
        return (current - timestamp).total_seconds()
    except TypeError:
        return None


def terminal_event_id(status: dict[str, Any]) -> str:
    payload = {
        "id": status.get("id"),
        "attempt": status.get("attempt"),
        "status": status.get("status"),
        "exit_code": status.get("exit_code"),
        "ended_at": status.get("ended_at"),
        "updated_at": status.get("updated_at"),
        "last_output": status.get("last_output"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_status(
    *,
    kind: str,
    item_id: str,
    attempt: int,
    name: str | None,
    status: str,
    pane_id: str | None,
    command_preview_text: str | None,
    cwd: str | None,
    status_file: Path,
    log_file: Path | None,
    started_at: str | None = None,
    exit_code: int | None = None,
    last_output: str = "",
    ended_at: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    kind_value = token_text(kind) or "job"
    status_value = token_text(status) or "unknown"
    data: dict[str, Any] = {
        "version": STATE_VERSION,
        "kind": kind_value,
        "id": item_id,
        "attempt": attempt,
        "event_id": None,
        "name": name,
        "status": status_value,
        "exit_code": exit_code,
        "pane_id": pane_id,
        "command_preview": command_preview_text,
        "cwd": cwd,
        "started_at": started_at or now,
        "updated_at": now,
        "ended_at": ended_at,
        "status_path": str(status_file),
        "log_path": str(log_file) if log_file else None,
        "last_output": tail_text(last_output),
    }
    if status_value in TERMINAL_STATUSES:
        data["ended_at"] = ended_at or now
        data["event_id"] = terminal_event_id(data)
    return data


def write_status(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    explicit_ended_at = bool(data.get("ended_at"))
    updated = normalize_status(data, path)
    now = utc_now()
    updated["updated_at"] = now
    if updated.get("status") in TERMINAL_STATUSES:
        if not explicit_ended_at:
            updated["ended_at"] = now
        updated["event_id"] = terminal_event_id(updated)
    atomic_write_json(path, updated)
    return updated


def load_statuses(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    status_dir = root / "status"
    statuses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(status_dir.glob("*.json")):
        data, error = read_json(path)
        if error:
            errors.append({"path": str(path), "error": error})
            continue
        if data:
            statuses.append(data)
    return statuses, errors


def normalize_status(status: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    item_id = safe_id(str(path.stem if path else status.get("id") or status.get("job_id") or "unknown"))
    file_timestamp = path_mtime_utc(path)
    normalized = dict(status)
    normalized.setdefault("version", STATE_VERSION)
    normalized["id"] = item_id
    if "job_id" in normalized:
        normalized["job_id"] = item_id
    normalized.setdefault("kind", "job")
    normalized["kind"] = token_text(normalized.get("kind")) or "job"
    normalized.setdefault("attempt", 1)
    normalized.setdefault("event_id", None)
    if normalized.get("event_id") is not None:
        event_id = str(normalized["event_id"]).strip()
        normalized["event_id"] = event_id or None
    normalized.setdefault("name", None)
    normalized.setdefault("status", "unknown")
    normalized["status"] = token_text(normalized.get("status")) or "unknown"
    normalized.setdefault("exit_code", None)
    normalized.setdefault("pane_id", None)
    normalized.setdefault("command_preview", None)
    if normalized["command_preview"] is not None:
        normalized["command_preview"] = command_preview(str(normalized["command_preview"]))
    normalized.setdefault("cwd", None)
    normalized.setdefault("started_at", normalized.get("updated_at") or file_timestamp)
    normalized.setdefault("updated_at", normalized.get("ended_at") or normalized.get("started_at") or file_timestamp)
    normalized.setdefault("ended_at", None)
    if path:
        normalized["status_path"] = str(path)
    else:
        normalized.setdefault("status_path", None)
    normalized.setdefault("log_path", None)
    normalized.setdefault("last_output", "")
    normalized["last_output"] = tail_text(str(normalized["last_output"])) if normalized["last_output"] is not None else ""
    if is_terminal(normalized):
        normalized["ended_at"] = normalized.get("ended_at") or normalized.get("updated_at") or file_timestamp or utc_now()
        if not normalized.get("event_id"):
            normalized["event_id"] = terminal_event_id(normalized)
    else:
        normalized["ended_at"] = None
        normalized["event_id"] = None
    return normalized


def load_statuses_normalized(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    status_dir = root / "status"
    statuses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(status_dir.glob("*.json")):
        data, error = read_json(path)
        if error:
            errors.append({"path": str(path), "error": error})
            continue
        if data:
            statuses.append(normalize_status(data, path))
    return statuses, errors


def is_terminal(status: dict[str, Any]) -> bool:
    return token_text(status.get("status")) in TERMINAL_STATUSES


def is_active_managed_job(record: dict[str, Any]) -> bool:
    return token_text(record.get("status")) in MANAGED_ACTIVE_STATUSES


def managed_job_interval_seconds(record: dict[str, Any]) -> float:
    try:
        interval = float(record.get("check_interval_seconds") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    if not math.isfinite(interval) or interval < 0:
        return 0.0
    return interval


def managed_job_stale_threshold(record: dict[str, Any]) -> float:
    return max(3.0 * managed_job_interval_seconds(record), 300.0)


def managed_job_pid(record: dict[str, Any]) -> int | None:
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command_line(pid: int | None) -> str:
    if not pid:
        return ""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip()


def managed_worker_command_matches(command_line: str, job_id: str) -> bool:
    if not command_line or not job_id:
        return False
    try:
        parts = shlex.split(command_line)
    except ValueError:
        return False
    script_index = next((index for index, part in enumerate(parts) if Path(part).name == "tmux_queue.py"), None)
    if script_index is None:
        return False
    if script_index + 1 >= len(parts) or parts[script_index + 1] not in MANAGED_WORKER_ACTIONS:
        return False
    for index, part in enumerate(parts[script_index + 2 :], start=script_index + 2):
        if part == "--job-id" and index + 1 < len(parts):
            return parts[index + 1] == job_id
        if part.startswith("--job-id="):
            return part.split("=", 1)[1] == job_id
    return False


def managed_worker_pid_matches(record: dict[str, Any]) -> bool:
    pid = managed_job_pid(record)
    command_line = process_command_line(pid)
    return managed_worker_command_matches(command_line, str(record.get("job_id") or ""))


def managed_job_stale_reason(
    record: dict[str, Any],
    *,
    pid_running: bool | None = None,
    pid_matches: bool | None = None,
    now: datetime | None = None,
) -> str | None:
    if not is_active_managed_job(record):
        return None

    age = age_seconds(record.get("heartbeat_at") or record.get("updated_at"), now=now)
    if age is None:
        return "missing heartbeat"
    threshold = managed_job_stale_threshold(record)
    if age < threshold:
        return None

    if not record.get("pid"):
        return f"heartbeat older than {int(threshold)}s and no pid recorded"
    if pid_running is False:
        return f"heartbeat older than {int(threshold)}s and pid is not running"
    if pid_matches is False:
        return f"heartbeat older than {int(threshold)}s and pid is not a tmux-skills worker"
    return None


def managed_job_effective_status(
    record: dict[str, Any],
    *,
    pid_running: bool | None = None,
    pid_matches: bool | None = None,
    now: datetime | None = None,
) -> tuple[str, str, str | None]:
    stored_status = token_text(record.get("status")) or "unknown"
    if stored_status in MANAGED_TERMINAL_STATUSES:
        return stored_status, "terminal", record.get("stale_reason") if stored_status == "stale" else None
    if stored_status not in MANAGED_ACTIVE_STATUSES:
        return stored_status, "unknown", None

    pid = managed_job_pid(record)
    if not pid:
        age = age_seconds(record.get("heartbeat_at") or record.get("updated_at"), now=now)
        if stored_status == "starting" and (age is None or age < 10):
            return stored_status, "starting", None
        return "dead", "missing_pid", "active managed job has no recorded pid"

    if pid_running is False:
        return "dead", "dead_pid", "recorded pid is not running"
    if pid_running is True and pid_matches is False:
        return "orphaned", "foreign_pid", "recorded pid is running but is not this tmux-skills worker"
    return stored_status, "verified_worker", None


def is_verified_active_managed_job(record: dict[str, Any]) -> bool:
    return is_active_managed_job(record) and token_text(record.get("effective_status") or record.get("status")) in MANAGED_ACTIVE_STATUSES


def sort_key(status: dict[str, Any]) -> str:
    return str(status.get("ended_at") or status.get("updated_at") or status.get("started_at") or "")


def is_acked(paths: dict[str, Path], status: dict[str, Any]) -> bool:
    event_id = str(status.get("event_id") or "")
    return bool(event_id) and ack_path(paths, event_id).exists()


def ack_status(paths: dict[str, Path], status: dict[str, Any]) -> None:
    event_id = str(status.get("event_id") or "")
    if not event_id:
        return
    atomic_write_json(
        ack_path(paths, event_id),
        {
            "event_id": event_id,
            "id": status.get("id"),
            "attempt": status.get("attempt"),
            "status": status.get("status"),
            "exit_code": status.get("exit_code"),
            "acked_at": utc_now(),
        },
    )


def newest_unacked_terminal(paths: dict[str, Path]) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    statuses, errors = load_statuses_normalized(paths["root"])
    candidates = [status for status in statuses if is_terminal(status) and not is_acked(paths, status)]
    candidates.sort(key=sort_key, reverse=True)
    return (candidates[0] if candidates else None), errors


def build_objective(
    *,
    objective_id: str,
    goal: str,
    pane_id: str,
    cwd: str,
    command_snapshot: str,
    max_attempts: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    item_id = safe_id(objective_id)
    return {
        "version": OBJECTIVE_VERSION,
        "objective_id": item_id,
        "status": "active",
        "goal": goal,
        "pane_id": pane_id,
        "cwd": cwd,
        "command_snapshot": command_snapshot,
        "attempts": [],
        "current_attempt": None,
        "max_attempts": max_attempts,
        "policy": policy,
        "lease": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "blocked_reason": None,
    }


def normalize_objective(objective: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    item_id = safe_id(str(path.stem if path else objective.get("objective_id") or objective.get("id") or "objective"))
    normalized = dict(objective)
    try:
        version = int(normalized.get("version") or OBJECTIVE_VERSION)
    except (TypeError, ValueError):
        version = OBJECTIVE_VERSION
    normalized["version"] = version
    normalized["objective_id"] = item_id
    normalized["id"] = item_id
    normalized["status"] = token_text(normalized.get("status")) or "active"
    if normalized["status"] not in OBJECTIVE_STATUSES:
        normalized["status"] = "active"
    normalized.setdefault("goal", "")
    normalized.setdefault("pane_id", None)
    normalized.setdefault("cwd", None)
    normalized.setdefault("command_snapshot", "")
    attempts = normalized.get("attempts")
    normalized["attempts"] = attempts if isinstance(attempts, list) else []
    normalized.setdefault("current_attempt", None)
    try:
        max_attempts = int(normalized.get("max_attempts") or 1)
    except (TypeError, ValueError):
        max_attempts = 1
    normalized["max_attempts"] = max(1, max_attempts)
    policy = normalized.get("policy")
    normalized["policy"] = policy if isinstance(policy, dict) else {}
    lease = normalized.get("lease")
    normalized["lease"] = lease if isinstance(lease, dict) else None
    normalized.setdefault("created_at", normalized.get("updated_at") or utc_now())
    normalized.setdefault("updated_at", normalized.get("created_at"))
    normalized.setdefault("completed_at", None)
    normalized.setdefault("blocked_reason", None)
    if path:
        normalized["objective_path"] = str(path)
    else:
        normalized.setdefault("objective_path", None)
    return normalized


def load_objectives(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    objective_dir = root / "objectives"
    objectives: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(objective_dir.glob("*.json")):
        data, error = read_json(path)
        if error:
            errors.append({"path": str(path), "error": error})
            continue
        if data:
            try:
                objectives.append(normalize_objective(data, path))
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
    return objectives, errors


def write_objective(paths: dict[str, Path], objective: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_objective(objective)
    normalized["updated_at"] = utc_now()
    path = objective_path(paths, normalized["objective_id"])
    stored = dict(normalized)
    stored.pop("objective_path", None)
    atomic_write_json(path, stored)
    normalized["objective_path"] = str(path)
    return normalized


def build_task(
    *,
    task_id: str | None,
    instruction: str,
    summary: str | None,
    intent: str | None,
    after_job_id: str | None,
    after_event_id: str | None,
    trigger_on: str,
    evidence_paths: list[str] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "version": TASK_VERSION,
        "task_id": safe_id(task_id or f"task-{uuid.uuid4().hex[:12]}"),
        "status": "waiting",
        "instruction": instruction,
        "summary": summary,
        "intent": intent,
        "after_job_id": after_job_id,
        "after_event_id": after_event_id,
        "trigger_on": trigger_on,
        "evidence_paths": evidence_paths or [],
        "created_at": now,
        "updated_at": now,
        "claimed_at": None,
        "completed_at": None,
        "blocked_reason": None,
    }


def normalize_task(task: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    task_id = str(path.stem if path else task.get("task_id") or task.get("id") or f"task-{uuid.uuid4().hex[:12]}")
    normalized = dict(task)
    try:
        version = int(normalized.get("version") or TASK_VERSION)
    except (TypeError, ValueError):
        version = TASK_VERSION
    normalized["version"] = version
    normalized["task_id"] = safe_id(task_id)
    normalized["status"] = token_text(normalized.get("status")) or "waiting"
    if normalized.get("status") not in TASK_STATUSES:
        normalized["status"] = "waiting"
    normalized.setdefault("instruction", "")
    normalized.setdefault("summary", None)
    normalized.setdefault("intent", None)
    normalized.setdefault("after_job_id", None)
    if normalized.get("after_job_id") is not None:
        after_job_id = one_line_text(normalized.get("after_job_id"))
        normalized["after_job_id"] = safe_id(after_job_id) if after_job_id else None
    normalized.setdefault("after_event_id", None)
    if normalized.get("after_event_id") is not None:
        after_event_id = str(normalized["after_event_id"]).strip()
        normalized["after_event_id"] = after_event_id or None
    normalized.setdefault("trigger_on", "succeeded")
    normalized["trigger_on"] = token_text(normalized.get("trigger_on")) or "succeeded"
    if normalized.get("trigger_on") not in TASK_TRIGGERS:
        normalized["trigger_on"] = "succeeded"
    evidence_paths = normalized.get("evidence_paths")
    if isinstance(evidence_paths, list):
        normalized["evidence_paths"] = [str(value) for value in evidence_paths if value]
    elif isinstance(evidence_paths, str) and evidence_paths:
        normalized["evidence_paths"] = [evidence_paths]
    else:
        normalized["evidence_paths"] = []
    normalized.setdefault("created_at", normalized.get("updated_at") or utc_now())
    normalized.setdefault("updated_at", normalized.get("created_at"))
    normalized.setdefault("claimed_at", None)
    normalized.setdefault("completed_at", None)
    normalized.setdefault("blocked_reason", None)
    normalized["task_path"] = str(path) if path else normalized.get("task_path")
    return normalized


def load_tasks(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    task_dir = root / "tasks"
    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(task_dir.glob("*.json")):
        data, error = read_json(path)
        if error:
            errors.append({"path": str(path), "error": error})
            continue
        if data:
            try:
                tasks.append(normalize_task(data, path))
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
    return tasks, errors


def normalize_managed_job(record: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    job_id = safe_id(str(path.stem if path else record.get("job_id") or record.get("id") or "unknown"))
    normalized = dict(record)
    normalized.setdefault("version", STATE_VERSION)
    normalized["job_id"] = job_id
    normalized["id"] = job_id
    normalized.setdefault("kind", "job")
    normalized["kind"] = token_text(normalized.get("kind")) or "job"
    normalized.setdefault("status", "unknown")
    normalized["status"] = token_text(normalized.get("status")) or "unknown"
    normalized.setdefault("pane_id", None)
    normalized.setdefault("heartbeat_at", normalized.get("updated_at"))
    normalized.setdefault("updated_at", normalized.get("heartbeat_at"))
    if path:
        root = path.parent.parent
        normalized["status_path"] = str(root / "status" / f"{job_id}.json")
        normalized["log_path"] = str(root / "logs" / f"{job_id}.log")
        normalized["job_path"] = str(path)
    else:
        normalized.setdefault("status_path", None)
        normalized.setdefault("log_path", None)
        normalized.setdefault("job_path", None)
    return normalized


def strip_managed_transient_fields(record: dict[str, Any]) -> None:
    for key in MANAGED_TRANSIENT_FIELDS:
        record.pop(key, None)
    if token_text(record.get("status")) != "stale":
        record.pop("stale_reason", None)


def load_managed_jobs(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    job_dir = root / "jobs"
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(job_dir.glob("*.json")):
        data, error = read_json(path)
        if error:
            errors.append({"path": str(path), "error": error})
            continue
        if data:
            jobs.append(normalize_managed_job(data, path))
    return jobs, errors


def managed_job_with_effective_state(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    pid = managed_job_pid(enriched)
    pid_running = pid_is_running(pid) if pid else False
    pid_matches = managed_worker_pid_matches(enriched) if pid_running else False
    effective_status, process_state, process_reason = managed_job_effective_status(
        enriched,
        pid_running=pid_running,
        pid_matches=pid_matches,
    )
    stale_reason = managed_job_stale_reason(enriched, pid_running=pid_running, pid_matches=pid_matches)
    enriched["pid_running"] = pid_running
    enriched["pid_matches"] = pid_matches
    enriched["effective_status"] = effective_status
    enriched["process_state"] = process_state
    enriched["stale"] = bool(stale_reason or effective_status in {"dead", "orphaned", "stale"})
    reason = stale_reason or process_reason
    if reason:
        enriched["stale_reason"] = reason
    else:
        enriched.pop("stale_reason", None)
    return enriched


def write_task(paths: dict[str, Path], task: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_task(task)
    for key in TASK_TRANSIENT_FIELDS:
        normalized.pop(key, None)
    normalized["updated_at"] = utc_now()
    path = task_path(paths, normalized["task_id"])
    stored = dict(normalized)
    atomic_write_json(path, stored)
    normalized["task_path"] = str(path)
    return normalized


def status_matches_trigger(status: dict[str, Any], trigger_on: str) -> bool:
    state = token_text(status.get("status"))
    trigger_on = token_text(trigger_on)
    if trigger_on == "terminal":
        return state in TERMINAL_STATUSES
    if trigger_on == "succeeded":
        return state == "succeeded"
    if trigger_on == "failed":
        return state in FAILED_TRIGGER_STATUSES
    return False


def matching_status(task: dict[str, Any], statuses: list[dict[str, Any]]) -> dict[str, Any] | None:
    trigger_on = token_text(task.get("trigger_on")) or "succeeded"
    after_event_id = task.get("after_event_id")
    after_job_id = task.get("after_job_id")
    if not after_event_id and not after_job_id:
        return None
    for status in statuses:
        if after_event_id and status.get("event_id") != after_event_id:
            continue
        if after_job_id and status.get("id") != after_job_id:
            continue
        if status_matches_trigger(status, trigger_on):
            return status
    return None


def effective_task_status(
    task: dict[str, Any],
    statuses: list[dict[str, Any]],
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> tuple[str, dict[str, Any] | None, bool]:
    raw_status = token_text(task.get("status")) or "waiting"
    match = matching_status(task, statuses)
    effective = raw_status
    if raw_status == "waiting" and match:
        effective = "ready"
    stale = False
    if effective == "in_progress":
        age = age_seconds(task.get("claimed_at") or task.get("updated_at"))
        stale = age is not None and age >= stale_seconds
    return effective, match, stale


def task_with_effective_state(
    task: dict[str, Any],
    statuses: list[dict[str, Any]],
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    effective, match, stale = effective_task_status(task, statuses, stale_seconds=stale_seconds)
    enriched = dict(task)
    enriched["effective_status"] = effective
    enriched["stale"] = stale
    enriched["matched_status"] = match
    if match:
        evidence = list(enriched.get("evidence_paths") or [])
        for key in ("status_path", "log_path"):
            value = match.get(key)
            if value and value not in evidence:
                evidence.append(value)
        enriched["evidence_paths"] = evidence
    return enriched


def load_task_state(
    paths: dict[str, Path],
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    statuses, status_errors = load_statuses_normalized(paths["root"])
    jobs, job_errors = load_managed_jobs(paths["root"])
    tasks, task_errors = load_tasks(paths["root"])
    jobs = [managed_job_with_effective_state(job) for job in jobs]
    enriched_tasks = [task_with_effective_state(task, statuses, stale_seconds=stale_seconds) for task in tasks]
    statuses.sort(key=sort_key, reverse=True)
    jobs.sort(key=lambda job: str(job.get("heartbeat_at") or job.get("updated_at") or ""), reverse=True)
    enriched_tasks.sort(key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""))
    return {
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "statuses": statuses,
        "jobs": jobs,
        "tasks": enriched_tasks,
        "errors": status_errors + job_errors + task_errors,
    }


def classify_task_state(state: dict[str, Any], *, max_items: int = 5) -> dict[str, Any]:
    tasks = list(state["tasks"])
    statuses = list(state["statuses"])
    jobs = list(state.get("jobs", []))
    ready = [task for task in tasks if task.get("effective_status") == "ready"]
    running_tasks = [task for task in tasks if task.get("effective_status") == "in_progress" and not task.get("stale")]
    blocked = [
        task
        for task in tasks
        if task.get("effective_status") in {"blocked", "cancelled"} or task.get("stale")
    ]
    running_statuses = [status for status in statuses if token_text(status.get("status")) in RUNNING_STATUSES]
    active_jobs = [job for job in jobs if is_active_managed_job(job) and not job.get("stale")]
    recent_jobs = [status for status in statuses if is_terminal(status)]
    return {
        "workspace": state["workspace"],
        "state_dir": state["state_dir"],
        "ready_tasks": ready[:max_items],
        "running": (running_tasks + running_statuses + active_jobs)[:max_items],
        "recent_jobs": recent_jobs[:max_items],
        "blocked": blocked[:max_items],
        "errors": state["errors"],
        "all_tasks": tasks,
    }


def task_summary_line(task: dict[str, Any]) -> str:
    summary = bounded_one_line_text(task.get("summary") or task.get("instruction")) or "(no instruction)"
    status = "stale" if task.get("stale") else one_line_text(task.get("effective_status") or task.get("status")) or "unknown"
    return f"{task.get('task_id')} [{status}] {summary}"
