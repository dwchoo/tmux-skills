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


def status_line(status: dict[str, Any]) -> str:
    name = status.get("name") or status.get("id")
    state = status.get("status")
    exit_code = status.get("exit_code")
    pane = status.get("pane_id")
    tail = str(status.get("last_output") or "").strip()
    if len(tail) > 800:
        tail = tail[-800:]
    parts = [f"{name}: {state}"]
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if pane:
        parts.append(f"pane={pane}")
    if tail:
        parts.append(f"tail={tail}")
    return " | ".join(parts)


def context(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    state = tmux_state.load_task_state(paths)
    classified = tmux_state.classify_task_state(state, max_items=3)
    statuses = state["statuses"]
    errors = state["errors"]
    interesting = [
        status
        for status in statuses
        if tmux_state.is_terminal(status) or str(status.get("status") or "") in tmux_state.RUNNING_STATUSES
    ]
    interesting.sort(key=tmux_state.sort_key, reverse=True)
    lines = []
    for task in classified["ready_tasks"]:
        lines.append(f"ready task {task.get('task_id')}: {task.get('instruction')}")
        evidence = [path for path in task.get("evidence_paths", []) if path]
        if evidence:
            lines.append("evidence: " + ", ".join(evidence[:3]))
    lines.extend(status_line(status) for status in interesting[:3])
    if errors:
        lines.append(f"Skipped {len(errors)} unreadable tmux-skills status file(s).")
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
        reason = f"tmux-skills has a ready task {task.get('task_id')}: {task.get('instruction')}"
        evidence = [path for path in task.get("evidence_paths", []) if path]
        if evidence:
            reason += "\nEvidence: " + ", ".join(evidence[:3])
        return {"decision": "block", "reason": reason}

    status, errors = tmux_state.newest_unacked_terminal(paths)
    if not status:
        return {}

    tmux_state.ack_status(paths, status)
    reason = "tmux-skills observed a terminal event: " + status_line(status)
    if errors:
        reason += f"\nSkipped {len(errors)} unreadable status file(s)."
    return {"decision": "block", "reason": reason}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read tmux-skills status for Codex hooks")
    subparsers = parser.add_subparsers(dest="action", required=True)

    context_parser = subparsers.add_parser("context")
    context_parser.add_argument("--event", choices=["SessionStart", "UserPromptSubmit"], required=True)
    context_parser.add_argument("--workspace")
    context_parser.add_argument("--state-dir")

    stop_parser = subparsers.add_parser("stop")
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
