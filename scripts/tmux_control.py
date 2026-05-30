#!/usr/bin/env python3
"""Small tmux helper for Codex skills."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import shlex
import socket
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import tmux_state


FIELD_SEP = "\t"
PANE_FIELDS = [
    "#{session_name}",
    "#{window_index}",
    "#{window_id}",
    "#{window_name}",
    "#{pane_id}",
    "#{pane_index}",
    "#{pane_active}",
    "#{pane_current_command}",
    "#{pane_current_path}",
    "#{pane_title}",
    "#{pane_pid}",
    "#{pane_dead}",
    "#{pane_width}",
    "#{pane_height}",
    "#{pane_tty}",
]
CURRENT_FIELDS = [
    "#{session_name}",
    "#{session_id}",
    "#{window_index}",
    "#{window_id}",
    "#{window_name}",
    "#{pane_id}",
    "#{pane_index}",
    "#{pane_current_command}",
    "#{pane_current_path}",
    "#{pane_title}",
    "#{pane_pid}",
    "#{pane_dead}",
    "#{pane_width}",
    "#{pane_height}",
    "#{pane_tty}",
]
PANE_FORMAT = FIELD_SEP.join(PANE_FIELDS)
CURRENT_FORMAT = FIELD_SEP.join(CURRENT_FIELDS)
SHELL_COMMANDS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "mksh"}
ANSI_RE = re.compile(
    r"(?:\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"  # OSC: titles, hyperlinks, etc.
    r"|(?:\x1B[P^_].*?\x1B\\)"  # DCS/PM/APC string controls.
    r"|(?:\x1B\[[0-?]*[ -/]*[@-~])"  # CSI controls, including colors/cursor movement.
    r"|(?:\x1B[@-Z\\-_])",  # 7-bit C1/Fe controls.
    re.DOTALL,
)
PROMPT_RE = re.compile("(?:[$#%]|\\u276f|\\u276e|\\u279c|\\u03bb)\\s*$")


def default_tmux_tmpdir() -> Path:
    base = Path(os.environ.get("TMPDIR") or "/tmp").expanduser()
    return base / "codex-tmux-control"


def tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    if not inside_tmux() and not env.get("TMUX_TMPDIR"):
        tmpdir = default_tmux_tmpdir()
        tmpdir.mkdir(parents=True, exist_ok=True)
        env["TMUX_TMPDIR"] = str(tmpdir)
    return env


def tmux_tmpdir_value() -> str | None:
    if inside_tmux():
        return None
    return os.environ.get("TMUX_TMPDIR") or str(default_tmux_tmpdir())


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def run_tmux(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["tmux", *args],
            check=False,
            env=tmux_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        die("tmux is not installed or is not on PATH")

    stderr = result.stderr.strip()
    if check and (result.returncode != 0 or stderr.startswith("error ")):
        detail = f": {stderr}" if stderr else ""
        code = result.returncode if result.returncode != 0 else 1
        die(f"tmux {' '.join(args)} failed{detail}", code)
    return result


def inside_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def workspace_session_name(cwd: Path) -> str:
    base = cwd.resolve().name or "workspace"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-._")
    return f"codex-{safe or 'workspace'}"


def session_exists(name: str) -> bool:
    result = run_tmux(["has-session", "-t", name], check=False)
    return result.returncode == 0


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def process_index() -> tuple[dict[int, list[tuple[int, str]]], dict[int, tuple[int, str]]]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    children: dict[int, list[tuple[int, str]]] = defaultdict(list)
    processes: dict[int, tuple[int, str]] = {}
    if result.returncode != 0:
        return children, processes

    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, ppid_s, command = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        processes[pid] = (ppid, command)
        children[ppid].append((pid, command))
    return children, processes


def descendant_processes(root_pid: int | None, *, limit: int = 12) -> tuple[int, list[dict[str, Any]], int]:
    if root_pid is None:
        return 0, [], 0

    children, processes = process_index()
    immediate_count = len(children.get(root_pid, []))
    descendants: list[dict[str, Any]] = []
    total = 0
    queue: deque[tuple[int, int]] = deque((pid, 1) for pid, _ in children.get(root_pid, []))
    while queue:
        pid, depth = queue.popleft()
        ppid, command = processes.get(pid, (None, ""))
        total += 1
        if len(descendants) < limit:
            descendants.append({"pid": pid, "ppid": ppid, "command": command, "depth": depth})
        for child_pid, _child_command in children.get(pid, []):
            queue.append((child_pid, depth + 1))
    return immediate_count, descendants, total


def enrich_pane_processes(pane: dict[str, Any]) -> dict[str, Any]:
    pane_pid = parse_int(str(pane.get("pane_pid") or ""))
    child_count, summary, descendant_count = descendant_processes(pane_pid)
    pane["child_process_count"] = child_count
    pane["descendant_process_count"] = descendant_count
    pane["descendant_summary"] = summary
    return pane


def parse_current_line(line: str) -> dict[str, Any] | None:
    parts = line.split(FIELD_SEP)
    if len(parts) != len(CURRENT_FIELDS):
        return None
    (
        session_name,
        session_id,
        window_index,
        window_id,
        window_name,
        pane_id,
        pane_index,
        pane_current_command,
        pane_current_path,
        pane_title,
        pane_pid,
        pane_dead,
        pane_width,
        pane_height,
        pane_tty,
    ) = parts
    return enrich_pane_processes(
        {
            "session_name": session_name,
            "session_id": session_id,
            "window_index": window_index,
            "window_id": window_id,
            "window_name": window_name,
            "pane_id": pane_id,
            "pane_index": pane_index,
            "current_command": pane_current_command,
            "current_path": pane_current_path,
            "title": pane_title,
            "pane_pid": pane_pid,
            "pane_dead": pane_dead == "1",
            "pane_width": parse_int(pane_width),
            "pane_height": parse_int(pane_height),
            "pane_tty": pane_tty,
        }
    )


def parse_pane_line(line: str, *, current_pane_id: str | None = None) -> dict[str, Any] | None:
    parts = line.split(FIELD_SEP)
    if len(parts) != len(PANE_FIELDS):
        return None
    (
        session_name,
        window_index,
        window_id,
        window_name,
        pane_id,
        pane_index,
        pane_active,
        pane_current_command,
        pane_current_path,
        pane_title,
        pane_pid,
        pane_dead,
        pane_width,
        pane_height,
        pane_tty,
    ) = parts
    return enrich_pane_processes(
        {
            "session_name": session_name,
            "window_index": window_index,
            "window_id": window_id,
            "window_name": window_name,
            "pane_id": pane_id,
            "pane_index": pane_index,
            "active": pane_active == "1",
            "current": pane_id == current_pane_id,
            "current_command": pane_current_command,
            "current_path": pane_current_path,
            "title": pane_title,
            "pane_pid": pane_pid,
            "pane_dead": pane_dead == "1",
            "pane_width": parse_int(pane_width),
            "pane_height": parse_int(pane_height),
            "pane_tty": pane_tty,
        }
    )


def current_info(target: str | None = None) -> dict[str, Any] | None:
    if target is None and not inside_tmux():
        return None

    tmux_args = ["display-message", "-p"]
    if target:
        tmux_args.extend(["-t", target])
    tmux_args.append(CURRENT_FORMAT)
    result = run_tmux(tmux_args, check=False)
    if result.returncode != 0 or result.stderr.strip().startswith("error "):
        return None
    return parse_current_line(result.stdout.strip())


def panes_for_target(target: str | None = None, *, current_pane_id: str | None = None) -> list[dict[str, Any]]:
    tmux_args = ["list-panes"]
    if target:
        tmux_args.extend(["-t", target])
    else:
        tmux_args.append("-a")
    tmux_args.extend(["-F", PANE_FORMAT])
    result = run_tmux(tmux_args, check=False)
    if result.returncode != 0:
        return []
    panes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        pane = parse_pane_line(line, current_pane_id=current_pane_id)
        if pane:
            panes.append(pane)
    return panes


def list_panes() -> dict[str, Any]:
    current = current_info()
    panes = panes_for_target(current_pane_id=current["pane_id"] if current else None)
    sessions: dict[str, dict[str, Any]] = {}
    for pane in panes:
        session_name = pane["session_name"]
        sessions.setdefault(session_name, {"session_name": session_name, "windows": set()})
        sessions[session_name]["windows"].add(f"{pane['window_index']}:{pane['window_name']}")

    session_values = []
    for session in sessions.values():
        session_values.append(
            {
                "session_name": session["session_name"],
                "windows": sorted(session["windows"]),
            }
        )
    return {
        "inside_tmux": inside_tmux(),
        "tmux_tmpdir": tmux_tmpdir_value(),
        "current": current,
        "sessions": session_values,
        "panes": panes,
    }


def current_window_target() -> str:
    result = run_tmux(["display-message", "-p", "#{session_name}:#{window_index}"])
    return result.stdout.strip()


def ensure_managed_target(cwd: Path) -> tuple[str, str]:
    session = workspace_session_name(cwd)
    if not session_exists(session):
        result = run_tmux(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{session_name}:#{window_index}",
                "-s",
                session,
                "-n",
                "work",
                "-c",
                str(cwd),
            ]
        )
        target = result.stdout.strip()
        return session, target

    result = run_tmux(["list-windows", "-t", session, "-F", "#{window_index}"])
    window_index = result.stdout.splitlines()[0].strip()
    return session, f"{session}:{window_index}"


def ensure_managed_session(workspace: Path, cwd: Path) -> tuple[str, str | None]:
    session = workspace_session_name(workspace)
    if session_exists(session):
        return session, None
    result = run_tmux(
        [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-s",
            session,
            "-n",
            "work",
            "-c",
            str(cwd),
        ]
    )
    return session, result.stdout.strip()


def spawn(args: argparse.Namespace) -> dict[str, Any]:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd().resolve()
    split_flag = "-h" if args.horizontal else "-v"
    size_args = ["-p", str(args.percent)] if args.percent is not None else []

    attach_command = None
    if args.target:
        target = args.target
    elif inside_tmux():
        target = current_window_target()
    else:
        session, target = ensure_managed_target(cwd)
        tmpdir = tmux_tmpdir_value()
        attach_command = f"TMUX_TMPDIR={tmpdir} tmux attach -t {session}" if tmpdir else f"tmux attach -t {session}"

    result = run_tmux(
        [
            "split-window",
            split_flag,
            *size_args,
            "-P",
            "-F",
            "#{session_name}\t#{window_id}\t#{pane_id}",
            "-c",
            str(cwd),
            "-t",
            target,
        ]
    )
    session_name, window_id, pane_id = result.stdout.strip().split(FIELD_SEP)
    return {
        "session_name": session_name,
        "window_id": window_id,
        "pane_id": pane_id,
        "target": target,
        "cwd": str(cwd),
        "attach_command": attach_command,
        "tmux_tmpdir": tmux_tmpdir_value(),
    }


def new_window(args: argparse.Namespace) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
    cwd = Path(args.cwd).expanduser().resolve()
    name_args = ["-n", args.name] if args.name else []
    attach_command = None

    if args.target:
        target = args.target
    elif inside_tmux():
        target = run_tmux(["display-message", "-p", "#{session_name}"]).stdout.strip()
    else:
        session, existing_window_id = ensure_managed_session(workspace, cwd)
        tmpdir = tmux_tmpdir_value()
        attach_command = f"TMUX_TMPDIR={tmpdir} tmux attach -t {session}" if tmpdir else f"tmux attach -t {session}"
        target = session
        if existing_window_id:
            info = current_info(existing_window_id)
            if not info:
                die(f"could not inspect created window: {existing_window_id}")
            return {
                "session_name": info["session_name"],
                "window_id": info["window_id"],
                "window_index": info["window_index"],
                "pane_id": info["pane_id"],
                "target": target,
                "cwd": str(cwd),
                "workspace": str(workspace),
                "attach_command": attach_command,
                "tmux_tmpdir": tmux_tmpdir_value(),
            }

    result = run_tmux(
        [
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{session_name}\t#{window_id}\t#{window_index}\t#{pane_id}",
            *name_args,
            "-c",
            str(cwd),
            "-t",
            target,
        ]
    )
    session_name, window_id, window_index, pane_id = result.stdout.strip().split(FIELD_SEP)
    return {
        "session_name": session_name,
        "window_id": window_id,
        "window_index": window_index,
        "pane_id": pane_id,
        "target": target,
        "cwd": str(cwd),
        "workspace": str(workspace),
        "attach_command": attach_command,
        "tmux_tmpdir": tmux_tmpdir_value(),
    }


def strip_ansi(text: str) -> str:
    # capture-pane can include terminal control bytes in old scrollback; remove those and
    # normalize carriage returns so progress bars are readable in JSON output.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return ANSI_RE.sub("", normalized)


def capture_text(pane: str, lines: int, *, strip: bool = False) -> str:
    result = run_tmux(["capture-pane", "-p", "-t", pane, "-S", f"-{lines}"])
    output = result.stdout.rstrip("\n")
    return strip_ansi(output) if strip else output


def prompt_like(output: str) -> bool:
    for line in reversed(strip_ansi(output).splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        return bool(PROMPT_RE.search(stripped))
    return False


def idle_shell_check(pane: str) -> dict[str, Any]:
    info = current_info(pane)
    if not info:
        return {"ok": False, "reason": "pane could not be resolved", "pane_id": pane}

    command = str(info.get("current_command") or "")
    pane_pid = parse_int(str(info.get("pane_pid") or ""))
    immediate_count, summary, descendant_count = descendant_processes(pane_pid)
    prompt_output = capture_text(pane, 20, strip=True)
    has_prompt = prompt_like(prompt_output)
    diagnostics = {
        "pane_id": info.get("pane_id", pane),
        "current_command": command,
        "pane_pid": info.get("pane_pid"),
        "child_process_count": immediate_count,
        "descendant_process_count": descendant_count,
        "descendant_summary": summary,
        "prompt_detected": has_prompt,
    }

    if command not in SHELL_COMMANDS:
        return {"ok": False, "reason": f"pane foreground command is not an idle shell: {command}", **diagnostics}
    if descendant_count:
        return {"ok": False, "reason": "pane has child processes and appears busy", **diagnostics}
    if not has_prompt:
        return {"ok": False, "reason": "recent pane output does not look like a shell prompt", **diagnostics}
    return {"ok": True, "reason": "pane appears to be an idle shell", **diagnostics}


def script_preflight(command_text: str, cwd: str | None = None) -> dict[str, Any]:
    try:
        parts = shlex.split(command_text, posix=True)
    except ValueError as exc:
        return {"ok": True, "warnings": [f"preflight skipped: could not parse command with shlex: {exc}"]}

    if not parts:
        return {"ok": True, "warnings": []}

    first = parts[0]
    if first in {"bash", "sh", "zsh", "fish", "dash", "ksh"}:
        return {"ok": True, "warnings": []}
    if not (first.startswith("./") or first.startswith("../") or first.endswith(".sh")):
        return {"ok": True, "warnings": []}

    base = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    script_path = Path(first).expanduser()
    resolved = script_path if script_path.is_absolute() else (base / script_path)
    if not resolved.exists() or resolved.is_dir() or os.access(resolved, os.X_OK):
        return {"ok": True, "warnings": [], "script_path": str(resolved)}

    warning = f"preflight_warning: {first} is not executable; use 'bash {first}' or chmod +x."
    return {
        "ok": False,
        "warnings": [warning],
        "script_path": str(resolved),
        "bash_command": " ".join(["bash", shlex.quote(first), *(shlex.quote(part) for part in parts[1:])]),
    }


def send(args: argparse.Namespace) -> dict[str, Any]:
    guard: dict[str, Any] | None = None
    preflight = script_preflight(args.command_text, getattr(args, "cwd", None))
    command_text = args.command_text
    if not preflight.get("ok"):
        if getattr(args, "bash_if_not_executable", False) and preflight.get("bash_command"):
            command_text = str(preflight["bash_command"])
            preflight["action"] = "rewrote-to-bash"
        elif getattr(args, "strict_preflight", False):
            return {
                "pane_id": args.pane,
                "sent": False,
                "sent_to_pane": False,
                "entered": False,
                "command_text": command_text,
                "reason": preflight["warnings"][0],
                "preflight": preflight,
            }
        else:
            preflight["action"] = "warn-only"

    if args.require_idle_shell:
        guard = idle_shell_check(args.pane)
        if not guard.get("ok"):
            return {
                "pane_id": args.pane,
                "sent": False,
                "sent_to_pane": False,
                "entered": False,
                "command_text": command_text,
                "reason": guard.get("reason"),
                "idle_shell_check": guard,
                "preflight": preflight,
            }

    run_tmux(["send-keys", "-t", args.pane, "-l", command_text])
    if args.enter:
        run_tmux(["send-keys", "-t", args.pane, "Enter"])
    return {
        "pane_id": args.pane,
        "sent": True,
        "sent_to_pane": True,
        "command_text": command_text,
        "entered": bool(args.enter),
        "idle_shell_check": guard,
        "preflight": preflight,
    }


def truncate_text(text: str, max_chars: int | None) -> tuple[str, bool, int]:
    if max_chars is None or len(text) <= max_chars:
        return text, False, 0
    return text[-max_chars:], True, len(text) - max_chars


def capture(args: argparse.Namespace) -> dict[str, Any]:
    output = capture_text(args.pane, args.lines, strip=args.strip_ansi)
    output, truncated, omitted_chars = truncate_text(output, args.max_chars)
    return {
        "pane_id": args.pane,
        "lines": args.lines,
        "strip_ansi": bool(args.strip_ansi),
        "max_chars": args.max_chars,
        "truncated": truncated,
        "omitted_chars": omitted_chars,
        "output": output,
    }


def next_attempt(paths: dict[str, Path], item_id: str) -> int:
    existing, _error = tmux_state.read_json(tmux_state.status_path(paths, item_id))
    if existing:
        try:
            return int(existing.get("attempt") or 0) + 1
        except (TypeError, ValueError):
            return 1
    return 1


def write_command_file(path: Path, command_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(command_text)
        if not command_text.endswith("\n"):
            handle.write("\n")


def shell_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def run_job(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths["workspace"]
    item_id = tmux_state.safe_id(args.job_id or f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    attempt = next_attempt(paths, item_id)

    if args.command_file:
        command_text = Path(args.command_file).expanduser().read_text(encoding="utf-8")
    else:
        command_text = args.command_text

    cmd_file = tmux_state.command_path(paths, item_id)
    status_file = tmux_state.status_path(paths, item_id)
    log_file = tmux_state.log_path(paths, item_id)
    write_command_file(cmd_file, command_text)

    pending = tmux_state.build_status(
        kind="job",
        item_id=item_id,
        attempt=attempt,
        name=args.name,
        status="pending",
        pane_id=args.pane,
        command_preview_text=tmux_state.command_preview(command_text),
        cwd=str(cwd),
        status_file=status_file,
        log_file=log_file,
    )
    tmux_state.write_status(status_file, pending)

    script_dir = Path(__file__).resolve().parent
    argv = [
        sys.executable,
        str(script_dir / "tmux_job.py"),
        "exec",
        "--job-id",
        item_id,
        "--attempt",
        str(attempt),
        "--pane",
        args.pane,
        "--command-file",
        str(cmd_file),
        "--cwd",
        str(cwd),
        "--workspace",
        str(paths["workspace"]),
        "--state-dir",
        str(paths["root"]),
    ]
    if args.name:
        argv.extend(["--name", args.name])

    next_instruction = read_text_arg(getattr(args, "next_instruction", None), getattr(args, "next_instruction_file", None))
    next_task: dict[str, Any] | None = None
    if next_instruction:
        next_task = tmux_state.build_task(
            task_id=None,
            instruction=next_instruction,
            summary=f"Follow up after job {item_id}",
            intent=args.name,
            after_job_id=item_id,
            after_event_id=None,
            trigger_on=args.next_on,
            evidence_paths=[str(status_file), str(log_file)],
        )
        next_task = tmux_state.write_task(paths, next_task)

    send_args = argparse.Namespace(
        pane=args.pane,
        command_text=shell_command(argv),
        enter=True,
        no_enter=False,
        require_idle_shell=args.require_idle_shell,
    )
    send_result = send(send_args)
    return {
        "job_id": item_id,
        "attempt": attempt,
        "pane_id": args.pane,
        "sent": send_result.get("sent_to_pane", False),
        "reason": send_result.get("reason"),
        "command_path": str(cmd_file),
        "status_path": str(status_file),
        "log_path": str(log_file),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "next_task": next_task,
    }


def monitor(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    monitor_id = tmux_state.safe_id(f"monitor-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    script_dir = Path(__file__).resolve().parent
    argv = [
        sys.executable,
        str(script_dir / "tmux_monitor.py"),
        "--monitor-id",
        monitor_id,
        "--pane",
        args.pane,
        "--poll-seconds",
        str(args.poll_seconds),
        "--lines",
        str(args.lines),
        "--workspace",
        str(paths["workspace"]),
        "--state-dir",
        str(paths["root"]),
    ]
    if args.match_regex:
        argv.extend(["--match-regex", args.match_regex])
    if args.idle_shell:
        argv.append("--idle-shell")
    if args.timeout_seconds is not None:
        argv.extend(["--timeout-seconds", str(args.timeout_seconds)])

    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {
        "monitor_id": monitor_id,
        "pid": proc.pid,
        "pane_id": args.pane,
        "status_path": str(tmux_state.status_path(paths, monitor_id)),
        "log_path": str(tmux_state.log_path(paths, monitor_id)),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }


LOCK_TIMEOUT_SECONDS = 5.0
LOCK_STALE_SECONDS = 30.0


def owner_identity(args: argparse.Namespace | None = None) -> str:
    explicit = getattr(args, "owner", None) if args else None
    if explicit:
        return str(explicit)
    return f"{socket.gethostname()}:{os.getpid()}"


def normalize_command_for_dedupe(command_text: str | None) -> str | None:
    if command_text is None:
        return None
    return command_text.replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_pane_for_dedupe(pane: str | None) -> str | None:
    return str(pane).strip() if pane is not None else None


def resolve_status_arg(paths: dict[str, Path], status_file: str | None) -> str | None:
    if not status_file:
        return None
    path = Path(status_file).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    return str((paths["workspace"] / path).resolve())


def canonical_row_specs(rows: list[str] | None) -> list[str]:
    return sorted(row.strip() for row in (rows or []) if row and row.strip())


def managed_dedupe_payload(
    paths: dict[str, Path],
    args: argparse.Namespace,
    *,
    kind: str,
    command_text: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "workspace": str(paths["workspace"]),
        "pane": normalize_pane_for_dedupe(getattr(args, "pane", None)),
    }
    if kind == "watch":
        payload["observed_status_file"] = resolve_status_arg(paths, getattr(args, "status_file", None))
        return payload

    payload.update(
        {
            "command": normalize_command_for_dedupe(command_text),
            "require_idle_shell": bool(getattr(args, "require_idle_shell", True)),
        }
    )
    if kind == "queue-after-status":
        payload.update(
            {
                "status_file": resolve_status_arg(paths, getattr(args, "status_file", None)),
                "require_rows": canonical_row_specs(getattr(args, "require_row", None)),
                "fail_rows": canonical_row_specs(getattr(args, "fail_row", None)),
            }
        )
    return payload


def managed_dedupe_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lock_metadata() -> dict[str, Any]:
    return {"host": socket.gethostname(), "pid": os.getpid(), "created_at": tmux_state.utc_now()}


def lock_dir_is_stale(lock_dir: Path, stale_seconds: float) -> bool:
    data, error = tmux_state.read_json(lock_dir / "owner.json")
    if data and not error:
        age = tmux_state.age_seconds(data.get("created_at"))
    else:
        try:
            age = time.time() - lock_dir.stat().st_mtime
        except OSError:
            return True
    return age is None or age >= stale_seconds


@contextlib.contextmanager
def registry_lock(paths: dict[str, Path], *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS, stale_seconds: float = LOCK_STALE_SECONDS) -> Any:
    lock_dir = tmux_state.job_registry_lock_path(paths)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    while True:
        try:
            lock_dir.mkdir(parents=True)
            tmux_state.atomic_write_json(lock_dir / "owner.json", lock_metadata())
            acquired = True
            break
        except FileExistsError:
            if lock_dir_is_stale(lock_dir, stale_seconds):
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for managed job registry lock: {lock_dir}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)


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


def managed_worker_pid_matches(record: dict[str, Any]) -> bool:
    pid = parse_int(str(record.get("pid") or ""))
    command_line = process_command_line(pid)
    return "tmux_queue.py" in command_line and str(record.get("job_id") or "") in command_line


def annotate_job_record(record: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(record)
    pid = parse_int(str(annotated.get("pid") or ""))
    pid_running = pid_is_running(pid)
    pid_matches = managed_worker_pid_matches(annotated) if pid_running else False
    stale_reason = tmux_state.managed_job_stale_reason(annotated, pid_running=pid_running, pid_matches=pid_matches)
    annotated["pid_running"] = pid_running
    annotated["pid_matches"] = pid_matches
    annotated["stale"] = bool(stale_reason)
    if stale_reason:
        annotated["stale_reason"] = stale_reason
    return annotated


def load_job_records(paths: dict[str, Path], kind: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths["jobs"].glob("*.json")):
        data, error = tmux_state.read_json(path)
        if error or not data:
            records.append({"job_path": str(path), "error": error or "empty job record"})
            continue
        record_kind = str(data.get("kind") or "")
        if kind and record_kind != kind and not record_kind.startswith(f"{kind}-"):
            continue
        data["job_path"] = str(path)
        records.append(annotate_job_record(data))
    records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return records


def active_duplicate_record(paths: dict[str, Path], *, dedupe_key: str, item_id: str | None = None) -> dict[str, Any] | None:
    for record in load_job_records(paths):
        if record.get("dedupe_key") != dedupe_key:
            continue
        if item_id and record.get("job_id") == item_id:
            continue
        if tmux_state.is_active_managed_job(record) and not record.get("stale"):
            return record
    return None


def duplicate_result(item_id: str, *, dedupe_key: str, existing: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "job_id": item_id,
        "started": False,
        "duplicate": True,
        "dedupe_key": dedupe_key,
        "existing_job_id": existing.get("job_id"),
        "existing": existing,
        "reason": reason,
    }


def write_managed_job_record(
    paths: dict[str, Path],
    *,
    job_id: str,
    kind: str,
    pid: int,
    pane_id: str | None,
    status: str,
    command_path_value: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = tmux_state.utc_now()
    path = tmux_state.job_path(paths, job_id)
    previous, _error = tmux_state.read_json(path)
    created_at = previous.get("created_at") if previous else now
    record: dict[str, Any] = {
        "version": 1,
        "job_id": job_id,
        "kind": kind,
        "status": status,
        "pid": pid,
        "pane_id": pane_id,
        "command_path": command_path_value,
        "status_path": str(tmux_state.status_path(paths, job_id)),
        "log_path": str(tmux_state.log_path(paths, job_id)),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "created_at": created_at,
        "updated_at": now,
        "heartbeat_at": now,
    }
    if extra:
        record.update(extra)
    tmux_state.atomic_write_json(path, record)
    return record


def command_text_for_worker(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if getattr(args, "command_text", None) is not None:
        return str(args.command_text), None
    if getattr(args, "command_file", None):
        command_file = Path(args.command_file).expanduser().resolve()
        return command_file.read_text(encoding="utf-8"), str(command_file)
    return None, None


def check_interval_seconds(args: argparse.Namespace, worker_action: str) -> float:
    if worker_action == "watch":
        return float(getattr(args, "interval", 180.0))
    return float(getattr(args, "poll_seconds", 2.0))


def start_managed_worker(args: argparse.Namespace, worker_action: str, kind: str) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = tmux_state.safe_id(args.job_id)
    record_path = tmux_state.job_path(paths, item_id)
    command_text, source_command_path = command_text_for_worker(args)
    payload = managed_dedupe_payload(paths, args, kind=kind, command_text=command_text)
    dedupe_key = managed_dedupe_key(payload)
    owner = owner_identity(args)
    interval = check_interval_seconds(args, worker_action)
    command_path_value: str | None = source_command_path

    script_dir = Path(__file__).resolve().parent
    try:
        with registry_lock(paths):
            existing, _error = tmux_state.read_json(record_path)
            existing = annotate_job_record(existing) if existing else None
            if existing and tmux_state.is_active_managed_job(existing) and not existing.get("stale"):
                if not getattr(args, "replace", False):
                    return duplicate_result(
                        item_id,
                        dedupe_key=dedupe_key,
                        existing=existing,
                        reason="managed job already appears active; use --replace or cancel it first",
                    )
                existing_pid = parse_int(str(existing.get("pid") or ""))
                if existing_pid and pid_is_running(existing_pid):
                    if not managed_worker_pid_matches(existing):
                        return {
                            "job_id": item_id,
                            "started": False,
                            "reason": "existing pid is running but no longer looks like this tmux-skills worker",
                            "existing": existing,
                        }
                    os.kill(existing_pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5.0
                    while pid_is_running(existing_pid) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if pid_is_running(existing_pid):
                        return {
                            "job_id": item_id,
                            "started": False,
                            "reason": "existing managed job did not stop after SIGTERM",
                            "existing": existing,
                        }

            duplicate = active_duplicate_record(paths, dedupe_key=dedupe_key, item_id=item_id)
            duplicate_allowed = bool(getattr(args, "allow_duplicate", False))
            duplicate_of = duplicate.get("job_id") if duplicate else None
            if duplicate and not duplicate_allowed:
                return duplicate_result(
                    item_id,
                    dedupe_key=dedupe_key,
                    existing=duplicate,
                    reason="active managed job with the same dedupe key already exists",
                )

            if command_text is not None:
                command_path = tmux_state.command_path(paths, item_id)
                write_command_file(command_path, command_text)
                command_path_value = str(command_path)
            elif command_path_value:
                command_path_value = str(Path(command_path_value).expanduser().resolve())

            argv = [
                sys.executable,
                str(script_dir / "tmux_queue.py"),
                worker_action,
                "--job-id",
                item_id,
                "--pane",
                args.pane,
                "--workspace",
                str(paths["workspace"]),
                "--state-dir",
                str(paths["root"]),
            ]
            if getattr(args, "name", None):
                argv.extend(["--name", args.name])
            if command_path_value:
                argv.extend(["--command-file", command_path_value])
            if worker_action == "watch":
                argv.extend(["--interval", str(args.interval), "--capture-lines", str(args.capture_lines)])
                if args.status_file:
                    argv.extend(["--status-file", args.status_file])
            else:
                argv.extend(["--poll-seconds", str(args.poll_seconds)])
                if args.strict_preflight:
                    argv.append("--strict-preflight")
                if args.bash_if_not_executable:
                    argv.append("--bash-if-not-executable")
                if worker_action == "queue-after-status":
                    argv.extend(["--status-file", args.status_file])
                    for row in args.require_row or []:
                        argv.extend(["--require-row", row])
                    for row in args.fail_row or []:
                        argv.extend(["--fail-row", row])
                    if not args.require_idle_shell:
                        argv.append("--no-require-idle-shell")
            if args.timeout_seconds is not None:
                argv.extend(["--timeout-seconds", str(args.timeout_seconds)])

            record_extra = {
                "argv": argv,
                "dedupe_key": dedupe_key,
                "dedupe_payload": payload,
                "owner": owner,
                "check_interval_seconds": interval,
            }
            if duplicate_allowed and duplicate_of:
                record_extra.update({"duplicate_allowed": True, "duplicate_of": duplicate_of})
            write_managed_job_record(
                paths,
                job_id=item_id,
                kind=kind,
                pid=0,
                pane_id=args.pane,
                status="starting",
                command_path_value=command_path_value,
                extra=record_extra,
            )
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status = "running" if kind == "watch" else "waiting"
            record = write_managed_job_record(
                paths,
                job_id=item_id,
                kind=kind,
                pid=proc.pid,
                pane_id=args.pane,
                status=status,
                command_path_value=command_path_value,
                extra=record_extra,
            )
    except TimeoutError as exc:
        return {"job_id": item_id, "started": False, "reason": str(exc)}

    return {
        "job_id": item_id,
        "kind": kind,
        "pid": proc.pid,
        "started": True,
        "duplicate": False,
        "dedupe_key": dedupe_key,
        "pane_id": args.pane,
        "job_path": str(record_path),
        "status_path": str(tmux_state.status_path(paths, item_id)),
        "log_path": str(tmux_state.log_path(paths, item_id)),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "record": record,
    }


def job_list(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    return {"jobs": load_job_records(paths, kind), "workspace": str(paths["workspace"]), "state_dir": str(paths["root"])}


def job_status(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = tmux_state.safe_id(args.job_id)
    record, record_error = tmux_state.read_json(tmux_state.job_path(paths, item_id))
    if record:
        record = annotate_job_record(record)
    status, status_error = tmux_state.read_json(tmux_state.status_path(paths, item_id))
    record_kind = str((record or {}).get("kind") or "")
    if kind and record and record_kind != kind and not record_kind.startswith(f"{kind}-"):
        return {"job_id": item_id, "found": False, "reason": f"job is not a {kind} job", "record": record}
    return {
        "job_id": item_id,
        "found": bool(record or status),
        "record": record,
        "record_error": record_error,
        "status": status,
        "status_error": status_error,
        "pid_running": pid_is_running(parse_int(str((record or {}).get("pid") or ""))),
    }


def job_cancel(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = tmux_state.safe_id(args.job_id)
    record_path = tmux_state.job_path(paths, item_id)
    record, error = tmux_state.read_json(record_path)
    if error or not record:
        return {"job_id": item_id, "cancelled": False, "reason": error or "job record not found"}
    record_kind = str(record.get("kind") or "")
    if kind and record_kind != kind and not record_kind.startswith(f"{kind}-"):
        return {"job_id": item_id, "cancelled": False, "reason": f"job is not a {kind} job", "record": record}

    pid = parse_int(str(record.get("pid") or ""))
    running = pid_is_running(pid)
    record_status = str(record.get("status") or "")
    if not running and record_status in tmux_state.TERMINAL_STATUSES:
        return {"job_id": item_id, "cancelled": False, "reason": f"job already {record_status}", "record": record}
    if running and pid:
        if not managed_worker_pid_matches(record):
            return {
                "job_id": item_id,
                "cancelled": False,
                "reason": "recorded pid is running but no longer looks like this tmux-skills worker",
                "record": record,
            }
        os.kill(pid, signal.SIGTERM)

    now = tmux_state.utc_now()
    record.update({"status": "cancelled", "updated_at": now, "heartbeat_at": now, "pid_running": pid_is_running(pid)})
    tmux_state.atomic_write_json(record_path, record)

    status_file = tmux_state.status_path(paths, item_id)
    status, _status_error = tmux_state.read_json(status_file)
    if status:
        status.update(
            {
                "status": "cancelled",
                "exit_code": 1,
                "updated_at": now,
                "ended_at": now,
                "last_output": "cancelled by tmux_control.py",
            }
        )
        status["event_id"] = tmux_state.terminal_event_id(status)
    else:
        status = tmux_state.build_status(
            kind=record_kind or "job",
            item_id=item_id,
            attempt=1,
            name=record.get("name"),
            status="cancelled",
            pane_id=record.get("pane_id"),
            command_preview_text=str(record.get("command_path") or ""),
            cwd=str(paths["workspace"]),
            status_file=status_file,
            log_file=tmux_state.log_path(paths, item_id),
            exit_code=1,
            last_output="cancelled by tmux_control.py",
        )
    tmux_state.write_status(status_file, status)
    return {"job_id": item_id, "cancelled": True, "signal_sent": running, "record": record, "status": status}


def job_gc(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    if not args.stale:
        die("job gc requires --stale")

    stale_records = [record for record in load_job_records(paths) if record.get("stale")]
    result: dict[str, Any] = {
        "dry_run": bool(args.dry_run),
        "stale_jobs": stale_records,
        "marked": [],
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }
    if args.dry_run:
        return result

    now = tmux_state.utc_now()
    for record in stale_records:
        job_id = str(record.get("job_id") or record.get("id") or "")
        if not job_id:
            continue
        stale_reason = str(record.get("stale_reason") or "stale managed job")
        record_path = tmux_state.job_path(paths, job_id)
        stored, error = tmux_state.read_json(record_path)
        if error or not stored:
            continue
        stored.update({"status": "stale", "updated_at": now, "heartbeat_at": now, "stale_reason": stale_reason})
        tmux_state.atomic_write_json(record_path, stored)

        status_path = tmux_state.status_path(paths, job_id)
        status, _status_error = tmux_state.read_json(status_path)
        if status:
            status.update({"status": "stale", "exit_code": 1, "last_output": stale_reason, "stale_reason": stale_reason})
        else:
            status = tmux_state.build_status(
                kind=str(stored.get("kind") or "job"),
                item_id=job_id,
                attempt=1,
                name=stored.get("name"),
                status="stale",
                pane_id=stored.get("pane_id"),
                command_preview_text=str(stored.get("command_path") or ""),
                cwd=str(paths["workspace"]),
                status_file=status_path,
                log_file=tmux_state.log_path(paths, job_id),
                exit_code=1,
                last_output=stale_reason,
            )
        tmux_state.write_status(status_path, status)
        result["marked"].append({"job_id": job_id, "stale_reason": stale_reason})
    return result


def watch(args: argparse.Namespace) -> dict[str, Any]:
    if args.watch_action == "list":
        return job_list(args, "watch")
    if args.watch_action == "status":
        return job_status(args, "watch")
    if args.watch_action == "cancel":
        return job_cancel(args, "watch")
    if not args.job_id or not args.pane:
        die("watch start requires --job-id and --pane")
    return start_managed_worker(args, "watch", "watch")


def queue_after_idle(args: argparse.Namespace) -> dict[str, Any]:
    return start_managed_worker(args, "queue-after-idle", "queue-after-idle")


def queue_after_status(args: argparse.Namespace) -> dict[str, Any]:
    if not args.require_row:
        die("queue-after-status requires at least one --require-row")
    return start_managed_worker(args, "queue-after-status", "queue-after-status")


def read_text_arg(text: str | None, path: str | None) -> str | None:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return text


def task_add(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    task = tmux_state.build_task(
        task_id=args.task_id,
        instruction=args.instruction,
        summary=args.summary,
        intent=args.intent,
        after_job_id=args.after_job,
        after_event_id=args.after_event,
        trigger_on=args.trigger_on,
    )
    return tmux_state.write_task(paths, task)


def task_state(args: argparse.Namespace, *, create: bool = False) -> tuple[dict[str, Path], dict[str, Any]]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    if create:
        tmux_state.ensure_state_dirs(paths)
    return paths, tmux_state.load_task_state(paths)


def task_list(args: argparse.Namespace) -> Any:
    _paths, state = task_state(args)
    tasks = state["tasks"]
    if not args.all:
        tasks = [task for task in tasks if task.get("effective_status") in tmux_state.TASK_OPEN_STATUSES or task.get("stale")]
    if args.json:
        return tasks
    if not tasks:
        return "No tmux-skills tasks."
    lines = ["# tmux-skills Tasks", ""]
    for task in tasks:
        lines.append(f"- {tmux_state.task_summary_line(task)}")
    return "\n".join(lines)


def safe_commands(paths: dict[str, Path]) -> list[str]:
    return [
        f"python scripts/tmux_control.py task load --for-skill --workspace {shlex.quote(str(paths['workspace']))}",
        f"python scripts/tmux_control.py task next --json --workspace {shlex.quote(str(paths['workspace']))}",
        f"python scripts/tmux_control.py list",
    ]


def task_load_data(args: argparse.Namespace) -> dict[str, Any]:
    paths, state = task_state(args)
    classified = tmux_state.classify_task_state(state, max_items=args.max_items)
    evidence: list[str] = []
    for group in ("ready_tasks", "running", "recent_jobs", "blocked"):
        for item in classified[group]:
            for key in ("task_path", "status_path", "log_path"):
                value = item.get(key) if isinstance(item, dict) else None
                if value and value not in evidence:
                    evidence.append(value)
            for value in item.get("evidence_paths", []) if isinstance(item, dict) else []:
                if value and value not in evidence:
                    evidence.append(value)
    classified["evidence_files"] = evidence[: args.max_items * 4]
    classified["safe_commands"] = safe_commands(paths)
    return classified


def task_item_tail(item: dict[str, Any], limit: int = 400) -> str:
    tail = str(item.get("last_output") or "").strip()
    if len(tail) > limit:
        return tail[-limit:]
    return tail


def render_task_load(data: dict[str, Any], *, for_skill: bool = False) -> str:
    if for_skill:
        lines = [
            "# tmux-skills Load Report",
            "",
            "## What happened",
        ]
        if data["recent_jobs"]:
            for job in data["recent_jobs"]:
                line = f"- {job.get('id')} {job.get('status')}"
                if job.get("exit_code") is not None:
                    line += f" exit={job.get('exit_code')}"
                tail = task_item_tail(job)
                if tail:
                    line += f" tail={tail}"
                lines.append(line)
        else:
            lines.append("- No terminal jobs recorded.")
        lines.extend(["", "## Current state"])
        for item in data["running"]:
            lines.append(f"- {item.get('id') or item.get('task_id')} {item.get('status') or item.get('effective_status')}")
        if not data["running"]:
            lines.append("- No running work recorded.")
        lines.extend(["", "## Next actionable instruction"])
        if data["ready_tasks"]:
            for task in data["ready_tasks"]:
                lines.append(f"- task_id={task.get('task_id')}: {task.get('instruction')}")
        else:
            lines.append("- No ready task.")
        lines.extend(["", "## Evidence files"])
        lines.extend(f"- {path}" for path in data["evidence_files"][:10])
        if not data["evidence_files"]:
            lines.append("- None")
        lines.extend(["", "## Safe commands to inspect"])
        lines.extend(f"- `{command}`" for command in data["safe_commands"])
        lines.extend(["", "## Do not auto-run", "- Loading this report must not claim tasks or execute follow-up work."])
        return "\n".join(lines)

    lines = [
        "# tmux-skills Task Load",
        "",
        "## Workspace",
        f"- workspace: {data['workspace']}",
        f"- state_dir: {data['state_dir']}",
        "",
        "## Ready Tasks",
    ]
    lines.extend(f"- {tmux_state.task_summary_line(task)}" for task in data["ready_tasks"])
    if not data["ready_tasks"]:
        lines.append("- None")
    lines.extend(["", "## Running Work"])
    for item in data["running"]:
        lines.append(f"- {item.get('id') or item.get('task_id')} {item.get('status') or item.get('effective_status')}")
    if not data["running"]:
        lines.append("- None")
    lines.extend(["", "## Recent Jobs"])
    for job in data["recent_jobs"]:
        line = f"- {job.get('id')} {job.get('status')}"
        if job.get("exit_code") is not None:
            line += f" exit={job.get('exit_code')}"
        if job.get("log_path"):
            line += f" log={job.get('log_path')}"
        lines.append(line)
    if not data["recent_jobs"]:
        lines.append("- None")
    lines.extend(["", "## Blocked or Stale"])
    lines.extend(f"- {tmux_state.task_summary_line(task)}" for task in data["blocked"])
    if not data["blocked"]:
        lines.append("- None")
    lines.extend(["", "## Evidence Files"])
    lines.extend(f"- {path}" for path in data["evidence_files"][:10])
    if not data["evidence_files"]:
        lines.append("- None")
    lines.extend(["", "## Safe Commands"])
    lines.extend(f"- `{command}`" for command in data["safe_commands"])
    return "\n".join(lines)


def task_load(args: argparse.Namespace) -> Any:
    data = task_load_data(args)
    if args.json:
        return data
    return render_task_load(data, for_skill=args.for_skill)


def task_next(args: argparse.Namespace) -> Any:
    _paths, state = task_state(args)
    classified = tmux_state.classify_task_state(state, max_items=1)
    next_task = classified["ready_tasks"][0] if classified["ready_tasks"] else None
    if args.json:
        return next_task or {}
    return tmux_state.task_summary_line(next_task) if next_task else "No ready task."


def find_task(paths: dict[str, Path], task_id: str) -> dict[str, Any]:
    task, error = tmux_state.read_json(tmux_state.task_path(paths, task_id))
    if error or not task:
        die(f"could not load task {task_id}: {error or 'not found'}")
    return tmux_state.normalize_task(task, tmux_state.task_path(paths, task_id))


def task_claim(args: argparse.Namespace) -> dict[str, Any]:
    paths, state = task_state(args)
    task = find_task(paths, args.task_id)
    enriched = tmux_state.task_with_effective_state(task, state["statuses"])
    if enriched.get("effective_status") != "ready" and not (args.reclaim_stale and enriched.get("stale")):
        die(f"task is not ready: {args.task_id}")
    enriched["status"] = "in_progress"
    enriched["claimed_at"] = tmux_state.utc_now()
    enriched["blocked_reason"] = None
    return tmux_state.write_task(paths, enriched)


def task_finish(args: argparse.Namespace, status: str) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    task = find_task(paths, args.task_id)
    task["status"] = status
    task["completed_at"] = tmux_state.utc_now() if status in {"done", "cancelled"} else task.get("completed_at")
    if status == "blocked":
        task["blocked_reason"] = args.note
    elif args.note:
        task["summary"] = args.note
    return tmux_state.write_task(paths, task)


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    requested: dict[str, Any] = {
        "target": args.target,
        "current_window": args.current_window,
        "pane_index": args.pane_index,
        "ordinal": args.ordinal,
    }
    if args.ordinal is not None and args.ordinal < 1:
        die("--ordinal is 1-based and must be >= 1")

    if args.pane_index is None and args.ordinal is None:
        if not args.target:
            target = current_window_target() if args.current_window or inside_tmux() else None
            if target is None:
                die("resolve without --target requires running inside tmux")
        else:
            target = args.target
        info = current_info(target)
        if not info:
            die(f"could not resolve target: {target}")
        return {"requested": requested, "resolved": True, "resolved_by": "target", **info}

    if args.current_window:
        if not inside_tmux():
            die("--current-window requires running inside tmux")
        scope_target = current_window_target()
    elif args.target:
        scope_target = args.target
    elif inside_tmux():
        scope_target = current_window_target()
        requested["implicit_scope"] = "current-window"
    else:
        die("resolving by pane index or ordinal requires --target or running inside tmux")

    panes = panes_for_target(scope_target)
    if not panes:
        die(f"no panes found for target: {scope_target}")

    match: dict[str, Any] | None = None
    resolved_by = ""
    if args.pane_index is not None:
        pane_index = str(args.pane_index)
        match = next((pane for pane in panes if pane["pane_index"] == pane_index), None)
        resolved_by = "pane_index"
    elif args.ordinal is not None:
        panes_sorted = sorted(panes, key=lambda pane: int(pane["pane_index"]))
        if args.ordinal <= len(panes_sorted):
            match = panes_sorted[args.ordinal - 1]
        resolved_by = "ordinal"

    if not match:
        available = [
            {"pane_id": pane["pane_id"], "pane_index": pane["pane_index"], "current_command": pane["current_command"]}
            for pane in panes
        ]
        return {
            "requested": requested,
            "resolved": False,
            "reason": "no matching pane found",
            "scope_target": scope_target,
            "available_panes": available,
        }

    ambiguity_note = None
    if args.pane_index is not None:
        ambiguity_note = "pane_index is tmux 0-based; use --ordinal for human 1-based pane numbers"
    elif args.ordinal is not None:
        ambiguity_note = "ordinal is human 1-based; use --pane-index for tmux pane_index"

    return {
        "requested": requested,
        "resolved": True,
        "resolved_by": resolved_by,
        "scope_target": scope_target,
        "ambiguity_note": ambiguity_note,
        **match,
    }


def print_json(data: Any) -> None:
    try:
        print(json.dumps(data, indent=2, sort_keys=True))
    except BrokenPipeError:
        raise SystemExit(0)


def print_result(data: Any) -> None:
    if isinstance(data, str):
        print(data)
    else:
        print_json(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex tmux control helper")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("list", help="List tmux sessions, windows, and panes")

    current_parser = subparsers.add_parser("current", help="Show the current tmux session, window, and pane")
    current_parser.add_argument("--target", help="Inspect a specific tmux target instead of the current client")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a human target to a stable tmux pane ID")
    resolve_parser.add_argument("--target", help="tmux target such as %%3, SESSION:WINDOW, or SESSION:WINDOW.PANE")
    resolve_parser.add_argument("--current-window", action="store_true", help="Limit pane-index/ordinal lookup to the current window")
    resolve_group = resolve_parser.add_mutually_exclusive_group()
    resolve_group.add_argument("--pane-index", type=int, help="tmux pane_index value, usually 0-based")
    resolve_group.add_argument("--ordinal", type=int, help="Human 1-based pane number within the selected window")

    spawn_parser = subparsers.add_parser("spawn", help="Create a pane for long-running work")
    spawn_parser.add_argument("--target", help="tmux target such as SESSION:WINDOW")
    spawn_parser.add_argument("--cwd", help="Working directory for the new pane")
    orientation = spawn_parser.add_mutually_exclusive_group()
    orientation.add_argument("--vertical", action="store_true", help="Split vertically")
    orientation.add_argument("--horizontal", action="store_true", help="Split horizontally")
    spawn_parser.add_argument("--percent", type=int, help="Pane size percentage")

    new_window_parser = subparsers.add_parser("new-window", help="Create a tmux window")
    new_window_parser.add_argument("--cwd", required=True, help="Working directory for the new window")
    new_window_parser.add_argument("--target", help="tmux session target")
    new_window_parser.add_argument("--name", help="Window name")
    new_window_parser.add_argument("--workspace", help="Workspace root for managed sessions and state paths")
    new_window_parser.add_argument("--state-dir", help="State directory; accepted for interface consistency")

    send_parser = subparsers.add_parser("send", help="Send literal command text to a pane")
    send_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    send_parser.add_argument("--command", dest="command_text", required=True, help="Literal command text")
    send_parser.add_argument(
        "--require-idle-shell",
        action="store_true",
        help="Refuse to send unless the pane looks like an idle shell prompt",
    )
    send_parser.add_argument("--strict-preflight", action="store_true", help="Fail before sending when script preflight detects a problem")
    send_parser.add_argument(
        "--bash-if-not-executable",
        action="store_true",
        help="Rewrite direct non-executable script commands to bash path/to/script",
    )
    enter_group = send_parser.add_mutually_exclusive_group(required=True)
    enter_group.add_argument("--enter", action="store_true", help="Press Enter after sending")
    enter_group.add_argument("--no-enter", action="store_true", help="Stage command without Enter")

    capture_parser = subparsers.add_parser("capture", help="Capture pane output")
    capture_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    capture_parser.add_argument("--lines", type=int, default=200, help="Number of recent lines")
    capture_parser.add_argument("--strip-ansi", action="store_true", help="Remove ANSI/control escape sequences from output")
    capture_parser.add_argument("--max-chars", type=int, help="Return only the last N characters after optional ANSI stripping")

    run_parser = subparsers.add_parser("run", help="Run a long-running command through the status wrapper")
    run_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    command_group = run_parser.add_mutually_exclusive_group(required=True)
    command_group.add_argument("--command", dest="command_text", help="Command text to execute through /bin/sh")
    command_group.add_argument("--command-file", help="Path to command text to copy into state")
    run_parser.add_argument("--job-id")
    run_parser.add_argument("--name")
    run_parser.add_argument("--cwd")
    run_parser.add_argument("--workspace")
    run_parser.add_argument("--state-dir")
    run_parser.add_argument("--require-idle-shell", action="store_true")
    run_parser.add_argument("--next-instruction", help="Codex instruction to make ready after this job finishes")
    run_parser.add_argument("--next-instruction-file", help="File containing the follow-up Codex instruction")
    run_parser.add_argument(
        "--next-on",
        choices=["succeeded", "failed", "terminal"],
        default="succeeded",
        help="Job terminal state that makes the follow-up task ready",
    )

    monitor_parser = subparsers.add_parser("monitor", help="Start a background single-trigger pane monitor")
    monitor_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    monitor_parser.add_argument("--match-regex")
    monitor_parser.add_argument("--idle-shell", action="store_true")
    monitor_parser.add_argument("--timeout-seconds", type=float)
    monitor_parser.add_argument("--poll-seconds", type=float, default=2.0)
    monitor_parser.add_argument("--lines", type=int, default=200)
    monitor_parser.add_argument("--workspace")
    monitor_parser.add_argument("--state-dir")

    watch_parser = subparsers.add_parser("watch", help="Start or inspect a managed recurring pane watch")
    watch_parser.add_argument("watch_action", nargs="?", choices=["start", "list", "status", "cancel"], default="start")
    watch_parser.add_argument("--job-id")
    watch_parser.add_argument("--pane", help="Stable tmux pane ID, such as %%3")
    watch_parser.add_argument("--interval", type=float, default=180.0)
    watch_parser.add_argument("--capture-lines", type=int, default=80)
    watch_parser.add_argument("--status-file")
    watch_parser.add_argument("--timeout-seconds", type=float)
    watch_parser.add_argument("--name")
    watch_parser.add_argument("--workspace")
    watch_parser.add_argument("--state-dir")
    watch_parser.add_argument("--replace", action="store_true", help="Replace a running managed worker with the same job id")
    watch_parser.add_argument("--allow-duplicate", action="store_true", help="Allow another active worker with the same dedupe key")
    watch_parser.add_argument("--owner", help="Owner metadata for this managed worker")

    queue_idle_parser = subparsers.add_parser("queue-after-idle", help="Submit a command after a pane becomes an idle shell")
    queue_idle_parser.add_argument("--job-id", required=True)
    queue_idle_parser.add_argument("--pane", "--then-pane", dest="pane", required=True, help="Stable tmux pane ID, such as %%3")
    queue_idle_parser.add_argument("--command", "--then-command", dest="command_text", required=True)
    queue_idle_parser.add_argument("--poll-seconds", "--interval", dest="poll_seconds", type=float, default=2.0)
    queue_idle_parser.add_argument("--timeout-seconds", type=float)
    queue_idle_parser.add_argument("--then-require-idle-shell", dest="require_idle_shell", action="store_true", default=True)
    queue_idle_parser.add_argument("--strict-preflight", action="store_true")
    queue_idle_parser.add_argument("--bash-if-not-executable", action="store_true")
    queue_idle_parser.add_argument("--name")
    queue_idle_parser.add_argument("--workspace")
    queue_idle_parser.add_argument("--state-dir")
    queue_idle_parser.add_argument("--replace", action="store_true", help="Replace a running managed worker with the same job id")
    queue_idle_parser.add_argument("--allow-duplicate", action="store_true", help="Allow another active worker with the same dedupe key")
    queue_idle_parser.add_argument("--owner", help="Owner metadata for this managed worker")

    queue_status_parser = subparsers.add_parser("queue-after-status", help="Submit a command after status-file rows are satisfied")
    queue_status_parser.add_argument("--job-id", required=True)
    queue_status_parser.add_argument("--pane", "--then-pane", dest="pane", required=True, help="Stable tmux pane ID, such as %%3")
    queue_status_parser.add_argument("--command", "--then-command", dest="command_text", required=True)
    queue_status_parser.add_argument("--status-file", required=True)
    queue_status_parser.add_argument("--require-row", action="append", default=[])
    queue_status_parser.add_argument("--fail-row", action="append", default=[])
    queue_status_parser.add_argument("--poll-seconds", "--interval", dest="poll_seconds", type=float, default=2.0)
    queue_status_parser.add_argument("--timeout-seconds", type=float)
    queue_status_parser.add_argument("--then-require-idle-shell", dest="require_idle_shell", action="store_true")
    queue_status_parser.add_argument("--no-require-idle-shell", dest="require_idle_shell", action="store_false")
    queue_status_parser.set_defaults(require_idle_shell=True)
    queue_status_parser.add_argument("--strict-preflight", action="store_true")
    queue_status_parser.add_argument("--bash-if-not-executable", action="store_true")
    queue_status_parser.add_argument("--name")
    queue_status_parser.add_argument("--workspace")
    queue_status_parser.add_argument("--state-dir")
    queue_status_parser.add_argument("--replace", action="store_true", help="Replace a running managed worker with the same job id")
    queue_status_parser.add_argument("--allow-duplicate", action="store_true", help="Allow another active worker with the same dedupe key")
    queue_status_parser.add_argument("--owner", help="Owner metadata for this managed worker")

    job_parser = subparsers.add_parser("job", help="Inspect or cancel managed background workers")
    job_subparsers = job_parser.add_subparsers(dest="job_action", required=True)
    job_list_parser = job_subparsers.add_parser("list", help="List managed workers")
    job_list_parser.add_argument("--workspace")
    job_list_parser.add_argument("--state-dir")
    job_status_parser = job_subparsers.add_parser("status", help="Show one managed worker")
    job_status_parser.add_argument("--job-id", required=True)
    job_status_parser.add_argument("--workspace")
    job_status_parser.add_argument("--state-dir")
    job_cancel_parser = job_subparsers.add_parser("cancel", help="Cancel one managed worker")
    job_cancel_parser.add_argument("--job-id", required=True)
    job_cancel_parser.add_argument("--workspace")
    job_cancel_parser.add_argument("--state-dir")
    job_gc_parser = job_subparsers.add_parser("gc", help="Mark stale managed workers")
    job_gc_parser.add_argument("--stale", action="store_true", help="Mark stale active managed jobs")
    job_gc_parser.add_argument("--dry-run", action="store_true", help="Only report stale active managed jobs")
    job_gc_parser.add_argument("--workspace")
    job_gc_parser.add_argument("--state-dir")

    task_parser = subparsers.add_parser("task", help="Manage tmux-skills follow-up tasks")
    task_subparsers = task_parser.add_subparsers(dest="task_action", required=True)

    task_add_parser = task_subparsers.add_parser("add", help="Add a Codex follow-up task")
    task_add_parser.add_argument("--task-id")
    task_add_parser.add_argument("--after-job")
    task_add_parser.add_argument("--after-event")
    task_add_parser.add_argument("--trigger-on", choices=["succeeded", "failed", "terminal"], required=True)
    task_add_parser.add_argument("--instruction", required=True)
    task_add_parser.add_argument("--summary")
    task_add_parser.add_argument("--intent")
    task_add_parser.add_argument("--workspace")
    task_add_parser.add_argument("--state-dir")

    task_list_parser = task_subparsers.add_parser("list", help="List follow-up tasks")
    task_list_parser.add_argument("--all", action="store_true")
    task_list_parser.add_argument("--json", action="store_true")
    task_list_parser.add_argument("--workspace")
    task_list_parser.add_argument("--state-dir")

    task_load_parser = task_subparsers.add_parser("load", help="Load a skill-friendly task report")
    task_load_parser.add_argument("--for-skill", action="store_true")
    task_load_parser.add_argument("--json", action="store_true")
    task_load_parser.add_argument("--max-items", type=int, default=5)
    task_load_parser.add_argument("--workspace")
    task_load_parser.add_argument("--state-dir")

    task_next_parser = task_subparsers.add_parser("next", help="Show the next ready task")
    task_next_parser.add_argument("--json", action="store_true")
    task_next_parser.add_argument("--workspace")
    task_next_parser.add_argument("--state-dir")

    task_claim_parser = task_subparsers.add_parser("claim", help="Claim a ready task")
    task_claim_parser.add_argument("--task-id", required=True)
    task_claim_parser.add_argument("--reclaim-stale", action="store_true")
    task_claim_parser.add_argument("--workspace")
    task_claim_parser.add_argument("--state-dir")

    for action in ("done", "blocked", "cancel"):
        finish_parser = task_subparsers.add_parser(action, help=f"Mark a task {action}")
        finish_parser.add_argument("--task-id", required=True)
        finish_parser.add_argument("--note")
        finish_parser.add_argument("--workspace")
        finish_parser.add_argument("--state-dir")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.action == "list":
        print_json(list_panes())
    elif args.action == "current":
        print_json(
            {
                "inside_tmux": inside_tmux(),
                "tmux_tmpdir": tmux_tmpdir_value(),
                "current": current_info(args.target),
            }
        )
    elif args.action == "resolve":
        print_json(resolve(args))
    elif args.action == "spawn":
        print_json(spawn(args))
    elif args.action == "new-window":
        print_json(new_window(args))
    elif args.action == "send":
        result = send(args)
        print_json(result)
        if not result.get("sent_to_pane"):
            raise SystemExit(2)
    elif args.action == "capture":
        print_json(capture(args))
    elif args.action == "run":
        result = run_job(args)
        print_json(result)
        if not result.get("sent"):
            raise SystemExit(2)
    elif args.action == "monitor":
        if not args.match_regex and not args.idle_shell and args.timeout_seconds is None:
            die("monitor requires --match-regex, --idle-shell, or --timeout-seconds")
        print_json(monitor(args))
    elif args.action == "watch":
        if args.watch_action in {"status", "cancel"} and not args.job_id:
            die(f"watch {args.watch_action} requires --job-id")
        result = watch(args)
        print_json(result)
        if args.watch_action == "start" and not result.get("started"):
            raise SystemExit(2)
    elif args.action == "queue-after-idle":
        result = queue_after_idle(args)
        print_json(result)
        if not result.get("started"):
            raise SystemExit(2)
    elif args.action == "queue-after-status":
        result = queue_after_status(args)
        print_json(result)
        if not result.get("started"):
            raise SystemExit(2)
    elif args.action == "job":
        if args.job_action == "list":
            print_json(job_list(args))
        elif args.job_action == "status":
            print_json(job_status(args))
        elif args.job_action == "cancel":
            print_json(job_cancel(args))
        elif args.job_action == "gc":
            print_json(job_gc(args))
        else:
            parser.error(f"unknown job command: {args.job_action}")
    elif args.action == "task":
        if args.task_action == "add":
            print_json(task_add(args))
        elif args.task_action == "list":
            print_result(task_list(args))
        elif args.task_action == "load":
            print_result(task_load(args))
        elif args.task_action == "next":
            print_result(task_next(args))
        elif args.task_action == "claim":
            print_json(task_claim(args))
        elif args.task_action == "done":
            print_json(task_finish(args, "done"))
        elif args.task_action == "blocked":
            print_json(task_finish(args, "blocked"))
        elif args.task_action == "cancel":
            print_json(task_finish(args, "cancelled"))
        else:
            parser.error(f"unknown task command: {args.task_action}")
    else:
        parser.error(f"unknown command: {args.action}")


if __name__ == "__main__":
    main()
