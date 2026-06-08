#!/usr/bin/env python3
"""MCP stdio server for tmux manager-owned workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tmux_control  # noqa: E402
import tmux_manager  # noqa: E402
import tmux_state  # noqa: E402


PROTOCOL_VERSION = "2025-06-18"


def text_content(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def tool_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "manager.start",
        "description": "Start or reuse a tmux manager. Returns redacted manager identity only.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "job_id": {"type": "string"},
                "command": {"type": "string"},
                "command_file": {"type": "string"},
                "notify": {"type": "string", "enum": ["bridge", "tmux-inject", "none"]},
                "thread_id": {"type": "string"},
                "endpoint": {"type": "string"},
                "codex_pane": {"type": "string"},
                "cwd": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
                "process_mode": {"type": "string", "enum": ["foreground", "background"]},
            }
        ),
    },
    {
        "name": "manager.submit",
        "description": "Submit manager-owned work and return only an opaque job_handle plus codex_contract.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "job_id": {"type": "string"},
                "command": {"type": "string"},
                "command_file": {"type": "string"},
                "cwd": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
                "new_worker": {"type": "boolean"},
            },
            ["job_id"],
        ),
    },
    {
        "name": "manager.status",
        "description": "Return redacted manager status. Raw evidence is not included without an override.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
                "manual_override_reason": {"type": "string"},
            }
        ),
    },
    {
        "name": "manager.observe",
        "description": "Read manager-owned evidence with an event token or explicit bounded observe grant.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "job_handle": {"type": "string"},
                "event_id": {"type": "string"},
                "observe_token": {"type": "string"},
                "reason": {"type": "string"},
                "once": {"type": "boolean"},
                "ttl_seconds": {"type": "number"},
                "interval_seconds": {"type": "number"},
                "max_reads": {"type": "integer"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
                "manual_override_reason": {"type": "string"},
            }
        ),
    },
    {
        "name": "manager.ack",
        "description": "Acknowledge one manager event and consume its event-scoped read token.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "event_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "note": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
            },
            ["event_id"],
        ),
    },
    {
        "name": "manager.run_next",
        "description": "Queue sequential follow-up work after the relevant manager event is acknowledged.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "job_id": {"type": "string"},
                "command": {"type": "string"},
                "command_file": {"type": "string"},
                "cwd": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
            },
            ["job_id"],
        ),
    },
    {
        "name": "manager.cancel",
        "description": "Cancel manager lifecycle or selected manager-owned worker jobs.",
        "inputSchema": tool_schema(
            {
                "manager_id": {"type": "string"},
                "workspace": {"type": "string"},
                "state_dir": {"type": "string"},
                "stop_worker": {"type": "boolean"},
                "job_id": {"type": "string"},
                "all_workers": {"type": "boolean"},
            }
        ),
    },
]


def default_manager_id(manager_id: str | None, workspace: str | None, state_dir: str | None) -> str:
    if tmux_state.one_line_text(manager_id):
        return tmux_manager.manager_id_value(manager_id)
    paths = tmux_manager.manager_paths(workspace, state_dir)
    return tmux_control.default_manager_id(paths)


def redacted_start_result(result: dict[str, Any]) -> dict[str, Any]:
    keep = {"manager_id", "started", "status", "notify", "reason", "queued", "job_handle", "codex_contract"}
    redacted = {key: value for key, value in result.items() if key in keep}
    redacted["redacted"] = True
    return redacted


def call_start(arguments: dict[str, Any]) -> dict[str, Any]:
    args = SimpleNamespace(
        manager_id=arguments.get("manager_id"),
        job_id=arguments.get("job_id"),
        command_text=arguments.get("command"),
        command_file=arguments.get("command_file"),
        notify=arguments.get("notify") or "bridge",
        thread_id=arguments.get("thread_id"),
        endpoint=arguments.get("endpoint"),
        codex_pane=arguments.get("codex_pane"),
        cwd=arguments.get("cwd"),
        workspace=arguments.get("workspace"),
        state_dir=arguments.get("state_dir"),
        poll_seconds=2.0,
        dashboard_renderer="pane",
        log_max_bytes=tmux_manager.DEFAULT_MANAGER_LOG_MAX_BYTES,
        process_mode=arguments.get("process_mode") or "foreground",
    )
    return redacted_start_result(tmux_control.manager_start(args))


def call_submit(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    args = SimpleNamespace(
        manager_id=manager_id,
        job_id=arguments.get("job_id"),
        command_text=arguments.get("command"),
        command_file=arguments.get("command_file"),
        cwd=arguments.get("cwd"),
        workspace=arguments.get("workspace"),
        state_dir=arguments.get("state_dir"),
        pane=None,
        new_worker=bool(arguments.get("new_worker")),
    )
    return tmux_control.manager_submit(args)


def call_status(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    return tmux_manager.manager_status_public(
        manager_id,
        arguments.get("workspace"),
        arguments.get("state_dir"),
        manual_override_reason=arguments.get("manual_override_reason"),
    )


def call_observe(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    return tmux_manager.observe_manager_output(
        manager_id=manager_id,
        workspace=arguments.get("workspace"),
        state_dir=arguments.get("state_dir"),
        job_handle=arguments.get("job_handle"),
        event_id=arguments.get("event_id"),
        observe_token=arguments.get("observe_token"),
        reason=arguments.get("reason"),
        once=bool(arguments.get("once", True)),
        ttl_seconds=float(arguments.get("ttl_seconds") or tmux_manager.MANAGER_OBSERVE_DEFAULT_TTL_SECONDS),
        interval_seconds=float(arguments.get("interval_seconds") or tmux_manager.MANAGER_OBSERVE_DEFAULT_INTERVAL_SECONDS),
        max_reads=int(arguments.get("max_reads") or tmux_manager.MANAGER_OBSERVE_DEFAULT_MAX_READS),
        manual_override_reason=arguments.get("manual_override_reason"),
    )


def public_ack(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key in {"manager_id", "event_id", "acked", "reason"}}


def call_ack(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    return public_ack(
        tmux_manager.ack_manager_event(
            manager_id=manager_id,
            event_id=str(arguments.get("event_id") or ""),
            workspace=arguments.get("workspace"),
            state_dir=arguments.get("state_dir"),
            turn_id=arguments.get("turn_id"),
            note=arguments.get("note"),
        )
    )


def call_run_next(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    result = tmux_manager.queue_manager_job(
        manager_id=manager_id,
        job_id=str(arguments.get("job_id") or ""),
        command_text=arguments.get("command"),
        command_file=arguments.get("command_file"),
        workspace=arguments.get("workspace"),
        state_dir=arguments.get("state_dir"),
        cwd=arguments.get("cwd"),
    )
    paths = tmux_manager.manager_paths(arguments.get("workspace"), arguments.get("state_dir"))
    tmux_manager.append_manager_audit(
        paths,
        manager_id=manager_id,
        action="run_next",
        result="queued" if result.get("queued") else "denied",
        details={"reason": result.get("reason"), "job_handle": result.get("job_handle")},
    )
    return tmux_manager.public_submit_result(result, paths)


def call_cancel(arguments: dict[str, Any]) -> dict[str, Any]:
    manager_id = default_manager_id(arguments.get("manager_id"), arguments.get("workspace"), arguments.get("state_dir"))
    result = tmux_manager.cancel_manager(
        manager_id,
        workspace=arguments.get("workspace"),
        state_dir=arguments.get("state_dir"),
        stop_worker=bool(arguments.get("stop_worker")),
        job_id=arguments.get("job_id"),
        all_workers=bool(arguments.get("all_workers")),
    )
    paths = tmux_manager.manager_paths(arguments.get("workspace"), arguments.get("state_dir"))
    tmux_manager.append_manager_audit(
        paths,
        manager_id=manager_id,
        action="cancel",
        result="cancelled" if result.get("cancelled") else "denied",
        details={"reason": result.get("reason"), "job_id": arguments.get("job_id")},
    )
    return {key: value for key, value in result.items() if key != "record"}


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "manager.start": call_start,
    "manager.submit": call_submit,
    "manager.status": call_status,
    "manager.observe": call_observe,
    "manager.ack": call_ack,
    "manager.run_next": call_run_next,
    "manager.cancel": call_cancel,
}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tmux-manager", "version": "1.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id, "result": text_content({"error": f"unknown tool: {name}"}, is_error=True)}
        try:
            payload = handler(arguments)
        except Exception as exc:  # MCP tool errors are returned as tool results.
            return {"jsonrpc": "2.0", "id": request_id, "result": text_content({"error": str(exc)}, is_error=True)}
        return {"jsonrpc": "2.0", "id": request_id, "result": text_content(payload)}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        response = handle_request(request)
        if response is not None:
            print(json.dumps(response, sort_keys=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="tmux-manager MCP server")
    parser.parse_args()
    serve()


if __name__ == "__main__":
    main()
