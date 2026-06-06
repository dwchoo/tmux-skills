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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_app_server_client
import tmux_bridge
import tmux_state


MANAGER_VERSION = 1
DEFAULT_MANAGER_LOG_MAX_BYTES = 65536
MANAGER_STATUSES = {"starting", "idle", "queued", "running", "waiting_for_codex", "cancel_requested", "cancelled", "failed"}
MANAGER_CANCEL_STATUSES = {"cancel_requested", "cancelled"}
MANAGER_PROCESS_MODES = {"foreground", "background"}
MANAGER_DASHBOARD_RENDERERS = {"pane", "none"}
DASHBOARD_MODES = ("summary", "jobs", "events")
MANAGER_PS_POC_STATUS_UNSUPPORTED = "unsupported_by_current_codex_surface"
MANAGER_PS_POC_STATUS_VERIFIED = "verified"
MANAGER_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "stopped", "timeout", "cancelled", "stale"}
MANAGER_DELETABLE_JOB_STATUSES = MANAGER_TERMINAL_JOB_STATUSES | {"complete", "completed", "error"}
BRIDGE_VERIFICATION_STATUSES = {"unverified", "awaiting_ack", "verified", "expired", "mismatched_config", "submission_failed"}
TMUX_INJECT_NOTIFICATION_STATUSES = {"injected", "inject_pending", "inject_refused"}
TMUX_INJECT_PRIMARY_SUBMIT_KEY = "C-m"
TMUX_INJECT_FOLLOWUP_SUBMIT_KEY = "C-m"
TMUX_INJECT_ACK_RECHECK_SECONDS = 5.0
CODEX_SDK_REASONING_EFFORT = "low"
TMUX_INJECT_WAKE_PROMPT = "\n".join(
    [
        "tmux-skills manager event is ready.",
        "",
        "Manager ID: {manager_id}",
        "Event ID: {event_id}",
        "",
        "Inspect the manager state for this workspace, decide the next action, and acknowledge the event after inspection.",
    ]
)


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


def dashboard_viewer_state(paths: dict[str, Path], manager_id: str) -> tuple[dict[str, Any] | None, str | None]:
    return tmux_state.read_json(manager_dashboard_viewer_state_path(paths, manager_id))


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
    viewer_script = script_dir() / "tmux_manager_viewer.py"
    poll_seconds = str(record.get("poll_seconds") or 2.0)
    command_args = [
        sys.executable or "python3",
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


def render_dashboard_to_pane(pane_id: str | None, dashboard_file: Path) -> dict[str, Any]:
    return {
        "rendered": False,
        "reason": "manager dashboards are rendered by tmux_manager_viewer.py",
        "pane_id": pane_id,
        "dashboard_path": str(dashboard_file),
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
    override = tmux_state.one_line_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_MODEL"))
    if override:
        return override
    for env_name in ("CODEX_MODEL", "OPENAI_MODEL"):
        value = tmux_state.one_line_text(os.environ.get(env_name))
        if value:
            return value
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        return tmux_state.one_line_text(data.get("model"))
    return None


def tmux_inject_ack_recheck_seconds() -> float:
    value = os.environ.get("TMUX_SKILLS_TMUX_INJECT_ACK_RECHECK_SECONDS")
    if value is None:
        return TMUX_INJECT_ACK_RECHECK_SECONDS
    try:
        return max(0.0, float(value))
    except ValueError:
        return TMUX_INJECT_ACK_RECHECK_SECONDS


def tmux_inject_ack_recheck_due(notification: dict[str, Any] | None) -> bool:
    if not isinstance(notification, dict):
        return True
    if notification.get("acknowledged_by_codex"):
        return False
    if notification.get("mode") != "tmux-inject":
        return False
    if notification.get("status") not in {"injected", "inject_pending"}:
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


def pending_followup_decision(reason: str, *, prompt: str, capture_output: str, source: str) -> dict[str, Any]:
    action = "submit" if wake_prompt_still_staged(prompt, capture_output) else "defer"
    return normalize_tmux_inject_followup_decision(
        {"action": action, "submit_key": TMUX_INJECT_FOLLOWUP_SUBMIT_KEY, "confidence": 0.0, "reason": reason},
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
    if notify.get("mode") != "bridge":
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
) -> dict[str, Any]:
    return {
        "job_id": tmux_state.safe_id(job_id),
        "command_file": str(command_request_path),
        "cwd": cwd,
        "pane_id": tmux_state.one_line_text(pane_id),
        "pane_index": tmux_state.one_line_text(pane_index),
        "queued_at": tmux_state.utc_now(),
    }


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
        return {"manager_id": manager_id, "queued": False, "reason": "manager run-next requires nonblank --job-id"}
    paths = manager_paths(workspace, state_dir)
    record, error = read_manager_record(paths, manager_id)
    item_id = tmux_state.safe_id(job_id)
    if error:
        return {"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": error}
    if record is None:
        return {"manager_id": manager_id_value(manager_id), "job_id": item_id, "queued": False, "reason": "manager record not found"}
    target_pane_id = tmux_state.one_line_text(pane_id) or tmux_state.one_line_text(record.get("worker_pane_id"))
    if not target_pane_id:
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager has no worker pane"}
    if record.get("pending_job"):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager already has a pending job"}
    if not allow_parallel and (record.get("status") == "running" or active_job_ids(record)):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager already has active jobs"}
    if pane_has_active_job(record, target_pane_id, excluding_job_id=item_id):
        return {
            "manager_id": record["manager_id"],
            "job_id": item_id,
            "queued": False,
            "reason": f"worker pane already has an active job: {target_pane_id}",
        }
    allowed, gate_reason = manager_queue_gate(record)
    if not allowed:
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": gate_reason}

    text, read_error = command_text_from_source(command_text, command_file)
    if read_error:
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": read_error}
    if not tmux_state.one_line_text(text):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "command is blank"}

    record = preserve_external_cancel_state(paths, record)
    if manager_cancel_state(record):
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager cancellation is requested"}

    request_path = write_command_request(paths, record["manager_id"], item_id, str(text))
    record = mark_last_terminal_event_handled(record, next_job_id=item_id)
    target_pane_index = tmux_state.one_line_text(pane_index)
    if not target_pane_index and target_pane_id == tmux_state.one_line_text(record.get("worker_pane_id")):
        target_pane_index = tmux_state.one_line_text(record.get("worker_pane_index"))
    record["pending_job"] = build_pending_job(item_id, request_path, cwd, target_pane_id, target_pane_index)
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
        return {"manager_id": record["manager_id"], "job_id": item_id, "queued": False, "reason": "manager cancellation is requested"}
    record = write_manager_record(paths, record)
    return {
        "manager_id": record["manager_id"],
        "job_id": item_id,
        "queued": True,
        "manager_path": record["manager_path"],
        "command_request_path": str(request_path),
        "record": record,
    }


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
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    bound_pane_id = tmux_state.one_line_text(record.get("codex_pane_id"))
    fixture_decision = tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_DECISION"))
    if fixture_decision:
        return normalize_tmux_inject_sdk_decision(
            {
                "decision": fixture_decision,
                "target_pane": os.environ.get("TMUX_SKILLS_CODEX_SDK_TARGET_PANE") or bound_pane_id,
                "confidence": os.environ.get("TMUX_SKILLS_CODEX_SDK_CONFIDENCE") or 1.0,
                "reason": "environment-supplied Codex SDK planner decision",
            },
            bound_pane_id,
        ) | {"source": "env"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {
            "decision": "defer",
            "target_pane": bound_pane_id,
            "confidence": 0.0,
            "reason": "Codex SDK unavailable: OPENAI_API_KEY is not set",
            "source": "sdk_unavailable",
        }
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        return {
            "decision": "defer",
            "target_pane": bound_pane_id,
            "confidence": 0.0,
            "reason": f"Codex SDK unavailable: {exc}",
            "source": "sdk_unavailable",
        }
    model = codex_sdk_model_name()
    if not model:
        return {
            "decision": "defer",
            "target_pane": bound_pane_id,
            "confidence": 0.0,
            "reason": "Codex SDK model is not configured",
            "source": "sdk_unavailable",
        }
    prompt = json.dumps(
        {
            "task": "Decide whether tmux-skills may inject a wake prompt into the bound Codex pane.",
            "allowed_decisions": ["inject", "defer", "refuse"],
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "bound_pane_id": bound_pane_id,
            "pane_validation": validation,
            "rules": [
                "Return only JSON with decision, target_pane, confidence, and reason.",
                "Do not execute tmux commands or describe shell commands.",
                "Choose inject only when the bound pane is the target and validation is safe.",
            ],
        },
        sort_keys=True,
    )
    schema = {
        "type": "json_schema",
        "name": "tmux_inject_decision",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "target_pane", "confidence", "reason"],
            "properties": {
                "decision": {"type": "string", "enum": ["inject", "defer", "refuse"]},
                "target_pane": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        },
        "strict": True,
    }
    try:
        client = OpenAI(timeout=timeout_seconds)
        response = client.responses.create(
            model=model,
            input=prompt,
            reasoning={"effort": CODEX_SDK_REASONING_EFFORT},
            text={"format": schema},
        )
        output_text = getattr(response, "output_text", "")
        payload = json.loads(output_text)
    except Exception as exc:
        return {
            "decision": "defer",
            "target_pane": bound_pane_id,
            "confidence": 0.0,
            "reason": f"Codex SDK planner failed: {exc}",
            "source": "sdk_error",
        }
    return normalize_tmux_inject_sdk_decision(payload, bound_pane_id) | {"source": "sdk"}


def build_tmux_inject_wake_prompt(record: dict[str, Any], candidate: dict[str, Any]) -> str:
    return TMUX_INJECT_WAKE_PROMPT.format(
        manager_id=record.get("manager_id") or "unknown",
        event_id=candidate.get("event_id") or "unknown",
    )


def capture_tmux_pane_text(pane_id: str, *, lines: int = 80, max_chars: int = 12000) -> dict[str, Any]:
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return {"captured": False, "reason": "pane id is blank", "output": ""}
    proc = subprocess.run(
        [*tmux_command_prefix(), "capture-pane", "-p", "-t", target, "-S", f"-{max(1, lines)}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = proc.stdout if proc.returncode == 0 else ""
    omitted = max(0, len(output) - max_chars)
    if omitted:
        output = output[-max_chars:]
    return {
        "captured": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output,
        "omitted_chars": omitted,
        "reason": None if proc.returncode == 0 else (proc.stderr.strip() or f"tmux capture-pane exited {proc.returncode}"),
    }


def wake_prompt_still_staged(prompt: str, capture_output: str) -> bool:
    if not prompt or not capture_output:
        return False
    tail = "\n".join(capture_output.splitlines()[-30:])
    if "• Working" in tail or "Working (" in tail:
        return False
    first_line = prompt.splitlines()[0]
    has_prompt = first_line in tail and "Manager ID:" in tail and "Event ID:" in tail
    footer_hint = "queue message" in tail or "to submit message" in tail or "Context" in tail
    return bool(has_prompt and footer_hint)


def normalize_tmux_inject_followup_decision(value: Any, *, prompt: str, capture_output: str) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    action = tmux_state.token_text(payload.get("action")) or ""
    if action not in {"confirmed", "submit", "defer", "refuse"}:
        action = "submit" if wake_prompt_still_staged(prompt, capture_output) else "confirmed"
    submit_key = tmux_state.one_line_text(payload.get("submit_key")) or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "action": action,
        "submit_key": submit_key,
        "confidence": min(max(confidence, 0.0), 1.0),
        "reason": tmux_state.one_line_text(payload.get("reason")) or "heuristic post-injection decision",
    }


def codex_sdk_inject_followup_decision(
    record: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
    injection: dict[str, Any],
    capture: dict[str, Any],
    prompt: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    output = str(capture.get("output") or "")
    fixture_action = tmux_state.token_text(os.environ.get("TMUX_SKILLS_CODEX_SDK_FOLLOWUP_ACTION"))
    if fixture_action:
        return normalize_tmux_inject_followup_decision(
            {
                "action": fixture_action,
                "submit_key": os.environ.get("TMUX_SKILLS_CODEX_SDK_FOLLOWUP_SUBMIT_KEY") or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY,
                "confidence": os.environ.get("TMUX_SKILLS_CODEX_SDK_CONFIDENCE") or 1.0,
                "reason": "environment-supplied Codex SDK follow-up decision",
            },
            prompt=prompt,
            capture_output=output,
        ) | {"source": "env"}
    if not os.environ.get("OPENAI_API_KEY"):
        return pending_followup_decision(
            "Codex SDK unavailable: OPENAI_API_KEY is not set",
            prompt=prompt,
            capture_output=output,
            source="sdk_unavailable",
        )
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        return pending_followup_decision(
            f"Codex SDK unavailable: {exc}",
            prompt=prompt,
            capture_output=output,
            source="sdk_unavailable",
        )
    model = codex_sdk_model_name()
    if not model:
        return pending_followup_decision(
            "Codex SDK model is not configured",
            prompt=prompt,
            capture_output=output,
            source="sdk_unavailable",
        )
    sdk_prompt = json.dumps(
        {
            "task": "Decide whether a tmux-inject wake prompt was submitted to Codex or remains staged in the composer.",
            "allowed_actions": ["confirmed", "submit", "defer", "refuse"],
            "manager_id": record.get("manager_id"),
            "event_id": candidate.get("event_id"),
            "bound_pane_id": record.get("codex_pane_id"),
            "pane_validation": validation,
            "injection": injection,
            "pane_capture_tail": output[-6000:],
            "rules": [
                "Return only JSON with action, submit_key, confidence, and reason.",
                "Choose submit only if the wake prompt appears to remain in the Codex composer/input area.",
                "Use submit_key C-m unless capture clearly indicates another bounded submit key is needed.",
                "Choose confirmed if Codex appears to be working on the wake prompt or the prompt is no longer staged.",
                "Do not choose a different tmux pane and do not describe shell commands.",
            ],
        },
        sort_keys=True,
    )
    schema = {
        "type": "json_schema",
        "name": "tmux_inject_followup_decision",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "submit_key", "confidence", "reason"],
            "properties": {
                "action": {"type": "string", "enum": ["confirmed", "submit", "defer", "refuse"]},
                "submit_key": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
        },
        "strict": True,
    }
    try:
        client = OpenAI(timeout=timeout_seconds)
        response = client.responses.create(
            model=model,
            input=sdk_prompt,
            reasoning={"effort": CODEX_SDK_REASONING_EFFORT},
            text={"format": schema},
        )
        payload = json.loads(getattr(response, "output_text", ""))
    except Exception as exc:
        return pending_followup_decision(
            f"Codex SDK follow-up failed: {exc}",
            prompt=prompt,
            capture_output=output,
            source="sdk_error",
        )
    return normalize_tmux_inject_followup_decision(payload, prompt=prompt, capture_output=output) | {"source": "sdk"}


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


def inject_tmux_wake_prompt(pane_id: str, prompt: str) -> dict[str, Any]:
    target = tmux_state.one_line_text(pane_id)
    if not target:
        return {"injected": False, "pasted": False, "entered": False, "reason": "pane id is blank"}
    buffer_name = "tmux-skills-" + hashlib.sha256(f"{target}\n{prompt}".encode("utf-8")).hexdigest()[:16]
    load = subprocess.run(
        [*tmux_command_prefix(), "load-buffer", "-b", buffer_name, "-"],
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if load.returncode != 0:
        return {
            "injected": False,
            "pasted": False,
            "entered": False,
            "reason": load.stderr.strip() or "tmux load-buffer failed",
            "load_returncode": load.returncode,
        }
    paste = subprocess.run(
        [*tmux_command_prefix(), "paste-buffer", "-b", buffer_name, "-t", target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    pasted = paste.returncode == 0
    submit = send_tmux_submit_key(target, TMUX_INJECT_PRIMARY_SUBMIT_KEY) if pasted else None
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
    pane_id = tmux_state.one_line_text(record.get("codex_pane_id"))
    time.sleep(0.5)
    before = capture_tmux_pane_text(pane_id)
    decision = codex_sdk_inject_followup_decision(record, candidate, validation, injection, before, prompt)
    followup: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    if decision.get("action") == "submit":
        followup = send_tmux_submit_key(pane_id, str(decision.get("submit_key") or TMUX_INJECT_FOLLOWUP_SUBMIT_KEY))
        time.sleep(0.5)
        after = capture_tmux_pane_text(pane_id)
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
    }


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
    now = tmux_state.utc_now()
    existing = notification_for_event(record, event_id) or {}
    if event_id in submitted and not (
        notify.get("mode") == "tmux-inject"
        and not existing.get("acknowledged_by_codex")
        and tmux_inject_ack_recheck_due(existing)
    ):
        return record
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
    }
    if notify.get("mode") == "none":
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
        validation = pane_codex_validation(bound_pane_id)
        if (
            existing.get("mode") == "tmux-inject"
            and existing.get("status") in {"inject_pending", "injected"}
            and existing.get("submitted_to_tmux")
            and bound_pane_id
            and validation.get("safe")
        ):
            injection = dict(existing.get("injection") or {"pasted": True, "injected": False})
            delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
            decision = delivery_check.get("decision") if isinstance(delivery_check.get("decision"), dict) else {}
            if delivery_check.get("prompt_still_staged"):
                status = "inject_pending"
                reason = "tmux-inject wake prompt is still staged in the Codex composer after submit attempts"
            elif decision.get("action") == "defer":
                status = "inject_pending"
                reason = str(decision.get("reason") or "tmux-inject delivery awaits Codex ack")
            elif decision.get("action") == "refuse":
                status = "inject_refused"
                reason = str(decision.get("reason") or "Codex SDK follow-up refused tmux-inject delivery")
            else:
                status = "injected"
                reason = None
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
                "delivery_check": delivery_check,
            }
            if existing.get("submitted_at"):
                fields["submitted_at"] = existing.get("submitted_at")
            if existing.get("injected_at"):
                fields["injected_at"] = existing.get("injected_at")
            if reason:
                fields["reason"] = reason
            record = upsert_notification(record, event_id, fields)
            if status == "injected":
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
            }
            record["events"] = events
            return record
        sdk_decision = codex_sdk_inject_decision(record, candidate, validation)
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
            refused_reason = str(sdk_decision.get("reason") or "Codex SDK planner refused injection")
        elif (
            sdk_decision.get("decision") == "inject"
            and tmux_state.one_line_text(sdk_decision.get("target_pane"))
            and tmux_state.one_line_text(sdk_decision.get("target_pane")) != bound_pane_id
        ):
            status = "inject_refused"
            refused_reason = "Codex SDK planner selected a pane different from the bound Codex pane"
        elif sdk_decision.get("decision") == "inject":
            injection = inject_tmux_wake_prompt(bound_pane_id, prompt)
            if injection.get("pasted"):
                delivery_check = verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)
                delivery_decision = delivery_check.get("decision") if isinstance(delivery_check.get("decision"), dict) else {}
                if delivery_check.get("prompt_still_staged"):
                    status = "inject_pending"
                    refused_reason = "tmux-inject wake prompt is still staged in the Codex composer after submit attempts"
                elif delivery_decision.get("action") == "defer":
                    status = "inject_pending"
                    refused_reason = str(delivery_decision.get("reason") or "tmux-inject delivery awaits Codex ack")
                elif delivery_decision.get("action") == "refuse":
                    status = "inject_refused"
                    refused_reason = str(delivery_decision.get("reason") or "Codex SDK follow-up refused tmux-inject delivery")
                elif injection.get("injected"):
                    status = "injected"
                else:
                    status = "inject_pending"
            else:
                status = "inject_pending"
                refused_reason = str(injection.get("reason") or "tmux injection did not paste prompt")
        else:
            status = "inject_pending"
            refused_reason = str(sdk_decision.get("reason") or "Codex SDK planner deferred injection")
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
        if injection and injection.get("pasted"):
            fields["submitted_at"] = now
            fields["injected_at"] = now
        record = upsert_notification(record, event_id, fields)
        if injection and injection.get("pasted") and status == "injected":
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
            "status_path": status.get("status_path"),
            "task_path": task_path_for_job(paths, target_job_id),
            "log_path": status.get("log_path"),
        }
    else:
        return record
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
    job_status = load_job_status(paths, str(record.get("current_job_id") or ""))
    active_statuses = {
        job_id: load_job_status(paths, job_id)
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


def dashboard_path_label(value: Any) -> str:
    text = tmux_state.one_line_text(value)
    return Path(str(text)).name if text else "-"


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
