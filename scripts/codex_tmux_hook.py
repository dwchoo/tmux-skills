#!/usr/bin/env python3
"""Codex command hook reader for tmux-skills status files."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import tmux_state


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def hook_text(value: Any, *, limit: int = tmux_state.TASK_DISPLAY_TEXT_LIMIT, keep_tail: bool = False) -> str:
    return tmux_state.bounded_one_line_text(value, limit=limit, keep_tail=keep_tail)


def status_line(status: dict[str, Any]) -> str:
    name = hook_text(status.get("name") or status.get("id"))
    state = hook_text(status.get("status"))
    exit_code = hook_text(status.get("exit_code"))
    pane = hook_text(status.get("pane_id"))
    tail = hook_text(status.get("last_output"), keep_tail=True)
    parts = [f"{name}: {state}"]
    if exit_code:
        parts.append(f"exit={exit_code}")
    if pane:
        parts.append(f"pane={pane}")
    if tail:
        parts.append(f"tail={tail}")
    return " | ".join(parts)


def managed_job_line(job: dict[str, Any]) -> str:
    job_id = hook_text(job.get("job_id") or job.get("id"))
    status = hook_text(job.get("status"))
    kind = hook_text(job.get("kind"))
    pane = hook_text(job.get("pane_id"))
    heartbeat = hook_text(job.get("heartbeat_at"))
    return (
        f"managed job {job_id}: {status} "
        f"kind={kind} pane={pane} heartbeat={heartbeat}"
    )


def acknowledge_status(paths: dict[str, Any], status: dict[str, Any]) -> str | None:
    try:
        tmux_state.ack_status(paths, status)
    except Exception as exc:
        return f"Could not acknowledge terminal event: {hook_text(exc)}"
    return None


def manager_owns_terminal(paths: dict[str, Any], status: dict[str, Any]) -> bool:
    if status.get("manager_owned") is True or tmux_state.one_line_text(status.get("manager_id")):
        return True
    job_id = str(status.get("id") or status.get("job_id") or "")
    event_id = str(status.get("event_id") or "")
    status_path = str(status.get("status_path") or "")
    managers_dir = paths["root"] / "managers"
    try:
        manager_paths = list(managers_dir.glob("*.json"))
    except OSError:
        return False

    for manager_path in manager_paths:
        record, error = tmux_state.read_json(manager_path)
        if error or not isinstance(record, dict):
            continue
        jobs = record.get("jobs")
        if isinstance(jobs, dict):
            if job_id and job_id in jobs:
                return True
            for job in jobs.values():
                if not isinstance(job, dict):
                    continue
                if job_id and str(job.get("job_id") or "") == job_id:
                    return True
                if event_id and str(job.get("terminal_event_id") or "") == event_id:
                    return True
                if status_path and str(job.get("status_path") or "") == status_path:
                    return True
        events = record.get("events")
        if isinstance(events, dict):
            if event_id and event_id in events:
                return True
            for event in events.values():
                if not isinstance(event, dict):
                    continue
                if event_id and str(event.get("event_id") or "") == event_id:
                    return True
                if job_id and str(event.get("job_id") or "") == job_id:
                    return True
                if status_path and str(event.get("status_path") or "") == status_path:
                    return True
        notifications = record.get("notifications")
        if isinstance(notifications, list):
            for notification in notifications:
                if not isinstance(notification, dict):
                    continue
                if event_id and str(notification.get("event_id") or "") == event_id:
                    return True
                if job_id and str(notification.get("job_id") or "") == job_id:
                    return True
                if status_path and str(notification.get("status_path") or "") == status_path:
                    return True
    return False


def newest_unhandled_terminal(paths: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, str]], str | None]:
    all_errors: list[dict[str, str]] = []
    seen_event_ids: set[str] = set()
    while True:
        status, errors = tmux_state.newest_unacked_terminal(paths)
        all_errors.extend(errors)
        if not status:
            return None, all_errors, None
        event_id = str(status.get("event_id") or "")
        if event_id in seen_event_ids:
            return status, all_errors, None
        if manager_owns_terminal(paths, status):
            ack_error = acknowledge_status(paths, status)
            if ack_error:
                return status, all_errors, ack_error
            if not event_id:
                return None, all_errors, None
            seen_event_ids.add(event_id)
            continue
        return status, all_errors, None


def context(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    state = tmux_state.load_task_state(paths)
    classified = tmux_state.classify_task_state(state, max_items=3)
    statuses = state["statuses"]
    errors = state["errors"]
    interesting = [
        status
        for status in statuses
        if (tmux_state.is_terminal(status) or tmux_state.token_text(status.get("status")) in tmux_state.RUNNING_STATUSES)
        and not manager_owns_terminal(paths, status)
    ]
    interesting.sort(key=tmux_state.sort_key, reverse=True)
    lines = []
    for task in classified["ready_tasks"]:
        lines.append(f"ready task {task.get('task_id')}: {hook_text(task.get('instruction'))}")
        evidence = [path for path in task.get("evidence_paths", []) if path]
        if evidence:
            lines.append("evidence: " + ", ".join(evidence[:3]))
    active_jobs = [job for job in state.get("jobs", []) if tmux_state.is_active_managed_job(job) and not job.get("stale")]
    active_jobs.sort(key=lambda job: str(job.get("heartbeat_at") or job.get("updated_at") or ""), reverse=True)
    lines.extend(managed_job_line(job) for job in active_jobs[:3])
    lines.extend(status_line(status) for status in interesting[:3])
    if errors:
        lines.append(f"Skipped {len(errors)} unreadable tmux-skills state file(s).")
    if not lines:
        return {}
    return {
        "hookSpecificOutput": {
            "additionalContext": "tmux-skills status:\n" + "\n".join(f"- {line}" for line in lines)
        }
    }


def stop(args: argparse.Namespace, stdin_data: dict[str, Any]) -> dict[str, Any]:
    if stdin_data.get("stop_hook_active") is True:
        return {}

    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    state = tmux_state.load_task_state(paths)
    classified = tmux_state.classify_task_state(state, max_items=1)
    if classified["ready_tasks"]:
        task = classified["ready_tasks"][0]
        reason = f"tmux-skills has a ready task {task.get('task_id')}: {hook_text(task.get('instruction'))}"
        evidence = [path for path in task.get("evidence_paths", []) if path]
        if evidence:
            reason += "\nEvidence: " + ", ".join(evidence[:3])
        matched_status = task.get("matched_status")
        if isinstance(matched_status, dict) and tmux_state.is_terminal(matched_status):
            ack_error = acknowledge_status(paths, matched_status)
            if ack_error:
                reason += f"\n{ack_error}"
        errors = state.get("errors") or []
        if errors:
            reason += f"\nSkipped {len(errors)} unreadable tmux-skills state file(s)."
        return {"decision": "block", "reason": reason}

    status, errors, prior_ack_error = newest_unhandled_terminal(paths)
    if not status:
        return {}

    reason = "tmux-skills observed a terminal event: " + status_line(status)
    if prior_ack_error:
        reason += f"\n{prior_ack_error}"
    ack_error = acknowledge_status(paths, status)
    if ack_error:
        reason += f"\n{ack_error}"
    if errors:
        reason += f"\nSkipped {len(errors)} unreadable status file(s)."
    return {"decision": "block", "reason": reason}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read tmux-skills status for Codex hooks")
    subparsers = parser.add_subparsers(dest="action", required=True)

    context_parser = subparsers.add_parser("context", help="Emit additional Codex context from tmux-skills state")
    context_parser.add_argument("--event", choices=["SessionStart", "UserPromptSubmit"], required=True)
    context_parser.add_argument("--workspace")
    context_parser.add_argument("--state-dir")

    stop_parser = subparsers.add_parser("stop", help="Block once on ready tasks or unacknowledged terminal events")
    stop_parser.add_argument("--workspace")
    stop_parser.add_argument("--state-dir")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    stdin_data = read_stdin_json()
    if args.action == "context":
        output = context(args)
    elif args.action == "stop":
        output = stop(args, stdin_data)
    else:
        parser.error(f"unknown command: {args.action}")
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
