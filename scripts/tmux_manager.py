#!/usr/bin/env python3
"""Visible manager pane loop for tmux-skills long tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_app_server_client
import tmux_bridge
import tmux_state
import tmux_text


MANAGER_VERSION = 1
DEFAULT_MANAGER_LOG_MAX_BYTES = 65536
MANAGER_STATUSES = {"starting", "idle", "queued", "running", "waiting_for_codex", "cancel_requested", "cancelled", "failed"}
MANAGER_CANCEL_STATUSES = {"cancel_requested", "cancelled"}
MANAGER_PROCESS_MODES = {"foreground", "background"}
MANAGER_DASHBOARD_RENDERERS = {"pane", "none"}
MANAGER_TUI_BACKENDS = {"compact", "textual"}
DASHBOARD_MODES = ("summary", "jobs", "events")
MANAGER_PS_POC_STATUS_UNSUPPORTED = "unsupported_by_current_codex_surface"
MANAGER_PS_POC_STATUS_VERIFIED = "verified"
MANAGER_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "stopped", "timeout", "cancelled", "stale"}
MANAGER_DELETABLE_JOB_STATUSES = MANAGER_TERMINAL_JOB_STATUSES | {"complete", "completed", "error"}
BRIDGE_VERIFICATION_STATUSES = {"unverified", "awaiting_ack", "verified", "expired", "mismatched_config", "submission_failed"}
TMUX_INJECT_NOTIFICATION_STATUSES = {
    "awaiting_receipt",
    "queued_in_codex",
    "injected",
    "inject_pending",
    "inject_refused",
    "receipt_blocked",
    "blocked_by_other_wake",
    "coalesced",
    "deferred",
    "discarded",
}
TMUX_INJECT_PRIMARY_SUBMIT_KEY = "C-m"
TMUX_INJECT_FOLLOWUP_SUBMIT_KEY = "C-m"
TMUX_INJECT_QUEUE_SUBMIT_KEY = "Tab"
TMUX_INJECT_ACK_RECHECK_SECONDS = 10.0
TMUX_INJECT_RECEIPT_RETRY_MAX = 1
TMUX_INJECT_RECEIPT_SIDECAR_CHECK_MAX = 6
TMUX_INJECT_CAPTURE_LINES = 30
TMUX_INJECT_CAPTURE_MAX_CHARS = 4000
DEFAULT_CODEX_SDK_MODEL = "gpt-5.5"
DEFAULT_CODEX_SDK_REASONING_EFFORT = "low"
DEFAULT_CODEX_SIDECAR_FAST_PATH = True
TMUX_SKILLS_CONFIG_FILENAME = "tmux-skills.config.json"
TMUX_INJECT_WAKE_PROMPT = "\n".join(
    [
        "ID:{wake_id};",
        "tmux-skills event ready. Use $tmux-control only.",
        "",
        "Manager ID: {manager_id}",
        "Event ID: {event_id}",
        "",
        "Inspect manager status once. Handle only the latest unacked event.",
        "If stale or already handled, ack/report only.",
        "After run-next, wait for the next manager event; do not poll or monitor directly.",
    ]
)
TMUX_INJECT_WAKE_ID_PATTERN = re.compile(r"ID:([0-9a-f]{6})")
MANAGER_CONTROLLED_NEXT_REMINDER = "After manager run-next, wait for the next manager event; do not poll or monitor directly."


def tmux_skills_config_path() -> Path:
    override = tmux_state.one_line_text(os.environ.get("TMUX_SKILLS_CONFIG"))
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / TMUX_SKILLS_CONFIG_FILENAME


def read_tmux_skills_config() -> dict[str, Any]:
    path = tmux_skills_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def codex_sidecar_config() -> dict[str, Any]:
    config = read_tmux_skills_config()
    sidecar = config.get("codex_sidecar") if isinstance(config.get("codex_sidecar"), dict) else {}
    model = tmux_state.one_line_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_MODEL")) or tmux_state.one_line_text(sidecar.get("model"))
    effort = (
        tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_REASONING_EFFORT"))
        or tmux_state.token_text(sidecar.get("reasoning_effort"))
        or DEFAULT_CODEX_SDK_REASONING_EFFORT
    )
    if effort not in {"low", "medium", "high"}:
        effort = DEFAULT_CODEX_SDK_REASONING_EFFORT
    fast_path = sidecar.get("deterministic_fast_path")
    if isinstance(fast_path, str):
        fast_path_enabled = tmux_state.token_text(fast_path) not in {"0", "false", "no", "off"}
    elif isinstance(fast_path, bool):
        fast_path_enabled = fast_path
    else:
        fast_path_enabled = DEFAULT_CODEX_SIDECAR_FAST_PATH
    return {
        "enabled": bool(sidecar.get("enabled", False)),
        "model": model or DEFAULT_CODEX_SDK_MODEL,
        "reasoning_effort": effort,
        "deterministic_fast_path": fast_path_enabled,
        "config_path": str(tmux_skills_config_path()),
    }


def config_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    token = tmux_state.token_text(value)
    if not token:
        return default
    return token not in {"0", "false", "no", "off"}


def debug_sidecar_trace_enabled() -> bool:
    override = tmux_state.token_text(os.environ.get("TMUX_SKILLS_DEBUG_SIDECAR"))
    if override:
        return config_bool(override)
    config = read_tmux_skills_config()
    debug = config.get("debug") if isinstance(config.get("debug"), dict) else {}
    return config_bool(debug.get("sidecar_trace"), default=False)


def debug_sidecar_payload_enabled() -> bool:
    override = tmux_state.token_text(os.environ.get("TMUX_SKILLS_DEBUG_SIDECAR_PAYLOAD"))
    if override:
        return config_bool(override)
    config = read_tmux_skills_config()
    debug = config.get("debug") if isinstance(config.get("debug"), dict) else {}
    return config_bool(debug.get("sidecar_payload"), default=False)


def bounded_debug_text(value: Any, *, max_chars: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


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


def manager_process_mode_value(value: str | None) -> str:
    mode = tmux_state.token_text(value) or "foreground"
    return mode if mode in MANAGER_PROCESS_MODES else "foreground"


def manager_dashboard_renderer_value(value: str | None) -> str:
    renderer = tmux_state.token_text(value) or "pane"
    return renderer if renderer in MANAGER_DASHBOARD_RENDERERS else "pane"


def manager_tui_backend_value(value: str | None) -> str:
    backend = tmux_state.token_text(value) or "compact"
    return backend if backend in MANAGER_TUI_BACKENDS else "compact"


def manager_tui_backend_env() -> str:
    return manager_tui_backend_value(os.environ.get("TMUX_SKILLS_MANAGER_TUI"))


def manager_tui_venv_python(paths: dict[str, Path]) -> Path:
    return paths["root"] / "manager-tui-venv" / "bin" / "python"


def ensure_manager_tui_venv(paths: dict[str, Path], *, timeout_seconds: float = 60.0) -> dict[str, Any]:
    python_path = manager_tui_venv_python(paths)
    marker_path = python_path.parent.parent / ".textual-rich-installed"
    if python_path.exists() and marker_path.exists():
        return {"ok": True, "python": str(python_path), "venv": str(python_path.parent.parent), "reused": True}
    venv_dir = python_path.parent.parent
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        create = subprocess.run(["uv", "venv", str(venv_dir)], text=True, capture_output=True, timeout=timeout_seconds)
    except Exception as exc:
        return {"ok": False, "reason": f"uv venv failed: {exc}", "python": str(python_path), "venv": str(venv_dir)}
    if create.returncode != 0:
        reason = create.stderr.strip() or create.stdout.strip() or f"uv venv exited {create.returncode}"
        return {"ok": False, "reason": tmux_state.one_line_text(reason), "python": str(python_path), "venv": str(venv_dir)}
    try:
        install = subprocess.run(
            ["uv", "pip", "install", "--python", str(python_path), "textual", "rich"],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"uv pip install failed: {exc}", "python": str(python_path), "venv": str(venv_dir)}
    if install.returncode != 0:
        reason = install.stderr.strip() or install.stdout.strip() or f"uv pip install exited {install.returncode}"
        return {"ok": False, "reason": tmux_state.one_line_text(reason), "python": str(python_path), "venv": str(venv_dir)}
    marker_path.write_text(tmux_state.utc_now(), encoding="utf-8")
    return {"ok": True, "python": str(python_path), "venv": str(venv_dir), "reused": False}


def manager_launcher_for_mode(process_mode: str | None) -> tuple[str, str]:
    mode = manager_process_mode_value(process_mode)
    if mode == "background":
        return "codex-background-terminal", "codex-background-terminal-lifetime"
    return "foreground-codex-command", "foreground-command-lifetime"


def manager_proofs_dir(paths: dict[str, Path]) -> Path:
    return paths["root"] / "proofs"


def manager_ps_poc_paths(paths: dict[str, Path], timestamp: str | None = None) -> dict[str, Path]:
    stamp = timestamp or f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{time.time_ns() % 1_000_000:06d}"
    proof_dir = manager_proofs_dir(paths)
    return {
        "json": proof_dir / f"manager-ps-poc-{stamp}.json",
        "manual": proof_dir / f"manager-ps-poc-{stamp}.manual.md",
    }


def write_manager_ps_poc_manual_note(path: Path, proof: dict[str, Any]) -> None:
    supported = bool(proof.get("supported"))
    lines = [
        "# tmux-skills manager /ps PoC manual evidence",
        "",
        f"status: {proof.get('status')}",
        f"workspace: {proof.get('workspace')}",
        f"checked_at: {proof.get('checked_at')}",
        "",
        (
            "This artifact records a passing background-manager launch surface."
            if supported
            else "This artifact is not a passing proof. Mark it passing only after the same launcher planned for"
        ),
        "" if supported else "`manager start --process-mode background` satisfies every item below.",
        "",
        "- manager start returns quickly and main Codex is idle.",
        "- Codex `/ps` shows the manager process by name or pid.",
        "- Manager heartbeat continues while Codex is idle.",
        "- Stopping/exiting Codex stops only the manager.",
        "- An already submitted tmux worker job keeps running and emitting output.",
        "- No hook wakeup or tmux send-keys into the Codex pane is used.",
        "",
        f"operator_confirmation: {'recorded' if supported else 'pending'}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def manager_ps_poc(workspace: str | None = None, state_dir: str | None = None) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    artifact_paths = manager_ps_poc_paths(paths)
    candidates: list[dict[str, Any]] = []
    for path in sorted(paths["managers"].glob("*.json")):
        record, error = read_manager_record(paths, path.stem)
        if error or record is None:
            continue
        pid = parse_pid(record.get("manager_pid"))
        if record.get("manager_process_mode") == "background" and pid_is_running(pid):
            candidates.append(
                {
                    "manager_id": record.get("manager_id"),
                    "manager_pid": pid,
                    "manager_path": record.get("manager_path"),
                    "heartbeat_at": record.get("heartbeat_at"),
                    "manager_launcher": record.get("manager_launcher"),
                    "manager_exit_watch": record.get("manager_exit_watch"),
                    "dashboard_path": record.get("dashboard_path"),
                }
            )
    supported = bool(candidates)
    status = MANAGER_PS_POC_STATUS_VERIFIED if supported else MANAGER_PS_POC_STATUS_UNSUPPORTED
    proof = {
        "poc": "manager_ps_background_terminal",
        "status": status,
        "supported": supported,
        "checked_at": tmux_state.utc_now(),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "background_managers": candidates,
        "reason": (
            "live Codex background-terminal manager record found"
            if supported
            else (
                "tmux-skills has no live Codex-owned background terminal manager record. "
                "A detached daemon, tmux-resident manager loop, OS ps check, or bridge-only daemon "
                "does not prove Codex /ps visibility while main Codex is idle."
            )
        ),
        "checks": [
            {
                "name": "codex_background_terminal_launch_surface",
                "status": "verified" if supported else "missing",
                "required": "start a long-running manager from Codex and return control to an idle main Codex thread",
            },
            {
                "name": "codex_ps_visibility",
                "status": "verified" if supported else "unverified",
                "required": "Codex /ps shows the manager process, not just an OS process listing",
            },
            {
                "name": "codex_exit_stops_manager_only",
                "status": "pending_runtime_exit_check" if supported else "unverified",
                "required": "Codex exit stops the manager but not an already submitted tmux worker job",
            },
            {
                "name": "forbidden_wakeup_mechanisms",
                "status": "not_used",
                "required": "no hook wakeup and no tmux send-keys into a Codex pane",
            },
        ],
        "proof_path": str(artifact_paths["json"]),
        "manual_note_path": str(artifact_paths["manual"]),
    }
    tmux_state.atomic_write_json(artifact_paths["json"], proof)
    write_manager_ps_poc_manual_note(artifact_paths["manual"], proof)
    return proof


def manager_command_request_path(paths: dict[str, Path], manager_id: str, job_id: str) -> Path:
    return paths["commands"] / f"{manager_id_value(manager_id)}-{tmux_state.safe_id(job_id)}.manager-command.sh"


def manager_dashboard_path(paths: dict[str, Path], manager_id: str) -> Path:
    return paths["managers"] / f"{manager_id_value(manager_id)}.dashboard.txt"


def manager_dashboard_viewer_state_path(paths: dict[str, Path], manager_id: str) -> Path:
    return paths["managers"] / f"{manager_id_value(manager_id)}.viewer.json"


def write_dashboard_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def refresh_dashboard_viewer_fields(record: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    record = dict(record)
    manager_id = manager_id_value(str(record.get("manager_id") or ""))
    state_path = manager_dashboard_viewer_state_path(paths, manager_id)
    record["dashboard_viewer_state_path"] = str(state_path)
    state, _error = tmux_state.read_json(state_path)
    if not isinstance(state, dict):
        if not pid_is_running(parse_pid(record.get("dashboard_viewer_pid"))):
            record["dashboard_viewer_pid"] = None
            record["dashboard_viewer_heartbeat_at"] = None
        return record

    pid = parse_pid(state.get("pid"))
    pane_id = tmux_state.one_line_text(state.get("pane_id"))
    if pane_id and pane_id != tmux_state.one_line_text(record.get("manager_pane_id")):
        record["dashboard_viewer_pid"] = None
        record["dashboard_viewer_heartbeat_at"] = None
        return record

    record["dashboard_viewer_pid"] = pid if pid_is_running(pid) else None
    record["dashboard_viewer_heartbeat_at"] = state.get("heartbeat_at") if record["dashboard_viewer_pid"] else None
    return record


def parse_pid(value: Any) -> int | None:
    try:
        pid = int(str(value))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def ensure_dashboard_viewer(record: dict[str, Any], paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    record = normalize_manager_record(record, paths)
    renderer = manager_dashboard_renderer_value(str(record.get("dashboard_renderer") or "pane"))
    record["dashboard_renderer"] = renderer
    state_path = manager_dashboard_viewer_state_path(paths, str(record["manager_id"]))
    record["dashboard_viewer_state_path"] = str(state_path)
    if renderer == "none":
        record["dashboard_viewer_pid"] = None
        record["dashboard_viewer_heartbeat_at"] = None
        return record, {"started": False, "reused": False, "renderer": "none", "state_path": str(state_path)}

    pane_id = tmux_state.one_line_text(record.get("manager_pane_id"))
    if not pane_exists(pane_id):
        return record, {
            "started": False,
            "reused": False,
            "renderer": renderer,
            "reason": "manager pane is missing",
            "pane_id": pane_id,
            "state_path": str(state_path),
        }

    state, _error = tmux_state.read_json(state_path)
    if isinstance(state, dict):
        pid = parse_pid(state.get("pid"))
        if (
            pid_is_running(pid)
            and tmux_state.one_line_text(state.get("pane_id")) == pane_id
            and tmux_state.one_line_text(state.get("manager_id")) == record["manager_id"]
        ):
            record["dashboard_viewer_pid"] = pid
            record["dashboard_viewer_heartbeat_at"] = state.get("heartbeat_at")
            return record, {
                "started": False,
                "reused": True,
                "renderer": renderer,
                "pid": pid,
                "pane_id": pane_id,
                "state_path": str(state_path),
            }

    dashboard_file = Path(str(record.get("dashboard_path") or manager_dashboard_path(paths, str(record["manager_id"]))))
    manager_file = manager_record_path(paths, str(record["manager_id"]))
    viewer_backend = manager_tui_backend_env()
    viewer_setup: dict[str, Any] | None = None
    viewer_python = sys.executable or "python3"
    viewer_script = script_dir() / "tmux_manager_viewer.py"
    if viewer_backend == "textual":
        viewer_setup = ensure_manager_tui_venv(paths)
        if viewer_setup.get("ok"):
            viewer_python = str(viewer_setup["python"])
            viewer_script = script_dir() / "tmux_manager_tui.py"
        else:
            record["last_dashboard_viewer_error"] = str(viewer_setup.get("reason") or "Textual viewer setup failed")
            viewer_backend = "compact"
    record["dashboard_viewer_backend"] = viewer_backend
    if viewer_setup is not None:
        record["dashboard_viewer_setup"] = viewer_setup
    poll_seconds = str(record.get("poll_seconds") or 2.0)
    command_args = [
        viewer_python,
        str(viewer_script),
        "--manager-id",
        str(record["manager_id"]),
        "--manager-file",
        str(manager_file),
        "--dashboard-file",
        str(dashboard_file),
        "--state-file",
        str(state_path),
        "--pane-id",
        str(pane_id),
        "--poll-seconds",
        poll_seconds,
    ]
    command = " ".join(shlex.quote(str(arg)) for arg in command_args)
    try:
        proc = subprocess.run(
            [*tmux_command_prefix(), "send-keys", "-t", str(pane_id), command, "Enter"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return record, {"started": False, "reused": False, "renderer": renderer, "reason": "tmux is not installed", "pane_id": pane_id}
    if proc.returncode != 0:
        return record, {
            "started": False,
            "reused": False,
            "renderer": renderer,
            "reason": proc.stderr.strip() or f"tmux send-keys exited {proc.returncode}",
            "pane_id": pane_id,
            "state_path": str(state_path),
        }

    deadline = time.monotonic() + 1.0
    state = None
    while time.monotonic() < deadline:
        state, _state_error = tmux_state.read_json(state_path)
        if isinstance(state, dict):
            pid = parse_pid(state.get("pid"))
            if pid_is_running(pid) and tmux_state.one_line_text(state.get("pane_id")) == pane_id:
                record["dashboard_viewer_pid"] = pid
                record["dashboard_viewer_heartbeat_at"] = state.get("heartbeat_at")
                break
        time.sleep(0.05)
    return record, {
        "started": True,
        "reused": False,
        "renderer": renderer,
        "pid": record.get("dashboard_viewer_pid"),
        "pane_id": pane_id,
        "state_path": str(state_path),
    }


def normalize_notify(
    mode: str,
    thread_id: str | None = None,
    endpoint: str | None = None,
    codex_pane: str | None = None,
) -> dict[str, Any]:
    if mode == "none":
        return {"mode": "none"}
    if mode == "tmux-inject":
        if not tmux_state.one_line_text(codex_pane):
            raise ValueError("manager start --notify tmux-inject requires --codex-pane")
        return {"mode": "tmux-inject", "codex_pane": str(codex_pane)}
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


def codex_sdk_model_name() -> str | None:
    return tmux_state.one_line_text(codex_sidecar_config().get("model")) or None


def codex_sdk_reasoning_effort() -> str:
    return tmux_state.token_text(codex_sidecar_config().get("reasoning_effort")) or DEFAULT_CODEX_SDK_REASONING_EFFORT


def codex_sidecar_fast_path_enabled() -> bool:
    return bool(codex_sidecar_config().get("deterministic_fast_path"))


def codex_sidecar_enabled() -> bool:
    override = tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SIDECAR"))
    if override:
        return override in {"1", "true", "yes", "on"}
    return bool(codex_sidecar_config().get("enabled"))


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def elapsed_ms(start_ms: float) -> int:
    return max(0, int(round(monotonic_ms() - start_ms)))


def timing_entry(start_ms: float, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"elapsed_ms": elapsed_ms(start_ms)}
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    return entry


def merge_timing(*items: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def codex_sidecar_venv_python(payload: dict[str, Any]) -> Path:
    state_dir = tmux_state.one_line_text(payload.get("state_dir"))
    workspace = tmux_state.one_line_text(payload.get("workspace")) or os.getcwd()
    root = Path(state_dir) if state_dir else tmux_state.state_paths(workspace, None)["root"]
    return root / "sidecar-venv" / "bin" / "python"


def ensure_codex_sidecar_venv(payload: dict[str, Any], *, timeout_seconds: float = 60.0) -> dict[str, Any]:
    python_path = codex_sidecar_venv_python(payload)
    marker_path = python_path.parent.parent / ".openai-codex-installed"
    if python_path.exists() and marker_path.exists():
        return {"ok": True, "python": str(python_path), "venv": str(python_path.parent.parent), "reused": True}
    venv_dir = python_path.parent.parent
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        create = subprocess.run(["uv", "venv", str(venv_dir)], text=True, capture_output=True, timeout=timeout_seconds)
    except Exception as exc:
        return {"ok": False, "reason": f"uv venv failed: {exc}", "python": str(python_path), "venv": str(venv_dir)}
    if create.returncode != 0:
        reason = create.stderr.strip() or create.stdout.strip() or f"uv venv exited {create.returncode}"
        return {"ok": False, "reason": tmux_state.one_line_text(reason), "python": str(python_path), "venv": str(venv_dir)}
    try:
        install = subprocess.run(
            ["uv", "pip", "install", "--python", str(python_path), "openai-codex"],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"uv pip install failed: {exc}", "python": str(python_path), "venv": str(venv_dir)}
    if install.returncode != 0:
        reason = install.stderr.strip() or install.stdout.strip() or f"uv pip install exited {install.returncode}"
        return {"ok": False, "reason": tmux_state.one_line_text(reason), "python": str(python_path), "venv": str(venv_dir)}
    marker_path.write_text(tmux_state.utc_now(), encoding="utf-8")
    return {"ok": True, "python": str(python_path), "venv": str(venv_dir), "reused": False}


def parse_codex_sidecar_json(output: str) -> dict[str, Any] | None:
    text = output.strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(line.strip() for line in reversed(text.splitlines()) if line.strip().startswith("{") and line.strip().endswith("}"))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def codex_sidecar_output_schema(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("terminal_assessment"):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "recommended_action", "confidence", "reason"],
            "properties": {
                "summary": {"type": "string"},
                "recommended_action": {"type": "string", "enum": ["wake_codex", "defer", "refuse"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        }
    if payload.get("allowed_actions"):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "submit_key", "confidence", "reason"],
            "properties": {
                "action": {"type": "string", "enum": ["confirmed", "submit", "defer", "refuse"]},
                "submit_key": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "target_pane", "confidence", "reason"],
        "properties": {
            "decision": {"type": "string", "enum": ["inject", "defer", "refuse"]},
            "target_pane": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }


def codex_sidecar_decision(payload: dict[str, Any], *, timeout_seconds: float = 90.0) -> dict[str, Any] | None:
    if not codex_sidecar_enabled():
        return None
    total_start = monotonic_ms()
    setup_start = monotonic_ms()
    setup = ensure_codex_sidecar_venv(payload)
    setup_timing = timing_entry(setup_start)
    if not setup.get("ok"):
        return {
            "source": "codex_sidecar_error",
            "reason": str(setup.get("reason") or "openai-codex sidecar venv setup failed"),
            "setup": setup,
            "timing": {"setup": setup_timing, "total": timing_entry(total_start)},
        }
    helper_path = script_dir() / "tmux_codex_sidecar.py"
    request_payload = {"payload": payload, "sidecar_config": codex_sidecar_config()}
    request = json.dumps(request_payload, sort_keys=True)
    call_start = monotonic_ms()
    try:
        proc = subprocess.run(
            [str(setup["python"]), str(helper_path)],
            input=request,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return {
            "source": "codex_sidecar_error",
            "reason": f"openai-codex sidecar helper failed: {exc}",
            "setup": setup,
            "timing": {"setup": setup_timing, "sdk_call": timing_entry(call_start), "total": timing_entry(total_start)},
        }
    call_timing = timing_entry(call_start, returncode=proc.returncode)
    parsed = parse_codex_sidecar_json(proc.stdout)
    total_timing = timing_entry(total_start)
    timing = {"setup": setup_timing, "sdk_call": call_timing, "total": total_timing}
    debug: dict[str, Any] | None = None
    if debug_sidecar_trace_enabled():
        debug = {
            "helper_returncode": proc.returncode,
            "helper_stdout_tail": bounded_debug_text(proc.stdout),
            "helper_stderr_tail": bounded_debug_text(proc.stderr),
        }
        if debug_sidecar_payload_enabled():
            debug["request"] = request_payload
    if proc.returncode != 0 or parsed is None:
        reason = proc.stderr.strip() or proc.stdout.strip() or f"openai-codex sidecar helper exited {proc.returncode}"
        result = {"source": "codex_sidecar_error", "reason": tmux_state.one_line_text(reason), "setup": setup, "timing": timing}
        if debug:
            result["sidecar_debug"] = debug
        return result
    if parsed.get("source") == "codex_sidecar_error":
        result = parsed | {"setup": setup, "timing": timing}
        if debug:
            result["sidecar_debug"] = debug
        return result
    output = str(parsed.get("output") or "")
    decision = parse_codex_sidecar_json(output)
    if decision is None:
        result = {
            "source": "codex_sidecar_error",
            "reason": tmux_state.one_line_text(output) or "openai-codex sidecar returned no decision JSON",
            "setup": setup,
            "timing": timing,
        }
        if debug:
            debug["final_response_tail"] = bounded_debug_text(output)
            result["sidecar_debug"] = debug
        return result
    result = decision | {"source": "codex_sidecar", "setup": setup, "timing": timing, "sidecar_config": codex_sidecar_config()}
    if debug:
        debug["final_response_tail"] = bounded_debug_text(output)
        result["sidecar_debug"] = debug
    return result


def tmux_inject_ack_recheck_seconds() -> float:
    value = os.environ.get("TMUX_SKILLS_TMUX_INJECT_ACK_RECHECK_SECONDS")
    if value is None:
        receipt = read_tmux_skills_config().get("tmux_inject_receipt")
        if isinstance(receipt, dict):
            value = receipt.get("recheck_seconds")
    if value is None:
        return TMUX_INJECT_ACK_RECHECK_SECONDS
    try:
        return max(0.0, float(value))
    except ValueError:
        return TMUX_INJECT_ACK_RECHECK_SECONDS


def tmux_inject_receipt_retry_max() -> int:
    value = os.environ.get("TMUX_SKILLS_TMUX_INJECT_RECEIPT_RETRY_MAX")
    if value is None:
        receipt = read_tmux_skills_config().get("tmux_inject_receipt")
        if isinstance(receipt, dict):
            value = receipt.get("max_retries")
    if value is None:
        return TMUX_INJECT_RECEIPT_RETRY_MAX
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return TMUX_INJECT_RECEIPT_RETRY_MAX


def tmux_inject_receipt_sidecar_check_max() -> int:
    value = os.environ.get("TMUX_SKILLS_TMUX_INJECT_RECEIPT_SIDECAR_CHECK_MAX")
    if value is None:
        receipt = read_tmux_skills_config().get("tmux_inject_receipt")
        if isinstance(receipt, dict):
            value = receipt.get("max_sidecar_checks")
    if value is None:
        return TMUX_INJECT_RECEIPT_SIDECAR_CHECK_MAX
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return TMUX_INJECT_RECEIPT_SIDECAR_CHECK_MAX


def tmux_inject_ack_recheck_due(notification: dict[str, Any] | None) -> bool:
    if not isinstance(notification, dict):
        return True
    if notification.get("acknowledged_by_codex"):
        return False
    if notification.get("mode") != "tmux-inject":
        return False
    if notification.get("status") == "deferred" or notification.get("requires_manual_resume"):
        return False
    if notification.get("status") not in {"awaiting_receipt", "injected", "inject_pending", "coalesced"}:
        return False
    delivery_check = notification.get("delivery_check") if isinstance(notification.get("delivery_check"), dict) else {}
    checked_at = (
        delivery_check.get("checked_at")
        or notification.get("submit_attempted_at")
        or notification.get("submitted_at")
        or notification.get("injected_at")
        or notification.get("observed_at")
    )
    age = tmux_state.age_seconds(checked_at)
    return age is None or age >= tmux_inject_ack_recheck_seconds()


def pending_tmux_inject_wake_event_id(record: dict[str, Any], current_event_id: str) -> str:
    for notification in record.get("notifications", []):
        if not isinstance(notification, dict):
            continue
        event_id = str(notification.get("event_id") or "")
        if not event_id or event_id == current_event_id:
            continue
        if notification.get("mode") != "tmux-inject":
            continue
        if notification.get("acknowledged_by_codex"):
            continue
        if notification.get("status") in {"queued_in_codex", "awaiting_receipt", "injected", "inject_pending", "receipt_blocked", "blocked_by_other_wake"} and notification.get("submitted_to_tmux"):
            return event_id
    return ""


def tmux_inject_wake_id(event_id: str) -> str:
    source = tmux_state.one_line_text(event_id) or "unknown"
    token = re.sub(r"[^0-9a-f]", "", source.lower())
    if len(token) >= 6:
        return token[:6]
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:6]


def tmux_inject_prompt_wake_id(prompt: str) -> str:
    for line in str(prompt or "").splitlines()[:3]:
        match = TMUX_INJECT_WAKE_ID_PATTERN.search(tmux_text.strip_ansi(line))
        if match:
            return match.group(1)
    return ""


def tmux_inject_capture_wake_ids(capture_output: str) -> list[str]:
    ids: list[str] = []
    for line in str(capture_output or "").splitlines()[-30:]:
        for match in TMUX_INJECT_WAKE_ID_PATTERN.finditer(tmux_text.strip_ansi(line)):
            wake_id = match.group(1)
            if wake_id not in ids:
                ids.append(wake_id)
    return ids


def tmux_inject_wake_records(record: dict[str, Any] | None, wake_id: str) -> list[dict[str, Any]]:
    if not isinstance(record, dict) or not wake_id:
        return []
    refs: list[dict[str, Any]] = []
    for notification in record.get("notifications", []):
        if isinstance(notification, dict) and str(notification.get("wake_id") or "") == wake_id:
            refs.append({"kind": "notification", "record": notification, "event_id": str(notification.get("event_id") or "")})
    events = record.get("events") if isinstance(record.get("events"), dict) else {}
    for event_id, event in events.items():
        if isinstance(event, dict) and str(event.get("wake_id") or "") == wake_id:
            refs.append({"kind": "event", "record": event, "event_id": str(event_id or event.get("event_id") or "")})
    return refs


def tmux_inject_wake_ref_is_stale(ref: dict[str, Any]) -> bool:
    item = ref.get("record") if isinstance(ref.get("record"), dict) else {}
    status = tmux_state.token_text(item.get("status") or item.get("notification_status"))
    return bool(
        item.get("acknowledged_by_codex")
        or item.get("handled_at")
        or item.get("handled_by_job_id")
        or item.get("handled_without_ack")
        or status in {"acknowledged", "handled"}
    )


def tmux_inject_classify_visible_wake_ids(
    record: dict[str, Any] | None,
    *,
    current_wake_id: str,
    visible_wake_ids: list[str],
) -> dict[str, Any]:
    same = [value for value in visible_wake_ids if value == current_wake_id]
    active_other: list[str] = []
    stale_or_handled: list[str] = []
    unknown: list[str] = []
    for wake_id in visible_wake_ids:
        if wake_id == current_wake_id:
            continue
        refs = tmux_inject_wake_records(record, wake_id)
        if not refs:
            unknown.append(wake_id)
        elif all(tmux_inject_wake_ref_is_stale(ref) for ref in refs):
            stale_or_handled.append(wake_id)
        else:
            active_other.append(wake_id)
    blocking = [*active_other, *unknown]
    return {
        "wake_id": current_wake_id,
        "visible_wake_ids": visible_wake_ids,
        "same_wake_ids": same,
        "same_wake_visible": bool(same),
        "active_other_wake_ids": active_other,
        "stale_or_handled_wake_ids": stale_or_handled,
        "unknown_wake_ids": unknown,
        "other_wake_ids": blocking,
    }


def tmux_inject_wake_visibility(prompt: str, capture_output: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
    wake_id = tmux_inject_prompt_wake_id(prompt)
    visible = tmux_inject_capture_wake_ids(capture_output)
    return tmux_inject_classify_visible_wake_ids(record, current_wake_id=wake_id, visible_wake_ids=visible)


def codex_capture_indicates_working(capture_output: str) -> bool:
    for line in capture_output.splitlines()[-30:]:
        stripped = tmux_text.strip_ansi(line)
        if "Working" in stripped and "esc to interrupt" in stripped:
            return True
    return False


def normalize_tmux_inject_receipt_sidecar_decision(value: Any, *, retry_count: int, sidecar_check_count: int) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    action = tmux_state.token_text(payload.get("action")) or "wait"
    if action not in {"retry", "wait", "block"}:
        action = "wait"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    status = "awaiting_receipt" if action in {"retry", "wait"} else "receipt_blocked"
    return {
        "action": action,
        "status": status,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": tmux_state.one_line_text(payload.get("reason")) or "Codex sidecar receipt recovery decision",
        "retry_count": retry_count,
        "sidecar_check_count": sidecar_check_count,
    }


def codex_sdk_receipt_recheck_decision(
    record: dict[str, Any],
    candidate: dict[str, Any],
    capture: dict[str, Any],
    composer_state: dict[str, Any],
    *,
    retry_count: int,
    sidecar_check_count: int,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    sidecar = codex_sidecar_decision(
        {
            "task": "Judge receipt recovery for a tmux-inject wake prompt after no manager ack was observed.",
            "allowed_actions": ["retry", "wait", "block"],
            "workspace": record.get("workspace"),
            "state_dir": record.get("state_dir"),
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "job_id": candidate.get("job_id"),
            "bound_pane_id": record.get("codex_pane_id"),
            "receipt_recovery": True,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
            "max_retries": tmux_inject_receipt_retry_max(),
            "max_sidecar_checks": tmux_inject_receipt_sidecar_check_max(),
            "composer_state": composer_state,
            "pane_capture_tail": receipt_sidecar_pane_capture_tail(capture, composer_state),
            "rules": [
                "Return JSON with action, submit_key, confidence, and reason.",
                "Choose retry only when the prior wake prompt appears lost and the Codex pane is idle/empty.",
                "Choose wait when Codex appears busy/working, a prompt may be queued, or the state is uncertain.",
                "Choose block when user text or unrelated composer content is visible, or retry/check limits are reached.",
                "If composer_state.status is placeholder_composer_suggestion, treat composer_preview as idle placeholder UI, not user text.",
                "Never block only because placeholder text such as Explain this codebase is visible.",
                "Never choose a different pane or suggest shell commands.",
            ],
        },
        timeout_seconds=timeout_seconds,
    )
    if sidecar and sidecar.get("source") == "codex_sidecar":
        decision = normalize_tmux_inject_receipt_sidecar_decision(sidecar, retry_count=retry_count, sidecar_check_count=sidecar_check_count) | {
            "source": "codex_sidecar",
            "sidecar_setup": sidecar.get("setup"),
            "timing": sidecar.get("timing"),
            "sidecar_config": sidecar.get("sidecar_config") or codex_sidecar_config(),
            "sidecar_debug": sidecar.get("sidecar_debug"),
        }
        if decision.get("action") == "block" and composer_state.get("status") == "placeholder_composer_suggestion" and composer_state.get("safe_to_inject"):
            decision = decision | {
                "action": "wait",
                "status": "awaiting_receipt",
                "reason": "sidecar block overridden: composer_state is safe placeholder UI, not user text",
                "sidecar_overridden_action": "block",
                "sidecar_original_reason": decision.get("reason"),
            }
        return decision
    return {
        "action": "wait",
        "status": "awaiting_receipt",
        "confidence": 0.0,
        "reason": "Codex sidecar receipt decision unavailable; waiting instead of retrying",
        "retry_count": retry_count,
        "sidecar_check_count": sidecar_check_count,
        "source": "codex_sidecar_unavailable",
        "sidecar": sidecar,
        "sidecar_debug": sidecar.get("sidecar_debug") if isinstance(sidecar, dict) else None,
    }


def receipt_sidecar_history_entry(receipt_check: dict[str, Any], *, checked_at: str) -> dict[str, Any] | None:
    if not debug_sidecar_trace_enabled():
        return None
    source = tmux_state.one_line_text(receipt_check.get("source"))
    reason = tmux_state.one_line_text(receipt_check.get("reason"))
    sidecar_related = source.startswith("codex_sidecar") or "sidecar" in reason.lower() or receipt_check.get("sidecar_config") is not None
    if not sidecar_related:
        return None
    entry: dict[str, Any] = {
        "checked_at": checked_at,
        "action": tmux_state.token_text(receipt_check.get("action")) or "wait",
        "status": tmux_state.token_text(receipt_check.get("status")) or "awaiting_receipt",
        "reason": reason,
        "source": source or "deterministic",
        "confidence": receipt_check.get("confidence"),
        "retry_count": receipt_check.get("retry_count"),
        "sidecar_check_count": receipt_check.get("next_sidecar_check_count", receipt_check.get("sidecar_check_count")),
    }
    for key in ("timing", "sidecar_config", "composer_state", "sidecar_debug"):
        if receipt_check.get(key) is not None:
            entry[key] = receipt_check.get(key)
    sidecar = receipt_check.get("sidecar")
    if isinstance(sidecar, dict):
        entry["sidecar"] = {
            key: sidecar.get(key)
            for key in ("source", "reason", "timing", "sidecar_config", "sidecar_debug")
            if sidecar.get(key) is not None
        }
    return entry


def append_receipt_sidecar_history(existing: dict[str, Any], receipt_check: dict[str, Any], *, checked_at: str) -> list[dict[str, Any]] | None:
    entry = receipt_sidecar_history_entry(receipt_check, checked_at=checked_at)
    if entry is None:
        return None
    history = existing.get("receipt_sidecar_history")
    values = [dict(item) for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    values.append(entry)
    return values[-50:]


def tmux_inject_receipt_recheck_decision(
    prompt: str,
    capture: dict[str, Any],
    existing: dict[str, Any],
    *,
    record: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = str(capture.get("output") or "")
    raw_output = str(capture.get("raw_output") or "")
    try:
        retry_count = int(existing.get("receipt_retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    try:
        sidecar_check_count = int(existing.get("receipt_sidecar_check_count") or 0)
    except (TypeError, ValueError):
        sidecar_check_count = 0
    composer_state = tmux_inject_composer_state(prompt, output, raw_output, record=record)
    if not capture.get("captured"):
        return {
            "action": "wait",
            "status": "awaiting_receipt",
            "reason": tmux_state.one_line_text(capture.get("reason")) or "receipt recheck could not capture Codex pane",
            "composer_state": composer_state,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
        }
    if wake_prompt_still_staged(prompt, output):
        return {
            "action": "delivery_check",
            "status": "inject_pending",
            "reason": "wake prompt remains staged; bounded submit follow-up may be needed",
            "composer_state": composer_state,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
        }
    wake_visibility = tmux_inject_wake_visibility(prompt, output, record=record)
    if wake_visibility.get("other_wake_ids"):
        return {
            "action": "block",
            "status": "blocked_by_other_wake",
            "reason": f"another tmux-skills wake prompt is already visible: {', '.join(wake_visibility['other_wake_ids'])}",
            "composer_state": composer_state,
            "wake_visibility": wake_visibility,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
            "requires_manual_resume": True,
        }
    if wake_visibility.get("same_wake_visible"):
        return {
            "action": "wait",
            "status": "queued_in_codex",
            "reason": "the same tmux-skills wake prompt is already visible in Codex; not reinjecting",
            "composer_state": composer_state,
            "wake_visibility": wake_visibility,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
        }
    if codex_capture_indicates_working(output):
        return {
            "action": "wait",
            "status": "awaiting_receipt",
            "reason": "Codex pane appears to be working; waiting for manager ack",
            "composer_state": composer_state,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
        }
    if composer_state.get("safe_to_inject"):
        retry_max = tmux_inject_receipt_retry_max()
        if retry_count >= retry_max:
            return {
                "action": "block",
                "status": "receipt_blocked",
                "reason": f"receipt retry limit reached ({retry_max})",
                "composer_state": composer_state,
                "retry_count": retry_count,
                "sidecar_check_count": sidecar_check_count,
            }
        sidecar_max = tmux_inject_receipt_sidecar_check_max()
        if sidecar_check_count >= sidecar_max:
            return {
                "action": "block",
                "status": "receipt_blocked",
                "reason": f"receipt sidecar check limit reached ({sidecar_max})",
                "composer_state": composer_state,
                "retry_count": retry_count,
                "sidecar_check_count": sidecar_check_count,
            }
        if record is None or candidate is None:
            return {
                "action": "wait",
                "status": "awaiting_receipt",
                "reason": "receipt recovery requires Codex sidecar context before retrying",
                "composer_state": composer_state,
                "retry_count": retry_count,
                "sidecar_check_count": sidecar_check_count,
                "next_sidecar_check_count": sidecar_check_count + 1,
            }
        sidecar_decision = codex_sdk_receipt_recheck_decision(
            record,
            candidate,
            capture,
            composer_state,
            retry_count=retry_count,
            sidecar_check_count=sidecar_check_count + 1,
        )
        result = sidecar_decision | {
            "composer_state": composer_state,
            "retry_count": retry_count,
            "sidecar_check_count": sidecar_check_count,
            "next_sidecar_check_count": sidecar_check_count + 1,
        }
        if result.get("action") == "retry":
            result["next_retry_count"] = retry_count + 1
        return result
    return {
        "action": "block",
        "status": "receipt_blocked",
        "reason": tmux_state.one_line_text(composer_state.get("reason")) or "Codex composer is not safe for receipt retry",
        "composer_state": composer_state,
        "retry_count": retry_count,
        "sidecar_check_count": sidecar_check_count,
    }


def pending_followup_decision(reason: str, *, prompt: str, capture_output: str, source: str) -> dict[str, Any]:
    action = "submit" if wake_prompt_still_staged(prompt, capture_output) else "confirmed"
    confidence = 0.85 if source == "deterministic" else 0.0
    return normalize_tmux_inject_followup_decision(
        {"action": action, "submit_key": default_tmux_inject_followup_submit_key(prompt, capture_output), "confidence": confidence, "reason": reason},
        prompt=prompt,
        capture_output=capture_output,
    ) | {"source": source}


def bridge_notify_identity(record: dict[str, Any]) -> dict[str, str]:
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {}
    return {
        "manager_id": str(record.get("manager_id") or ""),
        "workspace": str(record.get("workspace") or ""),
        "endpoint": str(notify.get("endpoint") or ""),
        "thread_id": str(notify.get("thread_id") or ""),
    }


def bridge_verification_matches(record: dict[str, Any], verification: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(record.get("notify"), dict) or record["notify"].get("mode") != "bridge":
        return False, "manager notify mode is not bridge"
    expected = bridge_notify_identity(record)
    for key, value in expected.items():
        if str(verification.get(key) or "") != value:
            return False, f"bridge verification {key} does not match current manager"
    return True, None


def normalize_bridge_verification(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("bridge_verification")
    verification = dict(raw) if isinstance(raw, dict) else {}
    status = tmux_state.token_text(verification.get("status")) or "unverified"
    if status not in BRIDGE_VERIFICATION_STATUSES:
        status = "unverified"
    verification["status"] = status
    verification.setdefault("event_id", None)
    verification.setdefault("mode", "bridge")
    verification.setdefault("manager_id", str(record.get("manager_id") or ""))
    verification.setdefault("workspace", str(record.get("workspace") or ""))
    verification.setdefault("endpoint", None)
    verification.setdefault("thread_id", None)
    verification.setdefault("prompt_sha256", None)
    verification.setdefault("submitted_to_app_server", False)
    verification.setdefault("acknowledged_by_codex", False)
    verification.setdefault("submitted_at", None)
    verification.setdefault("acknowledged_at", None)
    verification.setdefault("ack_turn_id", None)
    verification.setdefault("expires_at", None)
    if verification.get("expires_at") and (tmux_state.age_seconds(verification.get("expires_at")) or 0) > 0:
        verification["status"] = "expired"
    if verification["status"] in {"awaiting_ack", "verified", "submission_failed"}:
        matches, reason = bridge_verification_matches(record, verification)
        if not matches:
            verification["status"] = "mismatched_config"
            verification["acknowledged_by_codex"] = False
            verification["mismatch_reason"] = reason
    return verification


def bridge_receipt_verified(record: dict[str, Any]) -> tuple[bool, str | None]:
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    if notify.get("mode") != "bridge":
        return True, None
    verification = normalize_bridge_verification(record)
    status = verification.get("status") or "unverified"
    if status != "verified" or not verification.get("acknowledged_by_codex"):
        if status == "mismatched_config":
            _matches, reason = bridge_verification_matches(record, verification)
            return False, reason or "bridge receipt is not verified: mismatched_config"
        return False, f"bridge receipt is not verified: {status}"
    matches, reason = bridge_verification_matches(record, verification)
    if not matches:
        return False, reason
    return True, None


def terminal_event_acknowledged(record: dict[str, Any]) -> tuple[bool, str | None]:
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    if notify.get("mode") not in {"bridge", "tmux-inject"}:
        return True, None
    event_ids = unacknowledged_terminal_event_ids(record)
    if not event_ids and record.get("status") != "waiting_for_codex":
        return True, None
    if not event_ids:
        event_id = str(record.get("last_terminal_event_id") or "")
        if not event_id:
            return True, None
        notification = notification_for_event(record, event_id)
        if notification and notification.get("acknowledged_by_codex"):
            return True, None
        return False, f"last terminal event has not been acknowledged by Codex: {event_id}"
    event_id = event_ids[0]
    return False, f"last terminal event has not been acknowledged by Codex: {event_id}"


def manager_queue_gate(record: dict[str, Any]) -> tuple[bool, str | None]:
    if manager_cancel_state(record):
        if record.get("status") == "cancel_requested":
            return False, "manager cancellation is requested"
        return False, "manager is cancelled"
    verified, reason = bridge_receipt_verified(record)
    if not verified:
        return False, reason
    acknowledged, reason = terminal_event_acknowledged(record)
    if not acknowledged:
        return False, reason
    return True, None


def default_notify_route(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {
            "action": "start_bridge_check",
            "allowed": True,
            "reason": "no manager is running; start a background manager and run manager bridge-check before submit",
        }
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    if notify.get("mode") != "bridge":
        return {
            "action": "refuse",
            "allowed": False,
            "reason": "notify-mode work requires a bridge manager; --notify none is diagnostics-only",
        }
    allowed, reason = manager_queue_gate(record)
    if not allowed:
        return {"action": "refuse", "allowed": False, "reason": reason}
    return {"action": "submit", "allowed": True, "reason": None}


def unique_text_values(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = tmux_state.one_line_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def optional_safe_id(value: Any) -> str:
    text = tmux_state.one_line_text(value)
    return tmux_state.safe_id(str(text)) if text else ""


def job_pane_id(record: dict[str, Any], job_id: str) -> str:
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
    return str(job.get("pane_id") or record.get("worker_pane_id") or "")


def job_pane_index(record: dict[str, Any], job_id: str) -> str:
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
    pane_id = tmux_state.one_line_text(job.get("pane_id") or record.get("worker_pane_id"))
    if job.get("pane_index") is not None:
        return tmux_state.one_line_text(job.get("pane_index"))
    if pane_id == tmux_state.one_line_text(record.get("worker_pane_id")):
        return tmux_state.one_line_text(record.get("worker_pane_index"))
    if pane_id == tmux_state.one_line_text(record.get("manager_pane_id")):
        return tmux_state.one_line_text(record.get("manager_pane_index"))
    return ""


def active_job_ids(record: dict[str, Any]) -> list[str]:
    active = record.get("active_job_ids")
    if isinstance(active, list):
        return [optional_safe_id(value) for value in active if tmux_state.one_line_text(value)]
    current_job_id = optional_safe_id(record.get("current_job_id"))
    if current_job_id and record.get("status") == "running":
        return [current_job_id]
    return []


def pane_has_active_job(record: dict[str, Any], pane_id: str, *, excluding_job_id: str | None = None) -> bool:
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return False
    exclude = optional_safe_id(excluding_job_id)
    for job_id in active_job_ids(record):
        if exclude and job_id == exclude:
            continue
        if job_pane_id(record, job_id) == target:
            return True
    return False


def unacknowledged_terminal_event_ids(record: dict[str, Any]) -> list[str]:
    events = record.get("events") if isinstance(record.get("events"), dict) else {}
    result: list[str] = []
    for event_id, event in events.items():
        if not isinstance(event, dict):
            continue
        if event.get("source") not in {"manager_terminal", "manager_worker_missing"}:
            continue
        notification = notification_for_event(record, str(event_id))
        acknowledged = bool(event.get("acknowledged_by_codex")) or bool(
            notification and notification.get("acknowledged_by_codex")
        )
        if not acknowledged:
            result.append(str(event_id))
    return result


def refresh_aggregate_status(record: dict[str, Any]) -> dict[str, Any]:
    if manager_cancel_state(record) or record.get("status") == "failed":
        return record
    if record.get("pending_job"):
        record["status"] = "queued"
    elif unacknowledged_terminal_event_ids(record):
        record["status"] = "waiting_for_codex"
    elif active_job_ids(record):
        record["status"] = "running"
    else:
        record["status"] = "idle"
    return record


def manager_cancel_state(record: dict[str, Any] | None) -> bool:
    return bool(record) and str(record.get("status") or "") in MANAGER_CANCEL_STATUSES


def merge_external_cancel_state(record: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    if not manager_cancel_state(latest):
        return record
    merged = dict(record)
    if latest.get("cancel_requested_at") and not merged.get("cancel_requested_at"):
        merged["cancel_requested_at"] = latest["cancel_requested_at"]
    merged["stop_worker_requested"] = bool(record.get("stop_worker_requested") or latest.get("stop_worker_requested"))
    if record.get("worker_stop_result") is None and latest.get("worker_stop_result") is not None:
        merged["worker_stop_result"] = latest["worker_stop_result"]
    if not record.get("worker_stop_results") and latest.get("worker_stop_results"):
        merged["worker_stop_results"] = latest["worker_stop_results"]
    if latest.get("cancel_job_id") and not merged.get("cancel_job_id"):
        merged["cancel_job_id"] = latest["cancel_job_id"]
    merged["all_workers_stop_requested"] = bool(
        record.get("all_workers_stop_requested") or latest.get("all_workers_stop_requested")
    )
    merged["status"] = "cancelled" if record.get("status") == "cancelled" else latest.get("status")
    if latest.get("pending_job") is None:
        merged["pending_job"] = None
    return merged


def preserve_external_cancel_state(paths: dict[str, Path], record: dict[str, Any]) -> dict[str, Any]:
    manager_id = str(record.get("manager_id") or "")
    if not manager_id:
        return record
    latest, _latest_error = read_manager_record(paths, manager_id)
    return merge_external_cancel_state(record, latest)


def normalize_manager_record(record: dict[str, Any], paths: dict[str, Path] | None = None, path: Path | None = None) -> dict[str, Any]:
    normalized = dict(record)
    manager_id = manager_id_value(str(path.stem if path else normalized.get("manager_id")))
    normalized["version"] = int(normalized.get("version") or MANAGER_VERSION)
    normalized["manager_id"] = manager_id
    normalized["status"] = tmux_state.token_text(normalized.get("status")) or "starting"
    if normalized["status"] not in MANAGER_STATUSES:
        normalized["status"] = "failed"
    normalized.setdefault("manager_pane_id", None)
    normalized["manager_pane_index"] = tmux_state.one_line_text(normalized.get("manager_pane_index"))
    normalized.setdefault("worker_pane_id", None)
    normalized["worker_pane_index"] = tmux_state.one_line_text(normalized.get("worker_pane_index"))
    normalized.setdefault("current_job_id", None)
    job_ids = normalized.get("job_ids")
    normalized["job_ids"] = [tmux_state.safe_id(str(value)) for value in job_ids] if isinstance(job_ids, list) else []
    normalized.setdefault("notify", {"mode": "none"})
    notify = normalized.get("notify") if isinstance(normalized.get("notify"), dict) else {"mode": "none"}
    notify_codex_pane = tmux_state.one_line_text(notify.get("codex_pane_id") or notify.get("codex_pane"))
    record_codex_pane = tmux_state.one_line_text(normalized.get("codex_pane_id"))
    if notify.get("mode") == "tmux-inject":
        normalized["codex_pane_id"] = record_codex_pane or notify_codex_pane
        if normalized["codex_pane_id"]:
            notify = dict(notify) | {"codex_pane_id": normalized["codex_pane_id"]}
            normalized["notify"] = notify
    else:
        normalized.setdefault("codex_pane_id", record_codex_pane)
    normalized.setdefault("heartbeat_at", None)
    normalized.setdefault("last_terminal_event_id", None)
    normalized.setdefault("workspace", str(paths["workspace"]) if paths else None)
    normalized.setdefault("state_dir", str(paths["root"]) if paths else None)
    normalized.setdefault("created_at", normalized.get("updated_at") or tmux_state.utc_now())
    normalized.setdefault("updated_at", normalized.get("created_at"))
    normalized.setdefault("pending_job", None)
    raw_jobs = normalized.get("jobs")
    jobs: dict[str, dict[str, Any]] = {}
    if isinstance(raw_jobs, dict):
        for key, value in raw_jobs.items():
            job_id = tmux_state.safe_id(str(key))
            if not job_id:
                continue
            job = dict(value) if isinstance(value, dict) else {}
            job["job_id"] = tmux_state.safe_id(str(job.get("job_id") or job_id))
            if not job.get("pane_id") and job_id == str(normalized.get("current_job_id") or ""):
                job["pane_id"] = normalized.get("worker_pane_id")
            jobs[job_id] = job
            if job_id not in normalized["job_ids"]:
                normalized["job_ids"].append(job_id)
    normalized["jobs"] = jobs
    current_job_id = optional_safe_id(normalized.get("current_job_id"))
    if current_job_id and current_job_id not in normalized["job_ids"]:
        normalized["job_ids"].append(current_job_id)
    worker_pane_values: list[Any] = []
    worker_pane_values.extend(normalized.get("worker_pane_ids") if isinstance(normalized.get("worker_pane_ids"), list) else [])
    if normalized.get("worker_pane_id"):
        worker_pane_values.append(normalized.get("worker_pane_id"))
    for job in jobs.values():
        if job.get("pane_id"):
            worker_pane_values.append(job.get("pane_id"))
    normalized["worker_pane_ids"] = unique_text_values(worker_pane_values)
    active_values = normalized.get("active_job_ids") if isinstance(normalized.get("active_job_ids"), list) else []
    if current_job_id and normalized["status"] == "running" and current_job_id not in active_values:
        active_values = list(active_values) + [current_job_id]
    normalized["job_ids"] = unique_text_values(normalized["job_ids"])
    normalized["active_job_ids"] = unique_text_values(
        [optional_safe_id(value) for value in active_values if tmux_state.one_line_text(value)]
    )
    raw_events = normalized.get("events")
    events: dict[str, dict[str, Any]] = {}
    if isinstance(raw_events, dict):
        for key, value in raw_events.items():
            event_id = tmux_state.one_line_text(key)
            if event_id and isinstance(value, dict):
                events[event_id] = dict(value) | {"event_id": event_id}
    normalized["events"] = events
    notified_event_ids = normalized.get("notified_event_ids")
    normalized["notified_event_ids"] = [str(value) for value in notified_event_ids] if isinstance(notified_event_ids, list) else []
    submitted_event_ids = normalized.get("submitted_event_ids")
    if isinstance(submitted_event_ids, list):
        normalized["submitted_event_ids"] = [str(value) for value in submitted_event_ids]
    else:
        normalized["submitted_event_ids"] = list(normalized["notified_event_ids"])
    notifications = normalized.get("notifications")
    normalized["notifications"] = [dict(value) for value in notifications if isinstance(value, dict)] if isinstance(notifications, list) else []
    normalized.setdefault("last_notification", None)
    normalized.setdefault("last_ack", None)
    normalized.setdefault("last_error", None)
    normalized.setdefault("dashboard_path", str(manager_dashboard_path(paths, manager_id)) if paths else None)
    normalized["manager_process_mode"] = manager_process_mode_value(str(normalized.get("manager_process_mode") or "foreground"))
    normalized["dashboard_renderer"] = manager_dashboard_renderer_value(str(normalized.get("dashboard_renderer") or "pane"))
    normalized.setdefault(
        "dashboard_viewer_state_path",
        str(manager_dashboard_viewer_state_path(paths, manager_id)) if paths else None,
    )
    normalized["dashboard_viewer_pid"] = parse_pid(normalized.get("dashboard_viewer_pid"))
    normalized.setdefault("dashboard_viewer_heartbeat_at", None)
    normalized.setdefault("manager_pid", None)
    normalized.setdefault("manager_launcher", "foreground-codex-command")
    normalized.setdefault("manager_exit_watch", "foreground-command-lifetime")
    normalized.setdefault("manager_dashboard_owner", "manager-loop")
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
        normalized["dashboard_viewer_state_path"] = str(manager_dashboard_viewer_state_path(paths, manager_id))
    else:
        normalized.setdefault("manager_path", str(path) if path else None)
    normalized["bridge_verification"] = normalize_bridge_verification(normalized)
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
    current, _error = tmux_state.read_json(manager_record_path(paths, normalized["manager_id"]))
    normalized = merge_external_cancel_state(normalized, current)
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


def build_pending_job(
    job_id: str,
    command_request_path: Path,
    cwd: str | None = None,
    pane_id: str | None = None,
    pane_index: str | None = None,
    manager_sequence: int | None = None,
) -> dict[str, Any]:
    pending = {
        "job_id": tmux_state.safe_id(job_id),
        "command_file": str(command_request_path),
        "cwd": cwd,
        "pane_id": tmux_state.one_line_text(pane_id),
        "pane_index": tmux_state.one_line_text(pane_index),
        "queued_at": tmux_state.utc_now(),
    }
    if manager_sequence is not None:
        pending["manager_sequence"] = manager_sequence
    return pending


def manager_controlled_response(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("codex_next_action", "wait_for_next_manager_event")
    result.setdefault("manager_controlled_reminder", MANAGER_CONTROLLED_NEXT_REMINDER)
    return result


def build_manager_record(
    *,
    manager_id: str,
    manager_pane_id: str,
    worker_pane_id: str,
    manager_pane_index: str | None = None,
    worker_pane_index: str | None = None,
    pending_job: dict[str, Any] | None,
    notify: dict[str, Any],
    codex_pane_id: str | None = None,
    workspace: str,
    state_dir: str,
    attach_command: str | None = None,
    poll_seconds: float = 2.0,
    log_max_bytes: int = DEFAULT_MANAGER_LOG_MAX_BYTES,
    process_mode: str = "foreground",
    dashboard_renderer: str = "pane",
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    now = tmux_state.utc_now()
    launcher, exit_watch = manager_launcher_for_mode(process_mode)
    return normalize_manager_record(
        {
            "version": MANAGER_VERSION,
            "manager_id": manager_id,
            "status": "queued" if pending_job else "idle",
            "manager_pane_id": manager_pane_id,
            "manager_pane_index": tmux_state.one_line_text(manager_pane_index),
            "worker_pane_id": worker_pane_id,
            "worker_pane_index": tmux_state.one_line_text(worker_pane_index),
            "worker_pane_ids": [worker_pane_id] if worker_pane_id else [],
            "current_job_id": None,
            "active_job_ids": [],
            "job_ids": [],
            "notify": notify,
            "codex_pane_id": tmux_state.one_line_text(codex_pane_id),
            "heartbeat_at": None,
            "last_terminal_event_id": None,
            "workspace": str(paths["workspace"]),
            "state_dir": str(paths["root"]),
            "created_at": now,
            "updated_at": now,
            "pending_job": pending_job,
            "jobs": {},
            "events": {},
            "notified_event_ids": [],
            "submitted_event_ids": [],
            "notifications": [],
            "last_notification": None,
            "last_ack": None,
            "last_error": None,
            "dashboard_path": str(manager_dashboard_path(paths, manager_id)),
            "dashboard_renderer": manager_dashboard_renderer_value(dashboard_renderer),
            "dashboard_viewer_pid": None,
            "dashboard_viewer_state_path": str(manager_dashboard_viewer_state_path(paths, manager_id)),
            "dashboard_viewer_heartbeat_at": None,
            "manager_process_mode": manager_process_mode_value(process_mode),
            "manager_pid": os.getpid(),
            "manager_launcher": launcher,
            "manager_exit_watch": exit_watch,
            "manager_dashboard_owner": "manager-loop",
            "manager_process_started_at": now,
            "attach_command": attach_command,
            "poll_seconds": poll_seconds,
            "log_max_bytes": log_max_bytes,
        },
        paths,
    )


def append_unique_text(record: dict[str, Any], field: str, value: str) -> None:
    values = [tmux_state.one_line_text(item) for item in record.get(field, []) if tmux_state.one_line_text(item)]
    if value not in values:
        values.append(value)
    record[field] = values


def notification_for_event(record: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    for notification in record.get("notifications", []):
        if isinstance(notification, dict) and str(notification.get("event_id") or "") == event_id:
            return dict(notification)
    return None


def upsert_notification(record: dict[str, Any], event_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    notifications: list[dict[str, Any]] = []
    updated: dict[str, Any] | None = None
    for value in record.get("notifications", []):
        if not isinstance(value, dict):
            continue
        item = dict(value)
        if str(item.get("event_id") or "") == event_id:
            item.update(fields)
            updated = item
        notifications.append(item)
    if updated is None:
        updated = dict(fields)
        updated["event_id"] = event_id
        notifications.append(updated)
    record["notifications"] = notifications
    record["last_notification"] = updated
    return record


def sync_event_from_notification(record: dict[str, Any], event_id: str, notification: dict[str, Any]) -> dict[str, Any]:
    events = dict(record.get("events") or {})
    event = dict(events.get(event_id) or {})
    fields: dict[str, Any] = {"event_id": event_id}
    for key in (
        "acknowledged_by_codex",
        "acknowledged_at",
        "ack_turn_id",
        "ack_note",
        "handled_at",
        "handled_by_job_id",
        "handled_without_ack",
        "submitted_to_app_server",
        "submitted_to_tmux",
        "injected_to_tmux",
        "submitted_at",
        "notification_status",
        "wake_id",
        "codex_pane_id",
        "last_error",
        "terminal_assessment",
        "notification_phase",
        "blocked_at",
        "blocked_reason",
        "deferred_at",
        "defer_reason",
        "requires_manual_resume",
    ):
        if key in notification:
            fields[key] = notification[key]
    if "status" in notification and "notification_status" not in fields:
        fields["notification_status"] = notification["status"]
    events[event_id] = event | fields
    record["events"] = events
    return record


def tmux_inject_defer_reason_from_injection(injection: dict[str, Any] | None) -> str:
    if not isinstance(injection, dict):
        return ""
    preflight = injection.get("preflight") if isinstance(injection.get("preflight"), dict) else {}
    composer_state = preflight.get("composer_state") if isinstance(preflight.get("composer_state"), dict) else {}
    status = tmux_state.token_text(composer_state.get("status"))
    if status in {"composer_text_present", "other_wake_prompt_staged"}:
        return tmux_state.one_line_text(composer_state.get("reason")) or tmux_state.one_line_text(injection.get("reason"))
    if composer_state and composer_state.get("safe_to_inject") is False:
        return tmux_state.one_line_text(composer_state.get("reason")) or tmux_state.one_line_text(injection.get("reason"))
    return ""


def apply_deferred_notification_fields(fields: dict[str, Any], *, reason: str, now: str) -> dict[str, Any]:
    fields["status"] = "deferred"
    fields["notification_phase"] = "deferred"
    fields["deferred_at"] = now
    fields["defer_reason"] = reason
    fields["reason"] = reason
    fields["requires_manual_resume"] = True
    return fields


def tmux_inject_status_from_blocked_injection(injection: dict[str, Any] | None) -> tuple[str, str | None]:
    if not isinstance(injection, dict):
        return "", None
    preflight = injection.get("preflight") if isinstance(injection.get("preflight"), dict) else {}
    composer_state = preflight.get("composer_state") if isinstance(preflight.get("composer_state"), dict) else {}
    status = tmux_state.token_text(composer_state.get("status"))
    reason = tmux_state.one_line_text(composer_state.get("reason")) or tmux_state.one_line_text(injection.get("reason"))
    if status == "same_wake_prompt_visible":
        return "queued_in_codex", reason or "the same tmux-skills wake prompt is already visible in Codex"
    if status == "other_wake_prompt_staged":
        return "blocked_by_other_wake", reason or "a different tmux-skills wake prompt is already visible in Codex"
    return "", None


def mark_last_terminal_event_handled(record: dict[str, Any], *, next_job_id: str) -> dict[str, Any]:
    event_id = str(record.get("last_terminal_event_id") or "")
    if not event_id:
        return record
    notification = notification_for_event(record, event_id)
    if notification is None:
        return record
    now = tmux_state.utc_now()
    acknowledged = bool(notification.get("acknowledged_by_codex"))
    record = upsert_notification(
        record,
        event_id,
        {
            "status": "handled" if acknowledged else "handled_without_ack",
            "handled_at": now,
            "handled_by_job_id": next_job_id,
            "handled_without_ack": not acknowledged,
        },
    )
    events = dict(record.get("events") or {})
    if event_id in events and isinstance(events[event_id], dict):
        events[event_id] = dict(events[event_id]) | {
            "handled_at": now,
            "handled_by_job_id": next_job_id,
            "handled_without_ack": not acknowledged,
        }
        record["events"] = events
    return record


def bridge_check_event_id(record: dict[str, Any], observed_at: str) -> str:
    payload = bridge_notify_identity(record) | {"observed_at": observed_at, "event": "manager_bridge_check"}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_bridge_check_candidate(record: dict[str, Any], event_id: str) -> dict[str, Any]:
    return {
        "source": "manager_bridge_check",
        "event_id": event_id,
        "job_id": record.get("current_job_id") or "none",
        "status_path": None,
        "task_path": None,
        "log_path": None,
    }


def bridge_check_result(record: dict[str, Any], *, ack_timeout_seconds: float, reason: str | None = None) -> dict[str, Any]:
    verification = record.get("bridge_verification") if isinstance(record.get("bridge_verification"), dict) else {}
    return {
        "manager_id": record.get("manager_id"),
        "event_id": verification.get("event_id"),
        "verified": bool(verification.get("status") == "verified" and verification.get("acknowledged_by_codex")),
        "submitted_to_app_server": bool(verification.get("submitted_to_app_server")),
        "acknowledged_by_codex": bool(verification.get("acknowledged_by_codex")),
        "ack_timeout_seconds": ack_timeout_seconds,
        "manager_path": record.get("manager_path"),
        "reason": reason,
        "record": record,
    }


def bridge_check_manager(
    *,
    manager_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    ack_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    timeout = max(0.0, float(ack_timeout_seconds))
    if error:
        return {
            "manager_id": item_id,
            "event_id": None,
            "verified": False,
            "submitted_to_app_server": False,
            "acknowledged_by_codex": False,
            "ack_timeout_seconds": timeout,
            "manager_path": str(manager_record_path(paths, item_id)),
            "reason": error,
        }
    if record is None:
        return {
            "manager_id": item_id,
            "event_id": None,
            "verified": False,
            "submitted_to_app_server": False,
            "acknowledged_by_codex": False,
            "ack_timeout_seconds": timeout,
            "manager_path": str(manager_record_path(paths, item_id)),
            "reason": "manager record not found",
        }
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    if notify.get("mode") != "bridge":
        return bridge_check_result(record, ack_timeout_seconds=timeout, reason="manager bridge-check requires --notify bridge")
    if manager_cancel_state(record):
        return bridge_check_result(record, ack_timeout_seconds=timeout, reason="manager cancellation is requested")

    observed_at = tmux_state.utc_now()
    event_id = bridge_check_event_id(record, observed_at)
    candidate = build_bridge_check_candidate(record, event_id)
    prompt = build_manager_wake_prompt(record, candidate)
    prompt_hash = tmux_bridge.prompt_sha256(prompt)
    verification = bridge_notify_identity(record) | {
        "event_id": event_id,
        "mode": "bridge",
        "status": "awaiting_ack",
        "source": "manager_bridge_check",
        "prompt_sha256": prompt_hash,
        "submitted_to_app_server": False,
        "acknowledged_by_codex": False,
        "observed_at": observed_at,
        "submit_attempted_at": observed_at,
        "submitted_at": None,
        "acknowledged_at": None,
        "ack_turn_id": None,
        "ack_timeout_seconds": timeout,
        "expires_at": None,
    }
    record["bridge_verification"] = verification
    record = upsert_notification(
        record,
        event_id,
        {
            "event_id": event_id,
            "mode": "bridge",
            "source": "manager_bridge_check",
            "job_id": candidate.get("job_id"),
            "status": "awaiting_ack",
            "status_path": None,
            "task_path": None,
            "log_path": None,
            "observed_at": observed_at,
            "submit_attempted_at": observed_at,
            "submitted_to_app_server": False,
            "acknowledged_by_codex": False,
            "prompt_sha256": prompt_hash,
        },
    )
    record = preserve_external_cancel_state(paths, record)
    if manager_cancel_state(record):
        record = write_manager_record(paths, record)
        return bridge_check_result(record, ack_timeout_seconds=timeout, reason="manager cancellation is requested")
    record = write_manager_record(paths, record)

    bridge_record = {
        "endpoint": notify.get("endpoint"),
        "thread_id": notify.get("thread_id"),
        "workspace": record.get("workspace"),
    }
    try:
        delivery = tmux_bridge.deliver_bridge_candidate(bridge_record, candidate, prompt)
    except Exception as exc:
        record = preserve_external_cancel_state(paths, record)
        record["bridge_verification"] = dict(record["bridge_verification"]) | {
            "status": "submission_failed",
            "submitted_to_app_server": False,
            "error": str(exc),
        }
        record = upsert_notification(
            record,
            event_id,
            {
                "status": "submission_failed",
                "submitted_to_app_server": False,
                "error": str(exc),
            },
        )
        record = write_manager_record(paths, record)
        return bridge_check_result(record, ack_timeout_seconds=timeout, reason=str(exc))

    submitted_at = tmux_state.utc_now()
    latest, _latest_error = read_manager_record(paths, item_id)
    if latest is not None:
        record = merge_external_ack_fields(record, latest)
    record = preserve_external_cancel_state(paths, record)
    acknowledged = bool((record.get("bridge_verification") or {}).get("acknowledged_by_codex"))
    record["bridge_verification"] = dict(record["bridge_verification"]) | {
        "status": "verified" if acknowledged else "awaiting_ack",
        "submitted_to_app_server": True,
        "submitted_at": submitted_at,
        "prompt_sha256": delivery.get("prompt_sha256") or prompt_hash,
        "delivery": delivery,
    }
    record = upsert_notification(
        record,
        event_id,
        {
            "status": "acknowledged" if acknowledged else "awaiting_ack",
            "submitted_at": submitted_at,
            "submitted_to_app_server": True,
            "prompt_sha256": delivery.get("prompt_sha256") or prompt_hash,
            "delivery": delivery,
        },
    )
    record = write_manager_record(paths, record)

    deadline = time.monotonic() + timeout
    while True:
        latest, _latest_error = read_manager_record(paths, item_id)
        if latest is not None:
            record = latest
        if manager_cancel_state(record):
            return bridge_check_result(record, ack_timeout_seconds=timeout, reason="manager cancellation is requested")
        verification = record.get("bridge_verification") if isinstance(record.get("bridge_verification"), dict) else {}
        if verification.get("event_id") == event_id and verification.get("status") == "verified" and verification.get("acknowledged_by_codex"):
            return bridge_check_result(record, ack_timeout_seconds=timeout, reason=None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return bridge_check_result(record, ack_timeout_seconds=timeout, reason="bridge receipt ack timed out")
        time.sleep(min(0.25, remaining))


def queue_manager_job(
    *,
    manager_id: str,
    job_id: str,
    command_text: str | None,
    command_file: str | None,
    workspace: str | None = None,
    state_dir: str | None = None,
    cwd: str | None = None,
    pane_id: str | None = None,
    pane_index: str | None = None,
    allow_parallel: bool = False,
) -> dict[str, Any]:
    if not tmux_state.one_line_text(job_id):
        return manager_controlled_response({"manager_id": manager_id, "queued": False, "reason": "manager run-next requires nonblank --job-id"})
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, manager_id)
    item_id = tmux_state.safe_id(job_id)
    if error:
        return manager_controlled_response({"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": error})
    if record is None:
        return manager_controlled_response({"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": "manager record not found"})
    target_pane_id = tmux_state.one_line_text(pane_id) or tmux_state.one_line_text(record.get("worker_pane_id"))
    if not target_pane_id:
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager has no worker pane"})
    if record.get("pending_job"):
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager already has a pending job"})
    if not allow_parallel and (record.get("status") == "running" or active_job_ids(record)):
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager already has active jobs"})
    if pane_has_active_job(record, target_pane_id, excluding_job_id=item_id):
        return manager_controlled_response({
            "manager_id": record["manager_id"],
            "job_id": item_id,
            "queued": False,
            "reason": f"worker pane already has an active job: {target_pane_id}",
        })
    allowed, gate_reason = manager_queue_gate(record)
    if not allowed:
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": gate_reason})

    text, read_error = command_text_from_source(command_text, command_file)
    if read_error:
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": read_error})
    if not tmux_state.one_line_text(text):
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "command is blank"})

    record = preserve_external_cancel_state(paths, record)
    if manager_cancel_state(record):
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager cancellation is requested"})

    request_path = write_command_request(paths, record["manager_id"], item_id, str(text))
    record = mark_last_terminal_event_handled(record, next_job_id=item_id)
    manager_sequence = len([value for value in record.get("job_ids", []) if tmux_state.one_line_text(value)]) + 1
    target_pane_index = tmux_state.one_line_text(pane_index)
    if not target_pane_index and target_pane_id == tmux_state.one_line_text(record.get("worker_pane_id")):
        target_pane_index = tmux_state.one_line_text(record.get("worker_pane_index"))
    record["pending_job"] = build_pending_job(item_id, request_path, cwd, target_pane_id, target_pane_index, manager_sequence)
    record["status"] = "queued"
    record["worker_pane_ids"] = unique_text_values(list(record.get("worker_pane_ids") or []) + [target_pane_id])
    if not record.get("worker_pane_id"):
        record["worker_pane_id"] = target_pane_id
        record["worker_pane_index"] = target_pane_index
    elif target_pane_id == tmux_state.one_line_text(record.get("worker_pane_id")) and target_pane_index:
        record["worker_pane_index"] = target_pane_index
    record["last_error"] = None
    record = preserve_external_cancel_state(paths, record)
    if manager_cancel_state(record):
        request_path.unlink(missing_ok=True)
        record = write_manager_record(paths, record)
        return manager_controlled_response({"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager cancellation is requested"})
    record = write_manager_record(paths, record)
    return manager_controlled_response({
        "manager_id": record["manager_id"],
        "job_id": item_id,
        "queued": True,
        "manager_path": record["manager_path"],
        "command_request_path": str(request_path),
        "record": record,
    })


def ack_manager_event(
    *,
    manager_id: str,
    event_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    turn_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    if not tmux_state.one_line_text(event_id):
        return {"manager_id": item_id, "event_id": event_id, "acked": False, "reason": "manager ack requires nonblank --event-id"}
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    target_event_id = str(event_id)
    if error:
        return {"manager_id": item_id, "event_id": target_event_id, "acked": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "event_id": target_event_id, "acked": False, "reason": "manager record not found"}

    verification = record.get("bridge_verification") if isinstance(record.get("bridge_verification"), dict) else {}
    if target_event_id == str(verification.get("event_id") or ""):
        matches, match_reason = bridge_verification_matches(record, verification)
        if not matches:
            return {"manager_id": item_id, "event_id": target_event_id, "acked": False, "reason": match_reason}
        if not verification.get("submitted_to_app_server"):
            return {
                "manager_id": item_id,
                "event_id": target_event_id,
                "acked": False,
                "reason": "bridge verification event has not been submitted to app-server",
            }
        now = tmux_state.utc_now()
        existing = notification_for_event(record, target_event_id) or {}
        delivery = verification.get("delivery") if isinstance(verification.get("delivery"), dict) else {}
        if not delivery:
            delivery = existing.get("delivery") if isinstance(existing.get("delivery"), dict) else {}
        ack_turn_id = tmux_state.one_line_text(turn_id) or tmux_state.one_line_text(delivery.get("turn_id"))
        ack_fields = {
            "event_id": target_event_id,
            "mode": "bridge",
            "source": "manager_bridge_check",
            "job_id": existing.get("job_id") or record.get("current_job_id") or "none",
            "status": "acknowledged",
            "acknowledged_by_codex": True,
            "acknowledged_at": now,
            "ack_turn_id": ack_turn_id,
            "ack_note": tmux_state.one_line_text(note),
            "submitted_to_app_server": bool(verification.get("submitted_to_app_server")),
            "prompt_sha256": verification.get("prompt_sha256"),
        }
        record = upsert_notification(record, target_event_id, ack_fields)
        record["bridge_verification"] = dict(verification) | {
            "status": "verified",
            "acknowledged_by_codex": True,
            "acknowledged_at": now,
            "ack_turn_id": ack_turn_id,
            "ack_note": tmux_state.one_line_text(note),
        }
        record["last_ack"] = {
            "event_id": target_event_id,
            "acknowledged_at": now,
            "turn_id": ack_turn_id,
            "note": tmux_state.one_line_text(note),
        }
        record = preserve_external_cancel_state(paths, record)
        record = write_manager_record(paths, record)
        return {
            "manager_id": item_id,
            "event_id": target_event_id,
            "acked": True,
            "manager_path": record["manager_path"],
            "record": record,
        }

    existing = notification_for_event(record, target_event_id)
    if existing is None and target_event_id != str(record.get("last_terminal_event_id") or ""):
        return {"manager_id": item_id, "event_id": target_event_id, "acked": False, "reason": "event not found in manager record"}

    now = tmux_state.utc_now()
    existing_status = str((existing or {}).get("status") or "")
    status = "handled" if existing_status.startswith("handled") else "acknowledged"
    existing_delivery = (existing or {}).get("delivery") if isinstance((existing or {}).get("delivery"), dict) else {}
    ack_turn_id = tmux_state.one_line_text(turn_id) or tmux_state.one_line_text(existing_delivery.get("turn_id"))
    ack_fields = {
        "event_id": target_event_id,
        "mode": (existing or {}).get("mode") or "manual",
        "source": (existing or {}).get("source") or "manager_ack",
        "job_id": (existing or {}).get("job_id") or record.get("current_job_id"),
        "status": status,
        "acknowledged_by_codex": True,
        "acknowledged_at": now,
        "ack_turn_id": ack_turn_id,
        "ack_note": tmux_state.one_line_text(note),
    }
    if existing_status.startswith("handled"):
        ack_fields["handled_without_ack"] = False
    elif existing and "handled_without_ack" in existing:
        ack_fields["handled_without_ack"] = existing["handled_without_ack"]
    record = upsert_notification(
        record,
        target_event_id,
        ack_fields,
    )
    events = dict(record.get("events") or {})
    if target_event_id in events and isinstance(events[target_event_id], dict):
        events[target_event_id] = dict(events[target_event_id]) | {
            "acknowledged_by_codex": True,
            "acknowledged_at": now,
            "ack_turn_id": ack_turn_id,
            "ack_note": tmux_state.one_line_text(note),
            "notification_status": status,
        }
        record["events"] = events
    record["last_ack"] = {
        "event_id": target_event_id,
        "acknowledged_at": now,
        "turn_id": ack_turn_id,
        "note": tmux_state.one_line_text(note),
    }
    record = refresh_aggregate_status(record)
    record = preserve_external_cancel_state(paths, record)
    record = write_manager_record(paths, record)
    return {
        "manager_id": item_id,
        "event_id": target_event_id,
        "acked": True,
        "manager_path": record["manager_path"],
        "record": record,
    }


def notification_action_history(notification: dict[str, Any], action: str, *, note: str | None = None) -> list[dict[str, Any]]:
    history = notification.get("manual_action_history")
    rows = [dict(value) for value in history if isinstance(value, dict)] if isinstance(history, list) else []
    entry = {"action": action, "at": tmux_state.utc_now()}
    if note:
        entry["note"] = tmux_state.one_line_text(note)
    rows.append(entry)
    return rows[-20:]


def manager_notification_list(
    *,
    manager_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "found": False, "reason": error, "notifications": []}
    if record is None:
        return {"manager_id": item_id, "found": False, "reason": "manager record not found", "notifications": []}
    wanted = tmux_state.token_text(status)
    rows: list[dict[str, Any]] = []
    events = record.get("events") if isinstance(record.get("events"), dict) else {}
    for notification in record.get("notifications") or []:
        if not isinstance(notification, dict):
            continue
        event_id = tmux_state.one_line_text(notification.get("event_id"))
        item_status = tmux_state.token_text(notification.get("status")) or "unknown"
        if wanted and item_status != wanted:
            continue
        event = events.get(event_id) if isinstance(events.get(event_id), dict) else {}
        rows.append(
            {
                "event_id": event_id,
                "wake_id": notification.get("wake_id") or event.get("wake_id"),
                "job_id": notification.get("job_id") or event.get("job_id"),
                "status": item_status,
                "mode": notification.get("mode"),
                "acknowledged_by_codex": bool(notification.get("acknowledged_by_codex") or event.get("acknowledged_by_codex")),
                "submitted_to_tmux": bool(notification.get("submitted_to_tmux")),
                "injected_to_tmux": bool(notification.get("injected_to_tmux")),
                "requires_manual_resume": bool(notification.get("requires_manual_resume")),
                "receipt_retry_count": notification.get("receipt_retry_count"),
                "receipt_sidecar_check_count": notification.get("receipt_sidecar_check_count"),
                "sidecar_decision": (notification.get("receipt_check") or {}).get("action") if isinstance(notification.get("receipt_check"), dict) else None,
                "defer_reason": notification.get("defer_reason") or notification.get("reason") or event.get("defer_reason"),
                "observed_at": notification.get("observed_at") or event.get("observed_at"),
            }
        )
    return {"manager_id": item_id, "found": True, "manager_path": record.get("manager_path"), "notifications": rows}


def manager_notification_retry(
    *,
    manager_id: str,
    event_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    target_event_id = tmux_state.one_line_text(event_id)
    if not target_event_id:
        return {"manager_id": item_id, "event_id": event_id, "retried": False, "reason": "notification retry requires --event-id"}
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "event_id": target_event_id, "retried": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "event_id": target_event_id, "retried": False, "reason": "manager record not found"}
    notification = notification_for_event(record, target_event_id)
    if notification is None:
        return {"manager_id": item_id, "event_id": target_event_id, "retried": False, "reason": "notification not found"}
    if notification.get("acknowledged_by_codex"):
        return {"manager_id": item_id, "event_id": target_event_id, "retried": False, "reason": "notification is already acknowledged"}
    if notification.get("status") != "deferred" and not notification.get("requires_manual_resume"):
        return {"manager_id": item_id, "event_id": target_event_id, "retried": False, "reason": "notification is not deferred"}
    now = tmux_state.utc_now()
    fields = {
        "status": "inject_pending",
        "notification_phase": "manual_retry_ready",
        "manual_resumed_at": now,
        "requires_manual_resume": False,
        "submitted_to_tmux": False,
        "injected_to_tmux": False,
        "reason": tmux_state.one_line_text(note) or "manual notification retry requested",
        "manual_action_history": notification_action_history(notification, "retry", note=note),
    }
    record = upsert_notification(record, target_event_id, fields)
    record["submitted_event_ids"] = [value for value in list(record.get("submitted_event_ids") or []) if str(value) != target_event_id]
    events = dict(record.get("events") or {})
    event = dict(events.get(target_event_id) or {})
    events[target_event_id] = event | {
        "event_id": target_event_id,
        "notification_status": "inject_pending",
        "notification_phase": "manual_retry_ready",
        "requires_manual_resume": False,
        "submitted_to_tmux": False,
        "injected_to_tmux": False,
        "last_error": fields["reason"],
    }
    record["events"] = events
    record = refresh_aggregate_status(record)
    record = preserve_external_cancel_state(paths, record)
    record = write_manager_record(paths, record)
    return {"manager_id": item_id, "event_id": target_event_id, "retried": True, "manager_path": record["manager_path"], "record": record}


def manager_notification_discard(
    *,
    manager_id: str,
    event_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    target_event_id = tmux_state.one_line_text(event_id)
    if not target_event_id:
        return {"manager_id": item_id, "event_id": event_id, "discarded": False, "reason": "notification discard requires --event-id"}
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "event_id": target_event_id, "discarded": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "event_id": target_event_id, "discarded": False, "reason": "manager record not found"}
    notification = notification_for_event(record, target_event_id) or {}
    now = tmux_state.utc_now()
    reason = tmux_state.one_line_text(note) or "manual notification discard requested"
    fields = {
        "event_id": target_event_id,
        "mode": notification.get("mode") or "manual",
        "status": "discarded",
        "notification_phase": "discarded",
        "acknowledged_by_codex": True,
        "acknowledged_at": now,
        "ack_note": reason,
        "handled_without_ack": True,
        "requires_manual_resume": False,
        "reason": reason,
        "manual_action_history": notification_action_history(notification, "discard", note=note),
    }
    record = upsert_notification(record, target_event_id, fields)
    events = dict(record.get("events") or {})
    event = dict(events.get(target_event_id) or {})
    events[target_event_id] = event | {
        "event_id": target_event_id,
        "notification_status": "discarded",
        "notification_phase": "discarded",
        "acknowledged_by_codex": True,
        "acknowledged_at": now,
        "ack_note": reason,
        "handled_without_ack": True,
        "requires_manual_resume": False,
        "last_error": reason,
    }
    record["events"] = events
    record["last_ack"] = {"event_id": target_event_id, "acknowledged_at": now, "turn_id": "", "note": reason}
    record = refresh_aggregate_status(record)
    record = preserve_external_cancel_state(paths, record)
    record = write_manager_record(paths, record)
    return {"manager_id": item_id, "event_id": target_event_id, "discarded": True, "manager_path": record["manager_path"], "record": record}


def manager_notification_clear(
    *,
    manager_id: str,
    workspace: str | None = None,
    state_dir: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    item_id = manager_id_value(manager_id)
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "cleared": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "cleared": False, "reason": "manager record not found"}
    clearable_default = {"acknowledged", "handled", "discarded"}
    wanted = tmux_state.token_text(status)
    clearable = {wanted} if wanted else clearable_default
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for notification in record.get("notifications") or []:
        if not isinstance(notification, dict):
            continue
        event_id = tmux_state.one_line_text(notification.get("event_id"))
        if tmux_state.token_text(notification.get("status")) in clearable:
            removed.append(event_id)
            continue
        kept.append(notification)
    record["notifications"] = kept
    record["last_notification"] = kept[-1] if kept else None
    record = refresh_aggregate_status(record)
    record = preserve_external_cancel_state(paths, record)
    record = write_manager_record(paths, record)
    return {"manager_id": item_id, "cleared": True, "removed_event_ids": removed, "manager_path": record["manager_path"], "record": record}


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
    target_pane_id = tmux_state.one_line_text(pending.get("pane_id")) or tmux_state.one_line_text(record.get("worker_pane_id"))
    argv = [
        sys.executable,
        str(script_dir() / "tmux_control.py"),
        "run",
        "--pane",
        str(target_pane_id or ""),
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
    manager_sequence = pending.get("manager_sequence")
    if not isinstance(manager_sequence, int):
        manager_sequence = len([value for value in record.get("job_ids", []) if tmux_state.one_line_text(value)]) + 1
    if isinstance(result, dict):
        result["manager_sequence"] = manager_sequence
    jobs = dict(record.get("jobs") or {})
    jobs[job_id] = {
        "job_id": job_id,
        "pane_id": target_pane_id,
        "pane_index": tmux_state.one_line_text(pending.get("pane_index")),
        "command_request_path": pending.get("command_file"),
        "status_path": result.get("status_path"),
        "log_path": result.get("log_path"),
        "started_at": started_at,
        "run_returncode": proc.returncode,
        "run_result": result,
        "manager_sequence": manager_sequence,
        "status": "running" if proc.returncode == 0 else "failed_to_start",
    }
    record["jobs"] = jobs
    record["pending_job"] = None
    record["current_job_id"] = job_id
    if target_pane_id and not record.get("worker_pane_id"):
        record["worker_pane_id"] = target_pane_id
    record["worker_pane_ids"] = unique_text_values(list(record.get("worker_pane_ids") or []) + [target_pane_id])
    record["active_job_ids"] = unique_text_values(list(record.get("active_job_ids") or []) + [job_id])
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
    if proc.returncode != 0:
        record["active_job_ids"] = [value for value in active_job_ids(record) if value != job_id]
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


def tmux_pane_snapshot(pane_id: str | None) -> dict[str, Any]:
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return {"exists": False, "reason": "pane id is blank"}
    separator = "\x1f"
    fmt = separator.join(["#{pane_id}", "#{pane_dead}", "#{pane_pid}", "#{pane_current_command}"])
    try:
        proc = subprocess.run(
            [*tmux_command_prefix(), "display-message", "-p", "-t", target, fmt],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {"exists": False, "pane_id": target, "reason": "tmux command not found"}
    if proc.returncode != 0:
        return {"exists": False, "pane_id": target, "reason": proc.stderr.strip() or "pane not found"}
    parts = proc.stdout.rstrip("\n").split(separator)
    if len(parts) != 4 or parts[0] != target:
        return {"exists": False, "pane_id": target, "reason": "tmux pane lookup was ambiguous"}
    return {
        "exists": True,
        "pane_id": parts[0],
        "pane_dead": parts[1] == "1",
        "pane_pid": parse_pid(parts[2]),
        "pane_current_command": parts[3],
    }


def ps_process_index() -> tuple[dict[int, list[int]], dict[int, tuple[int | None, str]]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        return {}, {}
    if proc.returncode != 0:
        return {}, {}
    children: dict[int, list[int]] = {}
    processes: dict[int, tuple[int | None, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        pid = parse_pid(parts[0])
        ppid = parse_pid(parts[1])
        if pid is None:
            continue
        command = parts[2] if len(parts) > 2 else ""
        processes[pid] = (ppid, command)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    return children, processes


def command_looks_like_codex(command: str | None) -> bool:
    if not tmux_state.one_line_text(command):
        return False
    try:
        tokens = shlex.split(str(command))
    except ValueError:
        tokens = str(command).split()
    for token in tokens:
        if token.startswith("-"):
            continue
        basename = Path(token).name.lower()
        if basename in {"codex", "codex.exe", "codex-cli"}:
            return True
        if basename.startswith("codex.") or basename.startswith("codex-"):
            return True
    return False


def pane_codex_validation(pane_id: str | None) -> dict[str, Any]:
    snapshot = tmux_pane_snapshot(pane_id)
    if not snapshot.get("exists"):
        return {"safe": False, "status": "missing", "reason": snapshot.get("reason") or "pane not found", "pane": snapshot}
    if snapshot.get("pane_dead"):
        return {"safe": False, "status": "dead", "reason": "pane is dead", "pane": snapshot}
    if command_looks_like_codex(str(snapshot.get("pane_current_command") or "")):
        return {
            "safe": True,
            "status": "live_codex",
            "reason": "pane current command is Codex",
            "pane": snapshot,
            "codex_processes": [],
        }
    pane_pid = parse_pid(snapshot.get("pane_pid"))
    if pane_pid is None:
        return {"safe": False, "status": "no_pane_pid", "reason": "pane has no process id", "pane": snapshot}
    children, processes = ps_process_index()
    queue: list[int] = [pane_pid]
    seen: set[int] = set()
    matches: list[dict[str, Any]] = []
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        ppid, command = processes.get(pid, (None, ""))
        if command_looks_like_codex(command):
            matches.append({"pid": pid, "ppid": ppid, "command": command})
        queue.extend(children.get(pid, []))
    if not matches:
        return {
            "safe": False,
            "status": "no_live_codex_process",
            "reason": "pane does not contain a live Codex process",
            "pane": snapshot,
            "descendant_count": max(0, len(seen) - 1),
        }
    return {
        "safe": True,
        "status": "live_codex",
        "reason": "pane contains a live Codex process",
        "pane": snapshot,
        "codex_processes": matches[:3],
        "codex_process_count": len(matches),
    }


def normalize_tmux_inject_sdk_decision(value: Any, bound_pane_id: str | None = None) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    decision = tmux_state.token_text(payload.get("decision")) or "defer"
    if decision not in {"inject", "defer", "refuse"}:
        decision = "defer"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    target_pane = tmux_state.one_line_text(payload.get("target_pane") or payload.get("target_pane_id"))
    if decision == "inject" and target_pane and bound_pane_id and target_pane != bound_pane_id:
        return {
            "decision": "refuse",
            "target_pane": target_pane,
            "confidence": min(max(confidence, 0.0), 1.0),
            "reason": "SDK selected a pane different from the bound Codex pane",
            "raw_decision": decision,
        }
    return {
        "decision": decision,
        "target_pane": target_pane or bound_pane_id,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": tmux_state.one_line_text(payload.get("reason")) or "no reason provided",
    }


def codex_sdk_inject_decision(
    record: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
    *,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    bound_pane_id = tmux_state.one_line_text(record.get("codex_pane_id"))
    fixture_decision = tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_DECISION"))
    if fixture_decision:
        return normalize_tmux_inject_sdk_decision(
            {
                "decision": fixture_decision,
                "target_pane": os.environ.get("TMUX_SKILLS_CODEX_SDK_TARGET_PANE") or bound_pane_id,
                "confidence": os.environ.get("TMUX_SKILLS_CODEX_SDK_CONFIDENCE") or 1.0,
                "reason": "environment-supplied tmux-inject planner decision",
            },
            bound_pane_id,
        ) | {"source": "env"}
    if codex_sidecar_fast_path_enabled():
        if not bound_pane_id:
            return {
                "decision": "refuse",
                "target_pane": bound_pane_id,
                "confidence": 1.0,
                "reason": "tmux-inject has no bound Codex pane",
                "source": "deterministic_fast_path",
            }
        if not validation.get("safe"):
            return {
                "decision": "refuse",
                "target_pane": bound_pane_id,
                "confidence": 1.0,
                "reason": tmux_state.one_line_text(validation.get("reason")) or "Codex pane validation failed",
                "source": "deterministic_fast_path",
            }
        return {
            "decision": "inject",
            "target_pane": bound_pane_id,
            "confidence": 1.0,
            "reason": "deterministic fast path allows injection into the bound Codex pane",
            "source": "deterministic_fast_path",
        }
    sidecar = codex_sidecar_decision(
        {
            "task": "Decide whether tmux-skills may inject a wake prompt into the bound Codex pane.",
            "allowed_decisions": ["inject", "defer", "refuse"],
            "workspace": record.get("workspace"),
            "state_dir": record.get("state_dir"),
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "bound_pane_id": bound_pane_id,
            "pane_validation": validation,
            "rules": [
                "Return JSON with decision, target_pane, confidence, and reason.",
                "Choose inject only when the bound pane is the target and validation is safe.",
                "Never choose a pane different from bound_pane_id.",
            ],
        },
        timeout_seconds=timeout_seconds,
    )
    if sidecar and sidecar.get("source") == "codex_sidecar":
        return normalize_tmux_inject_sdk_decision(sidecar, bound_pane_id) | {
            "source": "codex_sidecar",
            "sidecar_setup": sidecar.get("setup"),
            "timing": sidecar.get("timing"),
            "sidecar_config": sidecar.get("sidecar_config") or codex_sidecar_config(),
        }
    if not bound_pane_id:
        return {
            "decision": "refuse",
            "target_pane": bound_pane_id,
            "confidence": 1.0,
            "reason": "tmux-inject has no bound Codex pane",
            "source": "deterministic",
            "sidecar": sidecar,
        }
    if not validation.get("safe"):
        return {
            "decision": "refuse",
            "target_pane": bound_pane_id,
            "confidence": 1.0,
            "reason": tmux_state.one_line_text(validation.get("reason")) or "Codex pane validation failed",
            "source": "deterministic",
            "sidecar": sidecar,
        }
    return {
        "decision": "inject",
        "target_pane": bound_pane_id,
        "confidence": 1.0,
        "reason": "deterministic guardrails allow injection into the bound Codex pane",
        "source": "deterministic",
        "sidecar": sidecar,
    }


def build_tmux_inject_wake_prompt(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    event_id = str(candidate.get("event_id") or "unknown")
    wake_id = tmux_state.one_line_text(candidate.get("wake_id")) or tmux_inject_wake_id(event_id)
    return TMUX_INJECT_WAKE_PROMPT.format(
        wake_id=wake_id,
        manager_id=record.get("manager_id") or "unknown",
        event_id=event_id,
    )


def capture_tmux_pane_text(
    pane_id: str,
    *,
    lines: int = TMUX_INJECT_CAPTURE_LINES,
    max_chars: int = TMUX_INJECT_CAPTURE_MAX_CHARS,
) -> dict[str, Any]:
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return {"captured": False, "reason": "pane id is blank", "output": ""}
    proc = subprocess.run(
        [*tmux_command_prefix(), "capture-pane", "-p", "-e", "-t", target, "-S", f"-{max(1, lines)}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    raw_output = proc.stdout if proc.returncode == 0 else ""
    output = tmux_text.strip_ansi(raw_output)
    omitted = max(0, len(output) - max_chars)
    if omitted:
        output = output[-max_chars:]
    raw_omitted = max(0, len(raw_output) - max_chars * 4)
    if raw_omitted:
        raw_output = raw_output[-max_chars * 4 :]
    return {
        "captured": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output,
        "raw_output": raw_output,
        "omitted_chars": omitted,
        "raw_omitted_chars": raw_omitted,
        "reason": None if proc.returncode == 0 else (proc.stderr.strip() or f"tmux capture-pane exited {proc.returncode}"),
    }


def latest_staged_wake_prompt_block(prompt: str, capture_output: str) -> str:
    if not prompt or not capture_output:
        return ""
    tail_lines = capture_output.splitlines()[-30:]
    prompt_lines = prompt.splitlines()
    first_line = prompt_lines[0] if prompt_lines else ""
    manager_line = next((line.strip() for line in prompt_lines if line.strip().startswith("Manager ID:")), "")
    event_line = next((line.strip() for line in prompt_lines if line.strip().startswith("Event ID:")), "")
    for index in range(len(tail_lines) - 1, -1, -1):
        if first_line in tail_lines[index]:
            block = "\n".join(tail_lines[index:])
            if manager_line and manager_line not in block:
                continue
            if event_line and event_line not in block:
                continue
            if "Manager ID:" in block and "Event ID:" in block and codex_composer_block_is_active(block):
                return block
    return ""


def codex_composer_footer_line(line: str) -> bool:
    text = tmux_text.strip_ansi(line).strip()
    if not text:
        return False
    return (
        "queue message" in text
        or "submit message" in text
        or "to submit" in text
        or ("Context" in text and "left" in text)
    )


def codex_composer_block_is_active(block: str) -> bool:
    if not block:
        return False
    lines = block.splitlines()
    if not lines or not tmux_text.strip_ansi(lines[0]).lstrip().startswith("›"):
        return False
    for line in lines[1:]:
        stripped = tmux_text.strip_ansi(line).lstrip()
        if codex_composer_footer_line(stripped):
            return True
        if not stripped:
            continue
        if stripped.startswith(("•", "└", "│", "├", "┌")):
            return False
        if stripped and set(stripped) <= {"─"}:
            return False
        if stripped.startswith("›"):
            return False
    return False


def codex_composer_block_content(block: str) -> str:
    if not block:
        return ""
    first = tmux_text.strip_ansi(block.splitlines()[0]).lstrip()
    return first[1:].strip() if first.startswith("›") else first.strip()


def codex_composer_block_is_placeholder(block: str) -> bool:
    content = codex_composer_block_content(block)
    if not content:
        return False
    first = block.splitlines()[0]
    if "›" not in tmux_text.strip_ansi(first):
        return False
    prompt_index = first.find("›")
    after_prompt = first[prompt_index + 1 :] if prompt_index >= 0 else first
    return "\x1b[2m" in after_prompt and content in {"Explain this codebase"}


def latest_codex_composer_block(capture_output: str) -> str:
    if not capture_output:
        return ""
    tail_lines = capture_output.splitlines()[-30:]
    for index in range(len(tail_lines) - 1, -1, -1):
        if tmux_text.strip_ansi(tail_lines[index]).lstrip().startswith("›"):
            block = "\n".join(tail_lines[index:])
            if codex_composer_block_is_active(block):
                return block
    return ""


def tmux_inject_composer_state(
    prompt: str,
    capture_output: str,
    raw_capture_output: str | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block = latest_codex_composer_block(capture_output)
    raw_block = latest_codex_composer_block(raw_capture_output or "")
    staged_block = latest_staged_wake_prompt_block(prompt, capture_output)
    wake_visibility = tmux_inject_wake_visibility(prompt, capture_output, record=record)
    if staged_block:
        return {
            "status": "manager_wake_prompt_staged",
            "safe_to_inject": False,
            "safe_to_submit": True,
            "prompt_still_staged": wake_prompt_still_staged(prompt, capture_output),
            "reason": "manager wake prompt is staged in the Codex composer",
            **wake_visibility,
        }
    if wake_visibility.get("other_wake_ids"):
        return {
            "status": "other_wake_prompt_staged",
            "safe_to_inject": False,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "a different tmux-skills wake prompt is already visible",
            **wake_visibility,
        }
    if wake_visibility.get("same_wake_visible"):
        return {
            "status": "same_wake_prompt_visible",
            "safe_to_inject": False,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "the same tmux-skills wake prompt is already visible in Codex",
            **wake_visibility,
        }
    if not block:
        return {
            "status": "no_composer_text_detected",
            "safe_to_inject": True,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "no Codex composer text detected in bounded capture",
            **wake_visibility,
        }
    content = codex_composer_block_content(block)
    if not content:
        return {
            "status": "empty_composer",
            "safe_to_inject": True,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "Codex composer appears empty",
            **wake_visibility,
        }
    if raw_block and codex_composer_block_is_placeholder(raw_block):
        return {
            "status": "placeholder_composer_suggestion",
            "safe_to_inject": True,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "Codex composer shows a placeholder suggestion, not user-entered text",
            "composer_preview": content[:120],
            **wake_visibility,
        }
    content_wake_ids = tmux_inject_capture_wake_ids(content)
    prompt_wake_id = tmux_inject_prompt_wake_id(prompt)
    if prompt_wake_id and prompt_wake_id in content_wake_ids:
        return {
            "status": "same_wake_prompt_visible",
            "safe_to_inject": False,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "the same tmux-skills wake prompt is already visible in Codex",
            "composer_preview": content[:120],
            **wake_visibility,
        }
    if content.startswith("tmux-skills event ready") or content.startswith("ID:") or content_wake_ids:
        return {
            "status": "other_wake_prompt_staged",
            "safe_to_inject": False,
            "safe_to_submit": False,
            "prompt_still_staged": False,
            "reason": "a different tmux-skills wake prompt is already staged",
            "composer_preview": content[:120],
            **wake_visibility,
        }
    return {
        "status": "composer_text_present",
        "safe_to_inject": False,
        "safe_to_submit": False,
        "prompt_still_staged": False,
        "reason": "Codex composer contains text that was not written by tmux-skills",
        "composer_preview": content[:120],
        **wake_visibility,
    }


def receipt_sidecar_pane_capture_tail(capture: dict[str, Any], composer_state: dict[str, Any], *, max_lines: int = 15, max_chars: int = 1200) -> str:
    output = str(capture.get("output") or "")
    if composer_state.get("status") != "placeholder_composer_suggestion":
        return "\n".join(output.splitlines()[-max_lines:])[-max_chars:]
    block = latest_codex_composer_block(output)
    sanitized = output
    if block and block in sanitized:
        sanitized = sanitized.rsplit(block, 1)[0].rstrip()
    marker = "[Codex composer placeholder suggestion omitted by tmux-skills; pane is idle/safe placeholder UI.]"
    sanitized = f"{sanitized}\n{marker}".strip()
    return "\n".join(sanitized.splitlines()[-max_lines:])[-max_chars:]


def wake_prompt_still_staged(prompt: str, capture_output: str) -> bool:
    block = latest_staged_wake_prompt_block(prompt, capture_output)
    return bool(block and codex_composer_block_is_active(block))


def default_tmux_inject_followup_submit_key(prompt: str, capture_output: str) -> str:
    block = latest_staged_wake_prompt_block(prompt, capture_output)
    if "queue message" in block:
        return TMUX_INJECT_QUEUE_SUBMIT_KEY
    return TMUX_INJECT_FOLLOWUP_SUBMIT_KEY


def normalize_tmux_inject_followup_decision(value: Any, *, prompt: str, capture_output: str) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    raw_action = tmux_state.token_text(payload.get("action")) or ""
    composer_state = tmux_inject_composer_state(prompt, capture_output)
    staged = bool(composer_state.get("prompt_still_staged"))
    submit_key = tmux_state.one_line_text(payload.get("submit_key")) or default_tmux_inject_followup_submit_key(prompt, capture_output)
    reason = tmux_state.one_line_text(payload.get("reason")) or "heuristic post-injection decision"
    if composer_state.get("status") in {"composer_text_present", "other_wake_prompt_staged"}:
        action = "defer"
        reason = tmux_state.one_line_text(composer_state.get("reason")) or reason
    elif staged:
        action = "submit"
        if raw_action == "confirmed":
            reason = f"{reason}; deterministic staged prompt override"
    else:
        action = raw_action if raw_action in {"confirmed", "defer", "refuse"} else "confirmed"
        if raw_action == "submit":
            reason = f"{reason}; deterministic capture no longer shows staged prompt"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "action": action,
        "submit_key": submit_key,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": reason,
    }


def codex_sdk_inject_followup_decision(
    record: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
    injection: dict[str, Any],
    capture: dict[str, Any],
    prompt: str,
    *,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    output = str(capture.get("output") or "")
    fixture_action = tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_FOLLOWUP_ACTION"))
    if fixture_action:
        return normalize_tmux_inject_followup_decision(
            {
                "action": fixture_action,
                "submit_key": os.environ.get("TMUX_SKILLS_CODEX_SDK_FOLLOWUP_SUBMIT_KEY") or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY,
                "confidence": os.environ.get("TMUX_SKILLS_CODEX_SDK_CONFIDENCE") or 1.0,
                "reason": "environment-supplied tmux-inject follow-up decision",
            },
            prompt=prompt,
            capture_output=output,
        ) | {"source": "env"}
    composer_state = tmux_inject_composer_state(prompt, output, str(capture.get("raw_output") or ""), record=record)
    if composer_state.get("status") in {"composer_text_present", "other_wake_prompt_staged"}:
        return {
            "action": "defer",
            "submit_key": TMUX_INJECT_FOLLOWUP_SUBMIT_KEY,
            "confidence": 1.0,
            "reason": tmux_state.one_line_text(composer_state.get("reason")) or "unsafe Codex composer state",
            "source": "deterministic",
            "composer_state": composer_state,
        }
    if wake_prompt_still_staged(prompt, output):
        return pending_followup_decision(
            "deterministic staged prompt requires immediate bounded follow-up",
            prompt=prompt,
            capture_output=output,
            source="deterministic",
        ) | {"sidecar_skipped": "staged prompt cannot wait for SDK confirmation", "composer_state": composer_state}
    if codex_sidecar_fast_path_enabled():
        return {
            "action": "confirmed",
            "submit_key": TMUX_INJECT_FOLLOWUP_SUBMIT_KEY,
            "confidence": 0.9,
            "reason": "deterministic fast path confirms the wake prompt is no longer staged",
            "source": "deterministic_fast_path",
        }
    sidecar = codex_sidecar_decision(
        {
            "task": "Decide whether a tmux-inject wake prompt was submitted to Codex or remains staged in the composer.",
            "allowed_actions": ["confirmed", "submit", "defer", "refuse"],
            "workspace": record.get("workspace"),
            "state_dir": record.get("state_dir"),
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "bound_pane_id": record.get("codex_pane_id"),
            "pane_validation": validation,
            "injection": injection,
            "pane_capture_tail": output[-6000:],
            "rules": [
                "Return JSON with action, submit_key, confidence, and reason.",
                "Choose submit only if the wake prompt remains in the Codex composer/input area and no unrelated text is visible.",
                "Use submit_key Tab when the composer footer says queue message.",
                "Choose defer when user text, a different prompt, or unknown staged content is visible.",
                "Choose confirmed if the prompt is no longer staged and no unrelated composer text is visible.",
                "Never choose a different pane or describe shell commands.",
            ],
        },
        timeout_seconds=timeout_seconds,
    )
    if sidecar and sidecar.get("source") == "codex_sidecar":
        return normalize_tmux_inject_followup_decision(sidecar, prompt=prompt, capture_output=output) | {
            "source": "codex_sidecar",
            "sidecar_setup": sidecar.get("setup"),
            "timing": sidecar.get("timing"),
        }
    result = pending_followup_decision(
        "deterministic pane inspection after tmux-inject delivery",
        prompt=prompt,
        capture_output=output,
        source="deterministic",
    )
    if sidecar:
        result["sidecar"] = sidecar
    return result


def normalize_terminal_event_assessment(value: Any, *, candidate: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    action = tmux_state.token_text(payload.get("recommended_action")) or "wake_codex"
    if action not in {"wake_codex", "defer", "refuse"}:
        action = "wake_codex"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    summary = tmux_state.one_line_text(payload.get("summary"))
    if not summary:
        summary = f"terminal event {candidate.get('event_id') or 'unknown'} status={candidate.get('status') or 'unknown'}"
    return {
        "summary": summary,
        "recommended_action": action,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": tmux_state.one_line_text(payload.get("reason")) or "terminal event should be inspected by Codex",
    }


def deterministic_terminal_event_assessment(candidate: dict[str, Any], *, sidecar: dict[str, Any] | None = None) -> dict[str, Any]:
    status = tmux_state.token_text(candidate.get("status")) or "unknown"
    last_output = str(candidate.get("last_output") or "")
    digit_match = re.search(r"RANDOM_DIGIT=(\d)", last_output)
    digit_text = f" random_digit={digit_match.group(1)}" if digit_match else ""
    summary = f"terminal event {candidate.get('event_id') or 'unknown'} status={status}{digit_text}"
    result = {
        "summary": summary,
        "recommended_action": "wake_codex" if status in MANAGER_TERMINAL_JOB_STATUSES else "defer",
        "confidence": 0.8 if digit_match else 0.65,
        "reason": "deterministic terminal event assessment from bounded status tail",
        "source": "deterministic",
    }
    if sidecar:
        result["sidecar"] = sidecar
    return result


def codex_sdk_terminal_event_assessment(
    record: dict[str, Any],
    candidate: dict[str, Any],
    *,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    if codex_sidecar_fast_path_enabled() and tmux_state.token_text(candidate.get("status")) in MANAGER_TERMINAL_JOB_STATUSES:
        return deterministic_terminal_event_assessment(candidate)
    sidecar = codex_sidecar_decision(
        {
            "task": "Assess a tmux-skills terminal event and recommend whether main Codex should inspect it.",
            "terminal_assessment": True,
            "workspace": record.get("workspace"),
            "state_dir": record.get("state_dir"),
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "job_id": candidate.get("job_id"),
            "job_status": candidate.get("status"),
            "exit_code": candidate.get("exit_code"),
            "last_output_tail": str(candidate.get("last_output") or "")[-2000:],
            "rules": [
                "Return JSON with summary, recommended_action, confidence, and reason.",
                "Choose wake_codex for terminal worker results that main Codex should inspect or acknowledge.",
                "Do not suggest shell commands or follow-up jobs.",
            ],
        },
        timeout_seconds=timeout_seconds,
    )
    if sidecar and sidecar.get("source") == "codex_sidecar":
        return normalize_terminal_event_assessment(sidecar, candidate=candidate) | {
            "source": "codex_sidecar",
            "sidecar_setup": sidecar.get("setup"),
            "timing": sidecar.get("timing"),
            "sidecar_config": sidecar.get("sidecar_config") or codex_sidecar_config(),
        }
    return deterministic_terminal_event_assessment(candidate, sidecar=sidecar)


def send_tmux_submit_key(pane_id: str, submit_key: str) -> dict[str, Any]:
    target = tmux_state.one_line_text(pane_id)
    key = tmux_state.one_line_text(submit_key) or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY
    if not target:
        return {"sent": False, "submit_key": key, "reason": "pane id is blank"}
    proc = subprocess.run(
        [*tmux_command_prefix(), "send-keys", "-t", target, key],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "sent": proc.returncode == 0,
        "submit_key": key,
        "returncode": proc.returncode,
        "reason": None if proc.returncode == 0 else (proc.stderr.strip() or f"tmux send-keys exited {proc.returncode}"),
    }


def inject_tmux_wake_prompt(pane_id: str, prompt: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
    total_start = monotonic_ms()
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return {"injected": False, "pasted": False, "entered": False, "reason": "pane id is blank", "timing": {"total": timing_entry(total_start)}}
    preflight_start = monotonic_ms()
    preflight_capture = capture_tmux_pane_text(target)
    preflight_state = tmux_inject_composer_state(
        prompt,
        str(preflight_capture.get("output") or ""),
        str(preflight_capture.get("raw_output") or ""),
        record=record,
    )
    preflight_timing = timing_entry(preflight_start, captured=preflight_capture.get("captured"), status=preflight_state.get("status"))
    if (not preflight_capture.get("captured")) or not preflight_state.get("safe_to_inject"):
        reason = (
            tmux_state.one_line_text(preflight_capture.get("reason"))
            if not preflight_capture.get("captured")
            else tmux_state.one_line_text(preflight_state.get("reason"))
        )
        return {
            "injected": False,
            "pasted": False,
            "entered": False,
            "reason": reason or "Codex composer is not safe for tmux-inject",
            "preflight": {
                "captured": preflight_capture.get("captured"),
                "returncode": preflight_capture.get("returncode"),
                "omitted_chars": preflight_capture.get("omitted_chars"),
                "composer_state": preflight_state,
                "reason": preflight_capture.get("reason"),
            },
            "timing": {"preflight_capture": preflight_timing, "total": timing_entry(total_start)},
        }
    buffer_name = "tmux-skills-" + hashlib.sha256(f"{target}\n{prompt}".encode("utf-8")).hexdigest()[:16]
    load_start = monotonic_ms()
    load = subprocess.run(
        [*tmux_command_prefix(), "load-buffer", "-b", buffer_name, "-"],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    load_timing = timing_entry(load_start, returncode=load.returncode)
    if load.returncode != 0:
        return {
            "injected": False,
            "pasted": False,
            "entered": False,
            "reason": load.stderr.strip() or "tmux load-buffer failed",
            "load_returncode": load.returncode,
            "preflight": {
                "captured": preflight_capture.get("captured"),
                "returncode": preflight_capture.get("returncode"),
                "omitted_chars": preflight_capture.get("omitted_chars"),
                "composer_state": preflight_state,
                "reason": preflight_capture.get("reason"),
            },
            "timing": {"preflight_capture": preflight_timing, "load_buffer": load_timing, "total": timing_entry(total_start)},
        }
    paste_start = monotonic_ms()
    paste = subprocess.run(
        [*tmux_command_prefix(), "paste-buffer", "-b", buffer_name, "-t", target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    paste_timing = timing_entry(paste_start, returncode=paste.returncode)
    pasted = paste.returncode == 0
    submit_start = monotonic_ms()
    submit = send_tmux_submit_key(target, TMUX_INJECT_PRIMARY_SUBMIT_KEY) if pasted else None
    submit_timing = timing_entry(submit_start, returncode=submit.get("returncode") if submit else None, sent=submit.get("sent") if submit else None)
    subprocess.run([*tmux_command_prefix(), "delete-buffer", "-b", buffer_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    submitted = bool(submit and submit.get("sent"))
    result = {
        "injected": pasted and submitted,
        "pasted": pasted,
        "entered": submitted,
        "submit_key": TMUX_INJECT_PRIMARY_SUBMIT_KEY,
        "paste_returncode": paste.returncode,
        "enter_returncode": submit.get("returncode") if submit else None,
        "submit_returncode": submit.get("returncode") if submit else None,
        "preflight": {
            "captured": preflight_capture.get("captured"),
            "returncode": preflight_capture.get("returncode"),
            "omitted_chars": preflight_capture.get("omitted_chars"),
            "composer_state": preflight_state,
            "reason": preflight_capture.get("reason"),
        },
        "timing": {
            "preflight_capture": preflight_timing,
            "load_buffer": load_timing,
            "paste_buffer": paste_timing,
            "submit_key": submit_timing,
            "total": timing_entry(total_start),
        },
    }
    if not pasted:
        result["reason"] = paste.stderr.strip() or "tmux paste-buffer failed"
    elif not submitted:
        result["reason"] = str(submit.get("reason") if submit else "tmux send-keys submit failed")
    return result


def verify_tmux_inject_delivery(
    record: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
    injection: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    total_start = monotonic_ms()
    pane_id = tmux_state.one_line_text(record.get("codex_pane_id"))
    time.sleep(0.5)
    capture_start = monotonic_ms()
    before = capture_tmux_pane_text(pane_id)
    capture_before_timing = timing_entry(capture_start, lines=TMUX_INJECT_CAPTURE_LINES, max_chars=TMUX_INJECT_CAPTURE_MAX_CHARS)
    decision_start = monotonic_ms()
    decision = codex_sdk_inject_followup_decision(record, candidate, validation, injection, before, prompt)
    decision_timing = timing_entry(decision_start, source=decision.get("source"))
    followup: dict[str, Any] | None = None
    followup_timing: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    capture_after_timing: dict[str, Any] | None = None
    if decision.get("action") == "submit":
        followup_start = monotonic_ms()
        followup = send_tmux_submit_key(pane_id, str(decision.get("submit_key") or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY))
        followup_timing = timing_entry(followup_start, submit_key=followup.get("submit_key"), sent=followup.get("sent"))
        time.sleep(0.5)
        capture_after_start = monotonic_ms()
        after = capture_tmux_pane_text(pane_id)
        capture_after_timing = timing_entry(capture_after_start, lines=TMUX_INJECT_CAPTURE_LINES, max_chars=TMUX_INJECT_CAPTURE_MAX_CHARS)
    final_capture = after or before
    final_output = str(final_capture.get("output") or "")
    return {
        "checked": bool(before.get("captured")),
        "checked_at": tmux_state.utc_now(),
        "decision": decision,
        "followup": followup,
        "capture_before": {
            "captured": before.get("captured"),
            "returncode": before.get("returncode"),
            "omitted_chars": before.get("omitted_chars"),
            "prompt_still_staged": wake_prompt_still_staged(prompt, str(before.get("output") or "")),
            "reason": before.get("reason"),
        },
        "capture_after": None
        if after is None
        else {
            "captured": after.get("captured"),
            "returncode": after.get("returncode"),
            "omitted_chars": after.get("omitted_chars"),
            "prompt_still_staged": wake_prompt_still_staged(prompt, str(after.get("output") or "")),
            "reason": after.get("reason"),
        },
        "prompt_still_staged": wake_prompt_still_staged(prompt, final_output),
        "timing": {
            "capture_before": capture_before_timing,
            "decision": decision_timing,
            "followup": followup_timing,
            "capture_after": capture_after_timing,
            "total": timing_entry(total_start),
        },
    }


def tmux_inject_status_after_delivery(delivery_check: dict[str, Any] | None, injection: dict[str, Any] | None) -> tuple[str, str | None]:
    delivery = delivery_check if isinstance(delivery_check, dict) else {}
    decision = delivery.get("decision") if isinstance(delivery.get("decision"), dict) else {}
    inject_result = injection if isinstance(injection, dict) else {}
    if delivery.get("prompt_still_staged"):
        return "inject_pending", "tmux-inject wake prompt is still staged in the Codex composer after submit attempts"
    if decision.get("action") == "defer":
        return "receipt_blocked", str(decision.get("reason") or "tmux-inject delivery awaits Codex ack")
    if decision.get("action") == "refuse":
        return "inject_refused", str(decision.get("reason") or "Codex SDK follow-up refused tmux-inject delivery")
    if inject_result.get("injected"):
        return "awaiting_receipt", "tmux-inject wake prompt delivered; waiting for manager ack"
    return "inject_pending", str(inject_result.get("reason") or "tmux injection did not complete prompt submission")


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
    job_ids = unique_text_values(active_job_ids(record) + [str(record.get("current_job_id") or "")])
    if not job_ids:
        return record
    try:
        max_bytes = int(record.get("log_max_bytes") or DEFAULT_MANAGER_LOG_MAX_BYTES)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MANAGER_LOG_MAX_BYTES
    if max_bytes <= 0:
        return record
    for job_id in job_ids:
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


def status_with_manager_metadata(status: dict[str, Any] | None, record: dict[str, Any], job_id: str | None) -> dict[str, Any] | None:
    if status is None or not job_id:
        return status
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
    if "manager_sequence" in job:
        return dict(status) | {"manager_sequence": job["manager_sequence"]}
    return status


def task_path_for_job(paths: dict[str, Path], job_id: str | None) -> str | None:
    if not job_id:
        return None
    tasks, _errors = tmux_state.load_tasks(paths["root"])
    safe_job_id = tmux_state.safe_id(job_id)
    for task in tasks:
        if task.get("after_job_id") == safe_job_id:
            return str(task.get("task_path") or "")
    return None


def worker_missing_event_id(record: dict[str, Any], job_id: str | None = None, pane_id: str | None = None) -> str:
    payload = {
        "manager_id": record.get("manager_id"),
        "worker_pane_id": pane_id or record.get("worker_pane_id"),
        "current_job_id": job_id or record.get("current_job_id"),
        "event": "worker_pane_missing",
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_manager_wake_prompt(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    ack_command = " ".join(
        shlex.quote(str(value))
        for value in (
            "python3",
            script_dir() / "tmux_control.py",
            "manager",
            "ack",
            "--manager-id",
            record.get("manager_id") or "",
            "--event-id",
            candidate.get("event_id") or "",
            "--workspace",
            record.get("workspace") or "",
            "--state-dir",
            record.get("state_dir") or "",
        )
    )
    return "\n".join(
        [
            "tmux-skills manager observed a bridge event.",
            "",
            f"Event ID: {candidate.get('event_id') or 'unknown'}",
            f"Manager ID: {record.get('manager_id') or 'unknown'}",
            f"Job ID: {candidate.get('job_id') or 'none'}",
            f"Pane ID: {candidate.get('pane_id') or 'none'}",
            f"Workspace: {record.get('workspace')}",
            f"Manager path: {record.get('manager_path') or 'none'}",
            f"Status path: {candidate.get('status_path') or 'none'}",
            f"Task path: {candidate.get('task_path') or 'none'}",
            f"Log path: {candidate.get('log_path') or 'none'}",
            "",
            f"Ack command: {ack_command}",
            "After inspecting these paths, acknowledge receipt with manager ack for the event id.",
        ]
    )


def notify_terminal_event(record: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    event_id = str(candidate["event_id"])
    submitted = list(record.get("submitted_event_ids") or [])
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {"mode": "none"}
    notify_start = monotonic_ms()
    now = tmux_state.utc_now()
    notification_timing: dict[str, Any] = {}
    existing = notification_for_event(record, event_id) or {}
    wake_id = tmux_state.one_line_text(existing.get("wake_id")) or tmux_state.one_line_text(candidate.get("wake_id")) or tmux_inject_wake_id(event_id)
    candidate["wake_id"] = wake_id
    if event_id in submitted and not (
        notify.get("mode") == "tmux-inject"
        and not existing.get("acknowledged_by_codex")
        and tmux_inject_ack_recheck_due(existing)
    ):
        return record
    terminal_assessment = existing.get("terminal_assessment") if isinstance(existing.get("terminal_assessment"), dict) else None
    if terminal_assessment is None:
        assessment_start = monotonic_ms()
        terminal_assessment = codex_sdk_terminal_event_assessment(record, candidate)
        notification_timing["terminal_assessment"] = timing_entry(assessment_start, source=terminal_assessment.get("source"))
        if isinstance(terminal_assessment.get("timing"), dict):
            notification_timing["terminal_assessment_sidecar"] = terminal_assessment.get("timing")
    else:
        notification_timing["terminal_assessment"] = {"reused": True}
    base = {
        "event_id": event_id,
        "source": candidate.get("source"),
        "job_id": candidate.get("job_id"),
        "pane_id": candidate.get("pane_id"),
        "job_status": candidate.get("status"),
        "status_path": candidate.get("status_path"),
        "task_path": candidate.get("task_path"),
        "log_path": candidate.get("log_path"),
        "observed_at": existing.get("observed_at") or now,
        "acknowledged_by_codex": bool(existing.get("acknowledged_by_codex")),
        "terminal_assessment": terminal_assessment,
        "timing": notification_timing,
        "wake_id": wake_id,
    }
    if notify.get("mode") == "none":
        notification_timing["total"] = timing_entry(notify_start)
        record = upsert_notification(
            record,
            event_id,
            base
            | {
                "mode": "none",
                "status": "dashboard_only",
                "submit_attempted_at": now,
                "submitted_to_app_server": False,
            },
        )
        events = dict(record.get("events") or {})
        event = dict(events.get(event_id) or {})
        events[event_id] = event | {"event_id": event_id, "notification_status": "dashboard_only"}
        record["events"] = events
        return record
    if notify.get("mode") == "tmux-inject":
        bound_pane_id = tmux_state.one_line_text(record.get("codex_pane_id") or notify.get("codex_pane_id") or notify.get("codex_pane"))
        record["codex_pane_id"] = bound_pane_id
        prompt = build_tmux_inject_wake_prompt(record, candidate)
        coalesced_by_event_id = pending_tmux_inject_wake_event_id(record, event_id)
        if coalesced_by_event_id and not existing.get("submitted_to_tmux"):
            reason = f"tmux-inject wake prompt already pending for event {coalesced_by_event_id}"
            notification_timing["total"] = timing_entry(notify_start)
            record = upsert_notification(
                record,
                event_id,
                base
                | {
                    "mode": "tmux-inject",
                    "status": "coalesced",
                    "submit_attempted_at": now,
                    "submitted_to_app_server": False,
                    "submitted_to_tmux": False,
                    "injected_to_tmux": False,
                    "codex_pane_id": bound_pane_id,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "coalesced_by_event_id": coalesced_by_event_id,
                    "reason": reason,
                },
            )
            events = dict(record.get("events") or {})
            event = dict(events.get(event_id) or {})
            events[event_id] = event | {
                "event_id": event_id,
                "notification_status": "coalesced",
                "submitted_to_app_server": False,
                "submitted_to_tmux": False,
                "injected_to_tmux": False,
                "codex_pane_id": bound_pane_id,
                "wake_id": wake_id,
                "coalesced_by_event_id": coalesced_by_event_id,
                "last_error": reason,
            }
            record["events"] = events
            return record
        validation_start = monotonic_ms()
        validation = pane_codex_validation(bound_pane_id)
        notification_timing["pane_validation"] = timing_entry(validation_start, safe=validation.get("safe"), status=validation.get("status"))
        if (
            existing.get("mode") == "tmux-inject"
            and existing.get("status") in {"queued_in_codex", "awaiting_receipt", "inject_pending", "injected", "receipt_blocked"}
            and existing.get("submitted_to_tmux")
            and bound_pane_id
            and validation.get("safe")
        ):
            injection = dict(existing.get("injection") or {"pasted": True, "injected": False})
            delivery_check: dict[str, Any] | None = None
            receipt_check: dict[str, Any] | None = None
            status = "awaiting_receipt"
            reason: str | None = "tmux-inject wake prompt delivered; waiting for manager ack"
            if existing.get("status") == "inject_pending":
                delivery_start = monotonic_ms()
                delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
                notification_timing["delivery_check"] = timing_entry(delivery_start)
                if isinstance(delivery_check.get("timing"), dict):
                    notification_timing["delivery_check_detail"] = delivery_check.get("timing")
                status, reason = tmux_inject_status_after_delivery(delivery_check, injection)
            else:
                receipt_start = monotonic_ms()
                receipt_capture = capture_tmux_pane_text(bound_pane_id)
                receipt_check = tmux_inject_receipt_recheck_decision(prompt, receipt_capture, existing, record=record, candidate=candidate)
                notification_timing["receipt_check"] = timing_entry(receipt_start, action=receipt_check.get("action"), status=receipt_check.get("status"))
                action = receipt_check.get("action")
                if action == "delivery_check":
                    delivery_start = monotonic_ms()
                    delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
                    notification_timing["delivery_check"] = timing_entry(delivery_start)
                    if isinstance(delivery_check.get("timing"), dict):
                        notification_timing["delivery_check_detail"] = delivery_check.get("timing")
                    status, reason = tmux_inject_status_after_delivery(delivery_check, injection)
                elif action == "retry":
                    injection_start = monotonic_ms()
                    injection = inject_tmux_wake_prompt(bound_pane_id, prompt, record=record)
                    notification_timing["prompt_injection"] = timing_entry(injection_start, pasted=injection.get("pasted"), injected=injection.get("injected"), receipt_retry=True)
                    if isinstance(injection.get("timing"), dict):
                        notification_timing["prompt_injection_detail"] = injection.get("timing")
                    if injection.get("pasted"):
                        delivery_start = monotonic_ms()
                        delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
                        notification_timing["delivery_check"] = timing_entry(delivery_start)
                        if isinstance(delivery_check.get("timing"), dict):
                            notification_timing["delivery_check_detail"] = delivery_check.get("timing")
                        status, reason = tmux_inject_status_after_delivery(delivery_check, injection)
                    else:
                        reason = str(injection.get("reason") or "tmux receipt retry did not paste prompt")
                        blocked_status, blocked_reason = tmux_inject_status_from_blocked_injection(injection)
                        defer_reason = tmux_inject_defer_reason_from_injection(injection)
                        status = blocked_status or ("deferred" if defer_reason else "inject_pending")
                        reason = blocked_reason or defer_reason or reason
                else:
                    status = str(receipt_check.get("status") or "awaiting_receipt")
                    reason = str(receipt_check.get("reason") or "tmux-inject delivery awaits Codex ack")
            notification_timing["total"] = timing_entry(notify_start)
            fields = base | {
                "mode": "tmux-inject",
                "status": status,
                "submit_attempted_at": existing.get("submit_attempted_at") or now,
                "submitted_to_app_server": False,
                "submitted_to_tmux": True,
                "injected_to_tmux": bool(injection.get("injected")),
                "codex_pane_id": bound_pane_id,
                "prompt_sha256": existing.get("prompt_sha256") or hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "pane_validation": validation,
                "sdk_decision": existing.get("sdk_decision"),
                "injection": injection,
                "receipt_retry_count": (receipt_check or {}).get("next_retry_count", existing.get("receipt_retry_count") or 0),
                "receipt_sidecar_check_count": (receipt_check or {}).get("next_sidecar_check_count", existing.get("receipt_sidecar_check_count") or 0),
            }
            if delivery_check is not None:
                fields["delivery_check"] = delivery_check
            if receipt_check is not None:
                fields["receipt_check"] = receipt_check
                receipt_sidecar_history = append_receipt_sidecar_history(existing, receipt_check, checked_at=now)
                if receipt_sidecar_history is not None:
                    fields["receipt_sidecar_history"] = receipt_sidecar_history
                    fields["receipt_debug_summary"] = (
                        f"sidecar checks={receipt_check.get('next_sidecar_check_count', receipt_check.get('sidecar_check_count'))}; "
                        f"last action={receipt_check.get('action')}; reason={tmux_state.one_line_text(receipt_check.get('reason'))}"
                    )
            if existing.get("submitted_at"):
                fields["submitted_at"] = existing.get("submitted_at")
            if existing.get("injected_at"):
                fields["injected_at"] = existing.get("injected_at")
            if receipt_check and receipt_check.get("action") == "retry" and injection.get("pasted"):
                fields["submitted_at"] = now
                fields["injected_at"] = now
            if reason:
                fields["reason"] = reason
            if status == "deferred":
                apply_deferred_notification_fields(fields, reason=reason or "tmux-inject deferred", now=now)
            elif status == "blocked_by_other_wake":
                fields["notification_phase"] = "blocked"
                fields["blocked_at"] = now
                fields["blocked_reason"] = reason
                fields["requires_manual_resume"] = True
            record = upsert_notification(record, event_id, fields)
            append_unique_text(record, "submitted_event_ids", event_id)
            append_unique_text(record, "notified_event_ids", event_id)
            events = dict(record.get("events") or {})
            event = dict(events.get(event_id) or {})
            events[event_id] = event | {
                "event_id": event_id,
                "notification_status": status,
                "submitted_to_app_server": False,
                "submitted_to_tmux": True,
                "injected_to_tmux": bool(injection.get("injected")),
                "codex_pane_id": bound_pane_id,
                "last_error": reason,
                "notification_phase": fields.get("notification_phase"),
                "wake_id": wake_id,
                "blocked_at": fields.get("blocked_at"),
                "blocked_reason": fields.get("blocked_reason"),
                "deferred_at": fields.get("deferred_at"),
                "defer_reason": fields.get("defer_reason"),
                "requires_manual_resume": fields.get("requires_manual_resume"),
            }
            record["events"] = events
            return record
        sdk_start = monotonic_ms()
        sdk_decision = codex_sdk_inject_decision(record, candidate, validation)
        notification_timing["inject_decision"] = timing_entry(sdk_start, source=sdk_decision.get("source"), decision=sdk_decision.get("decision"))
        if isinstance(sdk_decision.get("timing"), dict):
            notification_timing["inject_decision_sidecar"] = sdk_decision.get("timing")
        status = "inject_pending"
        refused_reason = None
        injection: dict[str, Any] | None = None
        delivery_check: dict[str, Any] | None = None
        if not bound_pane_id:
            status = "inject_refused"
            refused_reason = "tmux-inject has no bound Codex pane"
        elif not validation.get("safe"):
            status = "inject_refused"
            refused_reason = str(validation.get("reason") or "Codex pane validation failed")
        elif sdk_decision.get("decision") == "refuse":
            status = "inject_refused"
            refused_reason = str(sdk_decision.get("reason") or "tmux-inject planner refused injection")
        elif (
            sdk_decision.get("decision") == "inject"
            and tmux_state.one_line_text(sdk_decision.get("target_pane"))
            and tmux_state.one_line_text(sdk_decision.get("target_pane")) != bound_pane_id
        ):
            status = "inject_refused"
            refused_reason = "tmux-inject planner selected a pane different from the bound Codex pane"
        elif sdk_decision.get("decision") == "inject":
            injection_start = monotonic_ms()
            injection = inject_tmux_wake_prompt(bound_pane_id, prompt, record=record)
            notification_timing["prompt_injection"] = timing_entry(injection_start, pasted=injection.get("pasted"), injected=injection.get("injected"))
            if isinstance(injection.get("timing"), dict):
                notification_timing["prompt_injection_detail"] = injection.get("timing")
            if injection.get("pasted"):
                delivery_start = monotonic_ms()
                delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
                notification_timing["delivery_check"] = timing_entry(delivery_start)
                if isinstance(delivery_check.get("timing"), dict):
                    notification_timing["delivery_check_detail"] = delivery_check.get("timing")
                status, refused_reason = tmux_inject_status_after_delivery(delivery_check, injection)
            else:
                refused_reason = str(injection.get("reason") or "tmux injection did not paste prompt")
                blocked_status, blocked_reason = tmux_inject_status_from_blocked_injection(injection)
                defer_reason = tmux_inject_defer_reason_from_injection(injection)
                status = blocked_status or ("deferred" if defer_reason else "inject_pending")
                refused_reason = blocked_reason or defer_reason or refused_reason
        else:
            status = "inject_pending"
            refused_reason = str(sdk_decision.get("reason") or "tmux-inject planner deferred injection")
        notification_timing["total"] = timing_entry(notify_start)
        fields = base | {
            "mode": "tmux-inject",
            "status": status,
            "submit_attempted_at": now,
            "submitted_to_app_server": False,
            "submitted_to_tmux": bool(injection and injection.get("pasted")),
            "injected_to_tmux": bool(injection and injection.get("injected")),
            "codex_pane_id": bound_pane_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "pane_validation": validation,
            "sdk_decision": sdk_decision,
        }
        if injection is not None:
            fields["injection"] = injection
        if delivery_check is not None:
            fields["delivery_check"] = delivery_check
        if refused_reason:
            fields["reason"] = refused_reason
        if status == "deferred":
            apply_deferred_notification_fields(fields, reason=refused_reason or "tmux-inject deferred", now=now)
        elif status == "blocked_by_other_wake":
            fields["notification_phase"] = "blocked"
            fields["blocked_at"] = now
            fields["blocked_reason"] = refused_reason
            fields["requires_manual_resume"] = True
        if injection and injection.get("pasted"):
            fields["submitted_at"] = now
            fields["injected_at"] = now
        record = upsert_notification(record, event_id, fields)
        if injection and injection.get("pasted"):
            append_unique_text(record, "submitted_event_ids", event_id)
            append_unique_text(record, "notified_event_ids", event_id)
        events = dict(record.get("events") or {})
        event = dict(events.get(event_id) or {})
        events[event_id] = event | {
            "event_id": event_id,
            "notification_status": status,
            "submitted_to_app_server": False,
            "submitted_to_tmux": bool(injection and injection.get("pasted")),
            "injected_to_tmux": bool(injection and injection.get("injected")),
            "codex_pane_id": bound_pane_id,
            "last_error": refused_reason,
            "notification_phase": fields.get("notification_phase"),
            "wake_id": wake_id,
            "blocked_at": fields.get("blocked_at"),
            "blocked_reason": fields.get("blocked_reason"),
            "deferred_at": fields.get("deferred_at"),
            "defer_reason": fields.get("defer_reason"),
            "requires_manual_resume": fields.get("requires_manual_resume"),
        }
        record["events"] = events
        return record
    prompt = build_manager_wake_prompt(record, candidate)
    bridge_record = {
        "endpoint": notify.get("endpoint"),
        "thread_id": notify.get("thread_id"),
        "workspace": record.get("workspace"),
    }
    try:
        delivery = tmux_bridge.deliver_bridge_candidate(bridge_record, candidate, prompt)
        record = upsert_notification(
            record,
            event_id,
            base
            | {
                "mode": "bridge",
                "status": "acknowledged" if existing.get("acknowledged_by_codex") else "awaiting_ack",
                "submit_attempted_at": now,
                "submitted_at": now,
                "submitted_to_app_server": True,
                "prompt_sha256": delivery.get("prompt_sha256"),
                "delivery": delivery,
            },
        )
        append_unique_text(record, "submitted_event_ids", event_id)
        append_unique_text(record, "notified_event_ids", event_id)
        events = dict(record.get("events") or {})
        event = dict(events.get(event_id) or {})
        events[event_id] = event | {
            "event_id": event_id,
            "notification_status": "awaiting_ack" if not existing.get("acknowledged_by_codex") else "acknowledged",
            "submitted_to_app_server": True,
            "submitted_at": now,
        }
        record["events"] = events
    except Exception as exc:
        record = upsert_notification(
            record,
            event_id,
            base
            | {
                "mode": "bridge",
                "status": "submission_failed",
                "submit_attempted_at": now,
                "submitted_to_app_server": False,
                "error": str(exc),
            },
        )
        events = dict(record.get("events") or {})
        event = dict(events.get(event_id) or {})
        events[event_id] = event | {
            "event_id": event_id,
            "notification_status": "submission_failed",
            "submitted_to_app_server": False,
            "last_error": str(exc),
        }
        record["events"] = events
    return record


def transition_terminal(
    record: dict[str, Any],
    *,
    paths: dict[str, Path],
    status: dict[str, Any] | None,
    worker_missing: bool = False,
    job_id: str | None = None,
    pane_id: str | None = None,
) -> dict[str, Any]:
    target_job_id = optional_safe_id(job_id) or optional_safe_id(record.get("current_job_id"))
    target_pane_id = tmux_state.one_line_text(pane_id) or job_pane_id(record, target_job_id)
    target_pane_index = job_pane_index(record, target_job_id)
    if worker_missing:
        event_id = worker_missing_event_id(record, target_job_id, target_pane_id)
        candidate = {
            "source": "manager_worker_missing",
            "event_id": event_id,
            "job_id": target_job_id,
            "pane_id": target_pane_id,
            "pane_index": target_pane_index,
            "status": "worker_pane_missing",
            "status_path": str(tmux_state.status_path(paths, target_job_id)) if target_job_id else None,
            "task_path": task_path_for_job(paths, target_job_id),
            "log_path": str(tmux_state.log_path(paths, target_job_id)) if target_job_id else None,
        }
    elif status is not None and tmux_state.is_terminal(status):
        event_id = str(status.get("event_id") or tmux_state.terminal_event_id(status))
        target_job_id = optional_safe_id(status.get("id")) or target_job_id
        target_pane_id = target_pane_id or job_pane_id(record, target_job_id)
        target_pane_index = job_pane_index(record, target_job_id)
        candidate = {
            "source": "manager_terminal",
            "event_id": event_id,
            "job_id": target_job_id,
            "pane_id": target_pane_id,
            "pane_index": target_pane_index,
            "status": status.get("status"),
            "exit_code": status.get("exit_code"),
            "last_output": tmux_state.tail_text(str(status.get("last_output") or ""), limit=2000),
            "status_path": status.get("status_path"),
            "task_path": task_path_for_job(paths, target_job_id),
            "log_path": status.get("log_path"),
        }
    else:
        return record
    jobs = dict(record.get("jobs") or {})
    job = dict(jobs.get(target_job_id) or {})
    if "manager_sequence" in job:
        candidate["manager_sequence"] = job["manager_sequence"]
        if status is not None:
            status["manager_sequence"] = job["manager_sequence"]
    record["last_terminal_event_id"] = candidate["event_id"]
    record["last_terminal_candidate"] = candidate
    events = dict(record.get("events") or {})
    existing_event = events.get(str(candidate["event_id"])) if isinstance(events.get(str(candidate["event_id"])), dict) else {}
    events[str(candidate["event_id"])] = dict(existing_event) | candidate | {
        "observed_at": existing_event.get("observed_at") or tmux_state.utc_now(),
        "acknowledged_by_codex": bool(existing_event.get("acknowledged_by_codex")),
    }
    record["events"] = events
    jobs = dict(record.get("jobs") or {})
    job = dict(jobs.get(target_job_id) or {})
    job["job_id"] = target_job_id
    if target_pane_id:
        job["pane_id"] = target_pane_id
    if candidate.get("pane_index"):
        job["pane_index"] = candidate.get("pane_index")
    job["status"] = candidate.get("status") or ("worker_pane_missing" if worker_missing else "terminal")
    job["terminal_event_id"] = candidate["event_id"]
    job["terminal_at"] = events[str(candidate["event_id"])]["observed_at"]
    if candidate.get("status_path"):
        job["status_path"] = candidate.get("status_path")
    if candidate.get("log_path"):
        job["log_path"] = candidate.get("log_path")
    jobs[target_job_id] = job
    record["jobs"] = jobs
    record["active_job_ids"] = [value for value in active_job_ids(record) if value != target_job_id]
    record = notify_terminal_event(record, candidate)
    return refresh_aggregate_status(record)


def manager_cycle(record: dict[str, Any], *, paths: dict[str, Path]) -> dict[str, Any]:
    record = normalize_manager_record(record, paths)
    record["heartbeat_at"] = tmux_state.utc_now()
    if record.get("status") == "cancelled":
        return record
    if record.get("status") == "cancel_requested":
        record["status"] = "cancelled"
        return record
    if record.get("pending_job"):
        record = start_pending_job(record)
    record = enforce_log_retention(record)
    notify = record.get("notify") if isinstance(record.get("notify"), dict) else {}
    if notify.get("mode") == "bridge":
        events = record.get("events") if isinstance(record.get("events"), dict) else {}
        for event_id, event in list(events.items()):
            if event_id in list(record.get("submitted_event_ids") or []):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("source") not in {"manager_terminal", "manager_worker_missing"}:
                continue
            record = notify_terminal_event(record, event)
    elif notify.get("mode") == "tmux-inject":
        events = record.get("events") if isinstance(record.get("events"), dict) else {}
        for event_id, event in list(events.items()):
            if not isinstance(event, dict):
                continue
            if event.get("source") not in {"manager_terminal", "manager_worker_missing"}:
                continue
            if event.get("acknowledged_by_codex"):
                continue
            notification = notification_for_event(record, event_id)
            if isinstance(notification, dict) and (
                notification.get("status") == "deferred" or notification.get("requires_manual_resume")
            ):
                continue
            if notification is None or tmux_inject_ack_recheck_due(notification):
                record = notify_terminal_event(record, event)

    for job_id in list(active_job_ids(record)):
        pane_id = job_pane_id(record, job_id)
        if pane_id and not pane_exists(pane_id):
            record = transition_terminal(record, paths=paths, status=None, worker_missing=True, job_id=job_id, pane_id=pane_id)
            continue
        status = load_job_status(paths, job_id)
        if status and tmux_state.is_terminal(status):
            record = transition_terminal(record, paths=paths, status=status, job_id=job_id, pane_id=pane_id)
    return refresh_aggregate_status(record)


def merge_external_ack_fields(record: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    if not latest:
        return record
    merged = dict(record)
    original_last_notification = (
        dict(record["last_notification"]) if isinstance(record.get("last_notification"), dict) else None
    )
    latest_ack = latest.get("last_ack") if isinstance(latest.get("last_ack"), dict) else None
    if latest_ack:
        merged["last_ack"] = dict(latest_ack)
    for notification in latest.get("notifications", []):
        if not isinstance(notification, dict):
            continue
        event_id = str(notification.get("event_id") or "")
        if not event_id:
            continue
        if notification.get("acknowledged_by_codex") or str(notification.get("status") or "").startswith("handled"):
            merged = upsert_notification(merged, event_id, dict(notification))
            merged = sync_event_from_notification(merged, event_id, dict(notification))
    latest_events = latest.get("events") if isinstance(latest.get("events"), dict) else {}
    merged_events = dict(merged.get("events") or {})
    for event_id, latest_event in latest_events.items():
        if not isinstance(latest_event, dict):
            continue
        if latest_event.get("acknowledged_by_codex") or latest_event.get("handled_at") or latest_event.get("handled_by_job_id"):
            current_event = dict(merged_events.get(str(event_id)) or {})
            merged_events[str(event_id)] = current_event | dict(latest_event)
    merged["events"] = merged_events
    latest_verification = latest.get("bridge_verification") if isinstance(latest.get("bridge_verification"), dict) else None
    if latest_verification and latest_verification.get("event_id"):
        current_verification = merged.get("bridge_verification") if isinstance(merged.get("bridge_verification"), dict) else {}
        if (
            latest_verification.get("acknowledged_by_codex")
            or latest_verification.get("status") in {"verified", "expired", "mismatched_config", "submission_failed"}
            or latest_verification.get("event_id") == current_verification.get("event_id")
        ):
            merged["bridge_verification"] = dict(current_verification) | dict(latest_verification)
    if original_last_notification and original_last_notification.get("event_id"):
        event_id = str(original_last_notification.get("event_id") or "")
        merged["last_notification"] = notification_for_event(merged, event_id) or original_last_notification
    return merged


def merge_external_manager_update(record: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
    if not latest:
        return record
    record = merge_external_ack_fields(record, latest)
    if manager_cancel_state(latest) and record.get("status") != "cancelled":
        merged = merge_external_cancel_state(record, latest)
        merged["heartbeat_at"] = record.get("heartbeat_at")
        return merged
    if manager_cancel_state(record):
        return record
    latest_pending = latest.get("pending_job") if isinstance(latest.get("pending_job"), dict) else None
    latest_pending_job_id = optional_safe_id(latest_pending.get("job_id")) if latest_pending else ""
    job_already_seen = latest_pending_job_id and (
        latest_pending_job_id in list(record.get("job_ids") or [])
        or latest_pending_job_id in (record.get("jobs") if isinstance(record.get("jobs"), dict) else {})
    )
    if latest_pending and not job_already_seen and not record.get("pending_job") and record.get("status") in {"waiting_for_codex", "idle"}:
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
    current_job_id = str(record.get("current_job_id") or "")
    job_status = status_with_manager_metadata(load_job_status(paths, current_job_id), record, current_job_id)
    active_statuses = {
        job_id: status_with_manager_metadata(load_job_status(paths, job_id), record, job_id)
        for job_id in active_job_ids(record)
    }
    return {
        "manager_id": item_id,
        "found": True,
        "record": record,
        "current_job_status": job_status,
        "active_job_statuses": active_statuses,
    }


def cancel_manager(
    manager_id: str,
    *,
    workspace: str | None = None,
    state_dir: str | None = None,
    stop_worker: bool = False,
    job_id: str | None = None,
    all_workers: bool = False,
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    item_id = manager_id_value(manager_id)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "cancelled": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "cancelled": False, "reason": "manager record not found"}
    target_job_id = optional_safe_id(job_id)
    worker_stop_result = None
    worker_stop_results: list[dict[str, Any]] = []
    if target_job_id:
        target_pane_id = job_pane_id(record, target_job_id)
        worker_stop_result = send_worker_interrupt(target_pane_id)
        worker_stop_results.append({"job_id": target_job_id, "pane_id": target_pane_id, "result": worker_stop_result})
    elif stop_worker or all_workers:
        pane_ids = list(record.get("worker_pane_ids") or [])
        if stop_worker and not pane_ids and record.get("worker_pane_id"):
            pane_ids = [str(record.get("worker_pane_id"))]
        for pane_id in unique_text_values(pane_ids):
            result = send_worker_interrupt(pane_id)
            worker_stop_results.append({"pane_id": pane_id, "result": result})
        if len(worker_stop_results) == 1:
            worker_stop_result = worker_stop_results[0]["result"]
    record["status"] = "cancel_requested"
    record["cancel_requested_at"] = tmux_state.utc_now()
    record["stop_worker_requested"] = bool(stop_worker or target_job_id or all_workers)
    record["cancel_job_id"] = target_job_id or None
    record["all_workers_stop_requested"] = bool(all_workers)
    record["worker_stop_result"] = worker_stop_result
    record["worker_stop_results"] = worker_stop_results
    record = write_manager_record(paths, record)
    return {
        "manager_id": item_id,
        "cancelled": True,
        "stop_worker": bool(stop_worker or target_job_id or all_workers),
        "job_id": target_job_id or None,
        "all_workers": bool(all_workers),
        "worker_stop_result": worker_stop_result,
        "worker_stop_results": worker_stop_results,
        "manager_path": record["manager_path"],
        "record": record,
    }


def cleanup_path_allowed(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def delete_terminal_jobs(
    manager_id: str,
    *,
    workspace: str | None = None,
    state_dir: str | None = None,
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    paths = manager_paths(workspace, state_dir)
    item_id = manager_id_value(manager_id)
    record, error = read_manager_record(paths, item_id)
    if error:
        return {"manager_id": item_id, "deleted": False, "reason": error}
    if record is None:
        return {"manager_id": item_id, "deleted": False, "reason": "manager record not found"}

    requested = {optional_safe_id(value) for value in (job_ids or []) if optional_safe_id(value)}
    active = set(active_job_ids(record))
    jobs = dict(record.get("jobs") or {})
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    removed: list[str] = []
    missing: list[str] = []
    path_errors: list[dict[str, str]] = []

    for job_id in list(record.get("job_ids") or []):
        safe_job_id = optional_safe_id(job_id)
        if not safe_job_id or (requested and safe_job_id not in requested):
            continue
        if safe_job_id in active:
            skipped.append({"job_id": safe_job_id, "reason": "active job"})
            continue
        job = jobs.get(safe_job_id) if isinstance(jobs.get(safe_job_id), dict) else {}
        status = tmux_state.token_text(job.get("status"))
        if status not in MANAGER_DELETABLE_JOB_STATUSES:
            skipped.append({"job_id": safe_job_id, "reason": f"non-terminal status: {status or 'unknown'}"})
            continue

        candidates = [
            tmux_state.command_path(paths, safe_job_id),
            tmux_state.status_path(paths, safe_job_id),
            tmux_state.log_path(paths, safe_job_id),
        ]
        for key in ("command_request_path", "status_path", "log_path"):
            if job.get(key):
                candidates.append(Path(str(job[key])))
        run_result = job.get("run_result") if isinstance(job.get("run_result"), dict) else {}
        for key in ("command_path", "status_path", "log_path"):
            if run_result.get(key):
                candidates.append(Path(str(run_result[key])))

        seen_paths: set[str] = set()
        for candidate in candidates:
            path = candidate.expanduser()
            path_key = str(path)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            if not cleanup_path_allowed(path, paths["root"]):
                path_errors.append({"job_id": safe_job_id, "path": path_key, "reason": "outside state directory"})
                continue
            try:
                path.unlink()
                removed.append(path_key)
            except FileNotFoundError:
                missing.append(path_key)
            except IsADirectoryError:
                path_errors.append({"job_id": safe_job_id, "path": path_key, "reason": "is a directory"})
            except OSError as exc:
                path_errors.append({"job_id": safe_job_id, "path": path_key, "reason": str(exc)})

        jobs.pop(safe_job_id, None)
        deleted.append(safe_job_id)

    if deleted:
        deleted_set = set(deleted)
        record["jobs"] = jobs
        record["job_ids"] = [value for value in list(record.get("job_ids") or []) if value not in deleted_set]
        record["active_job_ids"] = [value for value in active_job_ids(record) if value not in deleted_set]
        record["events"] = {
            event_id: event
            for event_id, event in dict(record.get("events") or {}).items()
            if not (isinstance(event, dict) and optional_safe_id(event.get("job_id")) in deleted_set)
        }
        record["notifications"] = [
            notification
            for notification in list(record.get("notifications") or [])
            if not (isinstance(notification, dict) and optional_safe_id(notification.get("job_id")) in deleted_set)
        ]
        if optional_safe_id(record.get("current_job_id")) in deleted_set:
            record["current_job_id"] = active_job_ids(record)[-1] if active_job_ids(record) else None
        if str(record.get("last_terminal_event_id") or "") not in record.get("events", {}):
            record["last_terminal_event_id"] = None
            record["last_terminal_candidate"] = None
        if isinstance(record.get("last_notification"), dict) and optional_safe_id(record["last_notification"].get("job_id")) in deleted_set:
            record["last_notification"] = None
        record = refresh_aggregate_status(record)
        record = write_manager_record(paths, record)

    return {
        "manager_id": item_id,
        "deleted": bool(deleted) and not path_errors,
        "deleted_job_ids": deleted,
        "skipped": skipped,
        "removed": removed,
        "missing": missing,
        "path_errors": path_errors,
        "manager_path": str(manager_record_path(paths, item_id)),
        "record": record,
    }


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

    candidates: list[Path] = [
        manager_record_path(paths, item_id),
        manager_dashboard_path(paths, item_id),
        manager_dashboard_viewer_state_path(paths, item_id),
    ]
    if record.get("dashboard_viewer_state_path"):
        candidates.append(Path(str(record["dashboard_viewer_state_path"])))
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


def dashboard_cell(value: Any, width: int) -> str:
    text = str(value if value is not None and value != "" else "-")
    if len(text) > width:
        text = text[: max(1, width - 1)] + "~"
    return text.ljust(width)


def dashboard_short_id(value: Any, width: int = 24) -> str:
    text = tmux_state.one_line_text(value) or "-"
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "~"


def dashboard_parse_time(value: Any) -> datetime | None:
    text = tmux_state.one_line_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dashboard_age_text(value: Any) -> str:
    parsed = dashboard_parse_time(value)
    if parsed is None:
        return "none"
    age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if age_seconds < 60:
        return f"{age_seconds}s ago"
    age_minutes = age_seconds // 60
    if age_minutes < 60:
        return f"{age_minutes}m ago"
    age_hours = age_minutes // 60
    if age_hours < 48:
        return f"{age_hours}h ago"
    return f"{age_hours // 24}d ago"


def dashboard_pane_label(record: dict[str, Any], job_id: str, job: dict[str, Any] | None = None) -> str:
    job = job if isinstance(job, dict) else {}
    pane_id = tmux_state.one_line_text(job.get("pane_id")) or job_pane_id(record, job_id)
    pane_index = tmux_state.one_line_text(job.get("pane_index")) or job_pane_index(record, job_id)
    if pane_id and pane_index:
        return f"{pane_index}:{pane_id}"
    return pane_id or "-"


def dashboard_event_ack(record: dict[str, Any], event: dict[str, Any]) -> str:
    event_id = str(event.get("event_id") or "")
    notification = notification_for_event(record, event_id) or {}
    return "yes" if event.get("acknowledged_by_codex") or notification.get("acknowledged_by_codex") else "no"


def dashboard_event_notify(record: dict[str, Any], event: dict[str, Any]) -> str:
    event_id = str(event.get("event_id") or "")
    notification = notification_for_event(record, event_id) or {}
    if notification.get("submitted_to_app_server"):
        return "yes"
    return tmux_state.token_text(notification.get("status")) or "none"


def dashboard_recent_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    events = record.get("events") if isinstance(record.get("events"), dict) else {}
    return [event for event in events.values() if isinstance(event, dict)]


def dashboard_latest_event(record: dict[str, Any]) -> dict[str, Any] | None:
    events = record.get("events") if isinstance(record.get("events"), dict) else {}
    last_event_id = tmux_state.one_line_text(record.get("last_terminal_event_id"))
    if last_event_id and isinstance(events.get(last_event_id), dict):
        return events[last_event_id]
    recent = dashboard_recent_events(record)
    return recent[-1] if recent else None


def dashboard_summary_lines(record: dict[str, Any]) -> list[str]:
    record = normalize_manager_record(record)
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    active_ids = active_job_ids(record)
    events = dashboard_recent_events(record)
    failed_events = sum(1 for event in events if event.get("status") == "failed")
    waiting_events = sum(1 for event in events if dashboard_event_ack(record, event) != "yes")
    lines = [
        f"manager  {dashboard_short_id(record.get('manager_id'))}  {record.get('status')}",
        (
            f"heartbeat {dashboard_age_text(record.get('heartbeat_at'))}  "
            f"jobs {len(active_ids)} active / {len(record.get('job_ids') or [])} total  "
            f"events failed {failed_events} waiting {waiting_events}"
        ),
    ]
    event = dashboard_latest_event(record)
    if event:
        lines.append(
            f"LATEST EVENT  {dashboard_cell(event.get('status') or '-', 8)} "
            f"{dashboard_cell(event.get('event_id') or '-', 14)} "
            f"{dashboard_cell(event.get('job_id') or '-', 12)} "
            f"notify={dashboard_event_notify(record, event)} ack={dashboard_event_ack(record, event)}"
        )
    else:
        lines.append(f"LATEST EVENT  {dashboard_cell('-', 8)} {dashboard_cell('-', 14)} {dashboard_cell('-', 12)} notify=none ack=no")

    lines.append("ACTIVE")
    if active_ids:
        for job_id in active_ids[:3]:
            job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
            lines.append(f"{dashboard_cell(job_id, 14)} {dashboard_cell(dashboard_pane_label(record, job_id, job), 9)} {job.get('status') or 'running'}")
    else:
        lines.append(f"{dashboard_cell('-', 14)} {dashboard_cell('-', 9)} idle")
    return lines


def dashboard_jobs_lines(record: dict[str, Any]) -> list[str]:
    record = normalize_manager_record(record)
    jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
    active = set(active_job_ids(record))
    job_ids = list(active_job_ids(record))
    for job_id in list(record.get("job_ids") or [])[-8:]:
        if job_id not in job_ids:
            job_ids.append(job_id)
    lines = [
        f"tmux-skills manager  {dashboard_short_id(record.get('manager_id'))}  jobs",
        f"{dashboard_cell('job_id', 16)} {dashboard_cell('pane', 9)} {dashboard_cell('status', 12)} {dashboard_cell('event', 14)}",
    ]
    if not job_ids:
        lines.append(f"{dashboard_cell('-', 16)} {dashboard_cell('-', 9)} {dashboard_cell('idle', 12)} {dashboard_cell('-', 14)}")
        return lines
    for job_id in job_ids[:10]:
        job = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
        status_text = job.get("status") or ("running" if job_id in active else "unknown")
        lines.append(
            f"{dashboard_cell(job_id, 16)} "
            f"{dashboard_cell(dashboard_pane_label(record, job_id, job), 9)} "
            f"{dashboard_cell(status_text, 12)} "
            f"{dashboard_cell(job.get('terminal_event_id') or '-', 14)}"
        )
    return lines


def dashboard_events_lines(record: dict[str, Any]) -> list[str]:
    record = normalize_manager_record(record)
    events = dashboard_recent_events(record)[-10:]
    lines = [
        f"tmux-skills manager  {dashboard_short_id(record.get('manager_id'))}  events",
        f"{dashboard_cell('event_id', 16)} {dashboard_cell('job_id', 14)} {dashboard_cell('status', 10)} {dashboard_cell('notify', 8)} {dashboard_cell('ack', 4)}",
    ]
    if not events:
        lines.append(f"{dashboard_cell('-', 16)} {dashboard_cell('-', 14)} {dashboard_cell('-', 10)} {dashboard_cell('-', 8)} {dashboard_cell('-', 4)}")
        return lines
    for event in events:
        lines.append(
            f"{dashboard_cell(event.get('event_id') or '-', 16)} "
            f"{dashboard_cell(event.get('job_id') or '-', 14)} "
            f"{dashboard_cell(event.get('status') or '-', 10)} "
            f"{dashboard_cell(dashboard_event_notify(record, event), 8)} "
            f"{dashboard_cell(dashboard_event_ack(record, event), 4)}"
        )
    return lines


def clip_dashboard_lines(lines: list[str], width: int | None = None, height: int | None = None) -> list[str]:
    max_width = max(1, int(width)) if width else None
    max_height = max(1, int(height)) if height else None
    clipped = [line[:max_width] if max_width is not None else line for line in lines]
    return clipped[:max_height] if max_height is not None else clipped


def dashboard_text(
    record: dict[str, Any],
    job_status: dict[str, Any] | None = None,
    *,
    mode: str = "summary",
    width: int | None = None,
    height: int | None = None,
) -> str:
    selected_mode = mode if mode in DASHBOARD_MODES else "summary"
    if selected_mode == "jobs":
        lines = dashboard_jobs_lines(record)
    elif selected_mode == "events":
        lines = dashboard_events_lines(record)
    else:
        lines = dashboard_summary_lines(record)
    return "\n".join(clip_dashboard_lines(lines, width, height))


def dashboard_loop(args: argparse.Namespace) -> int:
    paths = manager_paths(args.workspace, args.state_dir)
    item_id = manager_id_value(args.manager_id)
    dashboard_file = Path(args.dashboard_file).expanduser().resolve() if getattr(args, "dashboard_file", None) else None
    viewer_ensured = False
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
        latest, _latest_error = read_manager_record(paths, item_id)
        merged = merge_external_manager_update(record, latest)
        if merged is not record:
            record = manager_cycle(merged, paths=paths)
        if dashboard_file:
            text = dashboard_text(record)
            write_dashboard_file(dashboard_file, text)
            if not viewer_ensured and record.get("status") != "cancelled":
                record, viewer_result = ensure_dashboard_viewer(record, paths)
                viewer_ensured = True
                if viewer_result.get("reason"):
                    record["last_dashboard_viewer_error"] = viewer_result["reason"]
            record = refresh_dashboard_viewer_fields(record, paths)
        record = write_manager_record(paths, record)
        job_status = load_job_status(paths, str(record.get("current_job_id") or ""))
        text = dashboard_text(record, job_status)
        if dashboard_file:
            write_dashboard_file(dashboard_file, text)
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
