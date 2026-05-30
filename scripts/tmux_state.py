#!/usr/bin/env python3
"""Shared state helpers for tmux-skills scripts."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_VERSION = 1
TASK_VERSION = 2
TERMINAL_STATUSES = {"succeeded", "failed", "matched", "timeout", "stopped", "submitted", "cancelled", "stale"}
MANAGED_ACTIVE_STATUSES = {"starting", "running", "waiting", "waiting_status", "waiting_pane_idle"}
RUNNING_STATUSES = {"pending", "running", "starting", "waiting", "waiting_status", "waiting_pane_idle"}
TASK_STATUSES = {"waiting", "ready", "in_progress", "done", "blocked", "cancelled"}
TASK_OPEN_STATUSES = {"waiting", "ready", "in_progress"}
DEFAULT_STALE_SECONDS = 30 * 60


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
    }


def ensure_state_dirs(paths: dict[str, Path]) -> None:
    for key in ("commands", "logs", "status", "acks", "tasks", "jobs"):
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


def job_registry_lock_path(paths: dict[str, Path]) -> Path:
    return paths["jobs"] / ".registry.lock"


def read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)

    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


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
    data: dict[str, Any] = {
        "version": STATE_VERSION,
        "kind": kind,
        "id": item_id,
        "attempt": attempt,
        "event_id": None,
        "name": name,
        "status": status,
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
    if status in TERMINAL_STATUSES:
        data["ended_at"] = ended_at or now
        data["event_id"] = terminal_event_id(data)
    return data


def write_status(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    updated["updated_at"] = utc_now()
    if updated.get("status") in TERMINAL_STATUSES:
        updated["ended_at"] = updated.get("ended_at") or updated["updated_at"]
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
    item_id = str(status.get("id") or status.get("job_id") or (path.stem if path else "unknown"))
    normalized = dict(status)
    normalized.setdefault("version", STATE_VERSION)
    normalized["id"] = item_id
    normalized.setdefault("kind", "job")
    normalized.setdefault("attempt", 1)
    normalized.setdefault("event_id", None)
    normalized.setdefault("name", None)
    normalized.setdefault("status", "unknown")
    normalized.setdefault("exit_code", None)
    normalized.setdefault("pane_id", None)
    normalized.setdefault("command_preview", None)
    normalized.setdefault("cwd", None)
    normalized.setdefault("started_at", normalized.get("updated_at"))
    normalized.setdefault("updated_at", normalized.get("ended_at") or normalized.get("started_at"))
    normalized.setdefault("ended_at", None)
    normalized.setdefault("status_path", str(path) if path else None)
    normalized.setdefault("log_path", None)
    normalized.setdefault("last_output", "")
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
    return str(status.get("status") or "") in TERMINAL_STATUSES


def is_active_managed_job(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "") in MANAGED_ACTIVE_STATUSES


def managed_job_interval_seconds(record: dict[str, Any]) -> float:
    try:
        interval = float(record.get("check_interval_seconds") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    return max(interval, 0.0)


def managed_job_stale_threshold(record: dict[str, Any]) -> float:
    return max(3.0 * managed_job_interval_seconds(record), 300.0)


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
    task_id = str(task.get("task_id") or task.get("id") or (path.stem if path else f"task-{uuid.uuid4().hex[:12]}"))
    normalized = dict(task)
    try:
        version = int(normalized.get("version") or TASK_VERSION)
    except (TypeError, ValueError):
        version = TASK_VERSION
    normalized["version"] = version
    normalized["task_id"] = safe_id(task_id)
    if normalized.get("status") not in TASK_STATUSES:
        normalized["status"] = "waiting"
    normalized.setdefault("instruction", "")
    normalized.setdefault("summary", None)
    normalized.setdefault("intent", None)
    normalized.setdefault("after_job_id", None)
    normalized.setdefault("after_event_id", None)
    normalized.setdefault("trigger_on", "succeeded")
    normalized.setdefault("evidence_paths", [])
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
    job_id = str(record.get("job_id") or record.get("id") or (path.stem if path else "unknown"))
    normalized = dict(record)
    normalized.setdefault("version", STATE_VERSION)
    normalized["job_id"] = job_id
    normalized["id"] = job_id
    normalized.setdefault("kind", "job")
    normalized.setdefault("status", "unknown")
    normalized.setdefault("pane_id", None)
    normalized.setdefault("heartbeat_at", normalized.get("updated_at"))
    normalized.setdefault("updated_at", normalized.get("heartbeat_at"))
    normalized.setdefault("status_path", None)
    normalized.setdefault("log_path", None)
    normalized.setdefault("job_path", str(path) if path else None)
    return normalized


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


def write_task(paths: dict[str, Path], task: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_task(task)
    normalized["updated_at"] = utc_now()
    path = task_path(paths, normalized["task_id"])
    stored = dict(normalized)
    stored.pop("task_path", None)
    atomic_write_json(path, stored)
    normalized["task_path"] = str(path)
    return normalized


def status_matches_trigger(status: dict[str, Any], trigger_on: str) -> bool:
    state = str(status.get("status") or "")
    if trigger_on == "terminal":
        return state in TERMINAL_STATUSES
    if trigger_on == "succeeded":
        return state == "succeeded"
    if trigger_on == "failed":
        return state in {"failed", "timeout", "stopped"}
    return False


def matching_status(task: dict[str, Any], statuses: list[dict[str, Any]]) -> dict[str, Any] | None:
    trigger_on = str(task.get("trigger_on") or "succeeded")
    after_event_id = task.get("after_event_id")
    after_job_id = task.get("after_job_id")
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
    raw_status = str(task.get("status") or "waiting")
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
    running_statuses = [status for status in statuses if str(status.get("status") or "") in RUNNING_STATUSES]
    active_jobs = [job for job in jobs if is_active_managed_job(job)]
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
    summary = task.get("summary") or task.get("instruction") or "(no instruction)"
    return f"{task.get('task_id')} [{task.get('effective_status', task.get('status'))}] {summary}"
