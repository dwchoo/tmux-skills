#!/usr/bin/env python3
"""Small tmux helper for Codex skills."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import tmux_state
import tmux_bridge
import tmux_manager
from tmux_text import prompt_like, strip_ansi


FIELD_SEP = "\x1f"
TMUX_ESCAPED_FIELD_SEP = "\\037"
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
    "#{pane_left}",
    "#{pane_top}",
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
    "#{pane_left}",
    "#{pane_top}",
    "#{pane_tty}",
]
PANE_FORMAT = FIELD_SEP.join(PANE_FIELDS)
CURRENT_FORMAT = FIELD_SEP.join(CURRENT_FIELDS)
SPAWN_FORMAT = FIELD_SEP.join(["#{session_name}", "#{window_id}", "#{pane_id}"])
NEW_WINDOW_FORMAT = FIELD_SEP.join(["#{session_name}", "#{window_id}", "#{window_index}", "#{pane_id}"])
SHELL_COMMANDS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "mksh"}


def split_tmux_fields(line: str) -> list[str]:
    """Split tmux format output across tmux versions.

    tmux 3.4 prints control separators in format output as octal escapes
    (``\037``) rather than the literal unit separator used in the format
    string. Keep FIELD_SEP as the in-memory delimiter so tabs and common
    punctuation remain valid inside fields, but accept tmux's escaped form
    when parsing real command output.
    """
    return line.replace(TMUX_ESCAPED_FIELD_SEP, FIELD_SEP).split(FIELD_SEP)



def tmux_socket_from_env(value: str | None = None) -> str | None:
    raw = value if value is not None else os.environ.get("TMUX")
    if not raw:
        return None
    socket_path = raw.split(",", 1)[0].strip()
    return socket_path or None


def default_tmux_socket() -> str:
    return str(Path("/tmp") / f"tmux-{os.getuid()}" / "default")


def socket_exists(path: str | None) -> bool:
    return bool(path and Path(path).exists())


def selected_tmux_socket() -> str | None:
    explicit = os.environ.get("TMUX_SKILLS_SOCKET")
    if explicit:
        return explicit

    current = tmux_socket_from_env()
    default_socket = default_tmux_socket()
    if current and "codex-tmux-control" in current and socket_exists(default_socket):
        return default_socket
    return None


def tmux_command_prefix() -> list[str]:
    socket_path = selected_tmux_socket()
    if socket_path:
        return ["tmux", "-S", socket_path]
    return ["tmux"]

def tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    socket_path = selected_tmux_socket()
    if socket_path:
        env["TMUX_SKILLS_SOCKET"] = socket_path
    return env


def tmux_tmpdir_value() -> str | None:
    if selected_tmux_socket():
        return None
    return os.environ.get("TMUX_TMPDIR")


def die(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def run_tmux(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [*tmux_command_prefix(), *args],
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


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def split_percent(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 99") from exc
    if number < 1 or number > 99:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 99")
    return number


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
    parts = split_tmux_fields(line)
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
        pane_left,
        pane_top,
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
            "pane_left": parse_int(pane_left),
            "pane_top": parse_int(pane_top),
            "pane_tty": pane_tty,
        }
    )


def parse_pane_line(line: str, *, current_pane_id: str | None = None) -> dict[str, Any] | None:
    parts = split_tmux_fields(line)
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
        pane_left,
        pane_top,
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
            "pane_left": parse_int(pane_left),
            "pane_top": parse_int(pane_top),
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
        "tmux_socket": selected_tmux_socket() or tmux_socket_from_env(),
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


def ensure_managed_session(workspace: Path, cwd: Path, name: str | None = None) -> tuple[str, str | None]:
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
            name or "work",
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
            SPAWN_FORMAT,
            "-c",
            str(cwd),
            "-t",
            target,
        ]
    )
    session_name, window_id, pane_id = split_tmux_fields(result.stdout.strip())
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
        session, existing_window_id = ensure_managed_session(workspace, cwd, args.name)
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
            NEW_WINDOW_FORMAT,
            *name_args,
            "-c",
            str(cwd),
            "-t",
            target,
        ]
    )
    session_name, window_id, window_index, pane_id = split_tmux_fields(result.stdout.strip())
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


def capture_text(pane: str, lines: int, *, strip: bool = False) -> str:
    result = run_tmux(["capture-pane", "-p", "-t", pane, "-S", f"-{lines}"])
    output = result.stdout.rstrip("\n")
    return strip_ansi(output) if strip else output


def idle_shell_check(pane: str) -> dict[str, Any]:
    info = current_info(pane)
    if not info:
        return {"ok": False, "reason": "pane could not be resolved", "pane_id": pane}

    command = str(info.get("current_command") or "")
    pane_pid = parse_int(str(info.get("pane_pid") or ""))
    immediate_count, summary, descendant_count = descendant_processes(pane_pid)
    diagnostics = {
        "pane_id": info.get("pane_id", pane),
        "current_command": command,
        "pane_pid": info.get("pane_pid"),
        "child_process_count": immediate_count,
        "descendant_process_count": descendant_count,
        "descendant_summary": summary,
    }
    try:
        prompt_output = capture_text(pane, 20, strip=True)
    except (Exception, SystemExit) as exc:
        error = repr(exc) if isinstance(exc, SystemExit) else str(exc)
        return {"ok": False, "reason": f"could not capture pane output: {error}", **diagnostics}
    has_prompt = prompt_like(prompt_output)
    diagnostics["prompt_detected"] = has_prompt

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

    bash_target = str(resolved) if first.startswith("~") else first
    warning = f"preflight_warning: {first} is not executable; use 'bash {bash_target}' or chmod +x."
    return {
        "ok": False,
        "warnings": [warning],
        "script_path": str(resolved),
        "bash_command": " ".join(["bash", shlex.quote(bash_target), *(shlex.quote(part) for part in parts[1:])]),
    }


def needs_script_preflight_cwd(command_text: str) -> bool:
    try:
        parts = shlex.split(command_text, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    first = parts[0]
    if first in {"bash", "sh", "zsh", "fish", "dash", "ksh"}:
        return False
    return first.startswith("./") or first.startswith("../") or first.endswith(".sh")


def send_preflight_cwd(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "cwd", None)
    if explicit:
        return str(explicit)
    if not needs_script_preflight_cwd(args.command_text):
        return None
    info = current_info(args.pane)
    current_path = (info or {}).get("current_path")
    return str(current_path) if current_path else None


def send(args: argparse.Namespace) -> dict[str, Any]:
    guard: dict[str, Any] | None = None
    preflight = script_preflight(args.command_text, send_preflight_cwd(args))
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
    if max_chars is None:
        return text, False, 0
    if max_chars <= 0:
        return "", bool(text), len(text)
    if len(text) <= max_chars:
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
    job_id_arg = getattr(args, "job_id", None)
    if job_id_arg is not None and not tmux_state.one_line_text(job_id_arg):
        die("run requires nonblank --job-id when provided")
    item_id = tmux_state.safe_id(job_id_arg or f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    attempt = next_attempt(paths, item_id)
    cmd_file = tmux_state.command_path(paths, item_id)
    status_file = tmux_state.status_path(paths, item_id)
    log_file = tmux_state.log_path(paths, item_id)

    def finalize_start_failure(reason: str, command_preview_text: str | None) -> dict[str, Any]:
        failed = tmux_state.build_status(
            kind="job",
            item_id=item_id,
            attempt=attempt,
            name=args.name,
            status="failed",
            pane_id=args.pane,
            command_preview_text=command_preview_text,
            cwd=str(cwd),
            status_file=status_file,
            log_file=log_file,
            exit_code=1,
            last_output=reason,
        )
        tmux_state.write_status(status_file, failed)
        return {
            "job_id": item_id,
            "attempt": attempt,
            "pane_id": args.pane,
            "sent": False,
            "reason": reason,
            "status": "failed",
            "command_path": str(cmd_file),
            "status_path": str(status_file),
            "log_path": str(log_file),
            "workspace": str(paths["workspace"]),
            "state_dir": str(paths["root"]),
            "next_task": None,
        }

    command_file_arg = getattr(args, "command_file", None)
    try:
        if command_file_arg is not None:
            if not tmux_state.one_line_text(command_file_arg):
                return finalize_start_failure("command file path is blank", None)
            source_command_file = Path(str(command_file_arg)).expanduser()
            command_text = source_command_file.read_text(encoding="utf-8")
        else:
            command_text = "" if getattr(args, "command_text", None) is None else str(args.command_text)
    except Exception as exc:
        return finalize_start_failure(
            f"could not read command file: {exc}",
            str(Path(str(command_file_arg)).expanduser()),
        )

    if not tmux_state.one_line_text(command_text):
        return finalize_start_failure("command is blank", tmux_state.command_preview(command_text))

    try:
        next_instruction = read_text_arg(getattr(args, "next_instruction", None), getattr(args, "next_instruction_file", None))
    except Exception as exc:
        return finalize_start_failure(f"could not read next instruction: {exc}", tmux_state.command_preview(command_text))
    if next_instruction is not None and not tmux_state.one_line_text(next_instruction):
        return finalize_start_failure("next instruction is blank", tmux_state.command_preview(command_text))

    try:
        write_command_file(cmd_file, command_text)
    except Exception as exc:
        return finalize_start_failure(f"could not write command file: {exc}", tmux_state.command_preview(command_text))

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

    def finalize_send_failure(reason: Any, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        nonlocal next_task
        failed = tmux_state.build_status(
            kind="job",
            item_id=item_id,
            attempt=attempt,
            name=args.name,
            status="failed",
            pane_id=args.pane,
            command_preview_text=tmux_state.command_preview(command_text),
            cwd=str(cwd),
            status_file=status_file,
            log_file=log_file,
            exit_code=1,
            last_output=f"command was not sent to pane: {reason}",
        )
        failed = tmux_state.write_status(status_file, failed)
        if next_task is not None:
            if tmux_state.status_matches_trigger(failed, str(next_task.get("trigger_on") or "succeeded")):
                if result is not None:
                    result["next_task"] = next_task
            else:
                next_task["status"] = "cancelled"
                next_task["blocked_reason"] = "job command was not sent to pane"
                next_task = tmux_state.write_task(paths, next_task)
                if result is not None:
                    result["next_task"] = next_task
        if result is not None:
            result["status"] = "failed"
        return result

    try:
        send_result = send(send_args)
    except BaseException as exc:
        finalize_send_failure(exc)
        raise

    result = {
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
    if not send_result.get("sent_to_pane", False):
        finalize_send_failure(send_result.get("reason"), result)
    return result


def default_manager_id(paths: dict[str, Path], current: dict[str, Any] | None = None) -> str:
    parts = ["manager", paths["workspace"].name or "workspace"]
    if current:
        parts.append(str(current.get("session_name") or "session"))
        parts.append(str(current.get("window_id") or current.get("window_index") or "window"))
    else:
        parts.append("default")
    return tmux_manager.manager_id_value("-".join(parts))


def resolve_manager_id_arg(
    value: str | None,
    workspace: str | None = None,
    state_dir: str | None = None,
    current: dict[str, Any] | None = None,
) -> str:
    if tmux_state.one_line_text(value):
        return tmux_manager.manager_id_value(value)
    paths = tmux_manager.manager_paths(workspace, state_dir)
    if current is None and inside_tmux():
        current = current_info()
    return default_manager_id(paths, current)


def pane_number(pane: dict[str, Any], key: str) -> int:
    value = pane.get(key)
    return value if isinstance(value, int) else 0


def panes_same_window(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("session_name") == right.get("session_name")
        and left.get("window_id") == right.get("window_id")
    )


def horizontal_overlap(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_start = pane_number(left, "pane_left")
    left_end = left_start + pane_number(left, "pane_width")
    right_start = pane_number(right, "pane_left")
    right_end = right_start + pane_number(right, "pane_width")
    return max(0, min(left_end, right_end) - max(left_start, right_start))


def pane_is_reusable_idle(pane_id: str | None) -> bool:
    if not tmux_state.one_line_text(pane_id):
        return False
    info = current_info(str(pane_id))
    if not info or info.get("pane_dead"):
        return False
    check = idle_shell_check(str(pane_id))
    return bool(check.get("ok"))


def find_idle_pane_below(
    base: dict[str, Any],
    panes: list[dict[str, Any]],
    *,
    exclude_ids: set[str],
) -> dict[str, Any] | None:
    base_bottom = pane_number(base, "pane_top") + pane_number(base, "pane_height")
    base_left = pane_number(base, "pane_left")
    candidates: list[tuple[int, int, int, dict[str, Any]]] = []
    for pane in panes:
        pane_id = str(pane.get("pane_id") or "")
        if not pane_id or pane_id in exclude_ids or pane.get("pane_dead"):
            continue
        if not panes_same_window(base, pane):
            continue
        overlap = horizontal_overlap(base, pane)
        if overlap <= 0:
            continue
        gap = pane_number(pane, "pane_top") - base_bottom
        if gap < 0 or gap > 2:
            continue
        if not pane_is_reusable_idle(pane_id):
            continue
        candidates.append((gap, abs(pane_number(pane, "pane_left") - base_left), -overlap, pane))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def pane_is_below_base(pane: dict[str, Any], base: dict[str, Any]) -> bool:
    if not panes_same_window(base, pane):
        return False
    base_bottom = pane_number(base, "pane_top") + pane_number(base, "pane_height")
    gap = pane_number(pane, "pane_top") - base_bottom
    return 0 <= gap <= 2 and horizontal_overlap(base, pane) > 0


def pane_is_tall_side_worker(pane: dict[str, Any], base: dict[str, Any]) -> bool:
    if not panes_same_window(base, pane):
        return False
    base_right = pane_number(base, "pane_left") + pane_number(base, "pane_width")
    pane_left = pane_number(pane, "pane_left")
    if pane_left < base_right - 2:
        return False
    if pane_number(pane, "pane_top") > pane_number(base, "pane_top") + 2:
        return False
    return pane_number(pane, "pane_height") >= pane_number(base, "pane_height")


def find_idle_tall_worker_pane(
    base: dict[str, Any],
    panes: list[dict[str, Any]],
    *,
    exclude_ids: set[str],
) -> dict[str, Any] | None:
    base_right = pane_number(base, "pane_left") + pane_number(base, "pane_width")
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for pane in panes:
        pane_id = str(pane.get("pane_id") or "")
        if not pane_id or pane_id in exclude_ids or pane.get("pane_dead"):
            continue
        if not pane_is_tall_side_worker(pane, base):
            continue
        if not pane_is_reusable_idle(pane_id):
            continue
        candidates.append((abs(pane_number(pane, "pane_left") - base_right), -pane_number(pane, "pane_height"), pane))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[:2])
    return candidates[0][2]


def reusable_record_pane(
    record: dict[str, Any] | None,
    key: str,
    anchor: dict[str, Any],
    *,
    exclude_ids: set[str],
    validator: Any | None = None,
) -> dict[str, Any] | None:
    if not record:
        return None
    pane_id = str(record.get(key) or "")
    if not pane_id or pane_id in exclude_ids:
        return None
    pane = current_info(pane_id)
    if not pane or pane.get("pane_dead") or not panes_same_window(anchor, pane):
        return None
    if validator and not validator(pane, anchor):
        return None
    if not pane_is_reusable_idle(pane_id):
        return None
    return pane


def split_pane(cwd: Path, *, target: str, vertical: bool, percent: int, full_size: bool = False) -> tuple[str, str, str]:
    tmux_args = [
        "split-window",
        "-v" if vertical else "-h",
    ]
    if full_size:
        tmux_args.append("-f")
    tmux_args.extend(
        [
            "-p",
            str(percent),
            "-P",
            "-F",
            SPAWN_FORMAT,
            "-c",
            str(cwd),
            "-t",
            target,
        ]
    )
    result = run_tmux(
        tmux_args
    )
    session_name, window_id, pane_id = split_tmux_fields(result.stdout.strip())
    return session_name, window_id, pane_id


def pane_index_value(pane: dict[str, Any] | None) -> str | None:
    if not pane:
        return None
    return tmux_state.one_line_text(pane.get("pane_index")) or None


def layout_from_existing_manager_record(record: dict[str, Any], cwd: Path) -> dict[str, Any]:
    manager_pane_id = str(record.get("manager_pane_id") or "")
    worker_pane_id = str(record.get("worker_pane_id") or "")
    manager_pane = None
    worker_pane = None
    if manager_pane_id:
        manager_pane = current_info(manager_pane_id)
    if worker_pane_id:
        worker_pane = current_info(worker_pane_id)
    pane = manager_pane or worker_pane or {}
    session_name = str(pane.get("session_name") or "")
    window_id = str(pane.get("window_id") or "")
    window_index = str(pane.get("window_index") or "")
    target = f"{session_name}:{window_index}" if session_name and window_index else ""
    return {
        "session_name": session_name,
        "window_id": window_id,
        "manager_window_id": window_id,
        "worker_pane_id": worker_pane_id,
        "worker_pane_index": pane_index_value(worker_pane) or tmux_state.one_line_text(record.get("worker_pane_index")),
        "manager_pane_id": manager_pane_id,
        "manager_pane_index": pane_index_value(manager_pane) or tmux_state.one_line_text(record.get("manager_pane_index")),
        "manager_reused": True,
        "worker_reused": True,
        "target": target,
        "cwd": str(cwd),
        "attach_command": record.get("attach_command"),
        "tmux_tmpdir": tmux_tmpdir_value(),
    }


def manager_layout(
    cwd: Path,
    *,
    paths: dict[str, Path] | None = None,
    manager_id: str | None = None,
    existing_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attach_command = None
    original_pane_id = None
    if inside_tmux():
        current = current_info()
        original_pane_id = current.get("pane_id") if current else None
        target = current_window_target()
    else:
        session, target = ensure_managed_target(cwd)
        tmpdir = tmux_tmpdir_value()
        attach_command = f"TMUX_TMPDIR={tmpdir} tmux attach -t {session}" if tmpdir else f"tmux attach -t {session}"
        current = current_info(target)

    if not current:
        die("could not resolve current tmux pane for manager layout")
    if existing_record is None and paths and manager_id:
        existing_record, _error = tmux_manager.read_manager_record(paths, manager_id)

    codex_pane_id = str(current.get("pane_id") or "")
    panes = panes_for_target(target, current_pane_id=codex_pane_id)
    session_name = str(current.get("session_name") or "")
    window_id = str(current.get("window_id") or "")

    worker_reused = False
    worker_pane = reusable_record_pane(
        existing_record,
        "worker_pane_id",
        current,
        exclude_ids={codex_pane_id},
        validator=pane_is_tall_side_worker,
    )
    if worker_pane is None:
        worker_pane = find_idle_tall_worker_pane(current, panes, exclude_ids={codex_pane_id})
    if worker_pane is None:
        session_name, window_id, worker_pane_id = split_pane(
            cwd,
            target=codex_pane_id,
            vertical=False,
            percent=50,
            full_size=True,
        )
        worker_pane_index = pane_index_value(current_info(worker_pane_id))
    else:
        worker_pane_id = str(worker_pane["pane_id"])
        worker_pane_index = pane_index_value(worker_pane)
        worker_reused = True

    current = current_info(codex_pane_id) or current
    panes = panes_for_target(target, current_pane_id=codex_pane_id)

    manager_reused = False
    manager_pane = reusable_record_pane(
        existing_record,
        "manager_pane_id",
        current,
        exclude_ids={codex_pane_id, worker_pane_id},
        validator=pane_is_below_base,
    )
    if manager_pane is None:
        manager_pane = find_idle_pane_below(current, panes, exclude_ids={codex_pane_id, worker_pane_id})
    if manager_pane is None:
        session_name, window_id, manager_pane_id = split_pane(cwd, target=codex_pane_id, vertical=True, percent=20)
        manager_window_id = window_id
        manager_pane_index = pane_index_value(current_info(manager_pane_id))
    else:
        manager_pane_id = str(manager_pane["pane_id"])
        manager_pane_index = pane_index_value(manager_pane)
        manager_window_id = str(manager_pane.get("window_id") or window_id)
        manager_reused = True

    if original_pane_id:
        run_tmux(["select-pane", "-t", original_pane_id], check=False)
    return {
        "session_name": session_name,
        "window_id": window_id,
        "manager_window_id": manager_window_id,
        "worker_pane_id": worker_pane_id,
        "worker_pane_index": worker_pane_index,
        "manager_pane_id": manager_pane_id,
        "manager_pane_index": manager_pane_index,
        "manager_reused": manager_reused,
        "worker_reused": worker_reused,
        "target": target,
        "cwd": str(cwd),
        "attach_command": attach_command,
        "tmux_tmpdir": tmux_tmpdir_value(),
    }


def manager_start(args: argparse.Namespace) -> dict[str, Any]:
    has_job = bool(tmux_state.one_line_text(args.job_id))
    has_command = args.command_text is not None or args.command_file is not None
    if has_job != has_command:
        return {
            "manager_id": args.manager_id,
            "started": False,
            "status": "failed",
            "reason": "manager start requires --job-id and exactly one command source together, or neither for idle start",
        }
    job_id = tmux_state.safe_id(args.job_id) if has_job else None
    try:
        notify = tmux_manager.normalize_notify(args.notify, args.thread_id, args.endpoint, getattr(args, "codex_pane", None))
    except Exception as exc:
        return {"manager_id": args.manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": str(exc)}

    command_text = None
    if has_command:
        command_text, command_error = tmux_manager.command_text_from_source(args.command_text, args.command_file)
        if command_error:
            return {"manager_id": args.manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": command_error}
        if not tmux_state.one_line_text(command_text):
            return {"manager_id": args.manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": "command is blank"}

    paths = tmux_manager.manager_paths(args.workspace, args.state_dir)
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths["workspace"]
    current = current_info() if inside_tmux() else None
    codex_pane_id = None
    if notify.get("mode") == "tmux-inject":
        raw_codex_pane = tmux_state.one_line_text(notify.get("codex_pane"))
        if raw_codex_pane == "current":
            if not current or not tmux_state.one_line_text(current.get("pane_id")):
                return {
                    "manager_id": args.manager_id,
                    "job_id": job_id,
                    "started": False,
                    "status": "failed",
                    "reason": "manager start --notify tmux-inject --codex-pane current requires a current tmux pane",
                }
            codex_pane_id = str(current["pane_id"])
        else:
            pane = current_info(raw_codex_pane)
            if not pane or pane.get("pane_dead") or not tmux_state.one_line_text(pane.get("pane_id")):
                return {
                    "manager_id": args.manager_id,
                    "job_id": job_id,
                    "started": False,
                    "status": "failed",
                    "reason": f"manager start --notify tmux-inject could not resolve Codex pane: {raw_codex_pane}",
                }
            codex_pane_id = str(pane["pane_id"])
        pane_validation = tmux_manager.pane_codex_validation(codex_pane_id)
        if not pane_validation.get("safe"):
            return {
                "manager_id": args.manager_id,
                "job_id": job_id,
                "started": False,
                "status": "failed",
                "reason": str(pane_validation.get("reason") or "Codex pane validation failed"),
                "codex_pane_id": codex_pane_id,
                "pane_validation": pane_validation,
            }
        notify = dict(notify) | {"codex_pane_id": codex_pane_id}
    try:
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir, current)
    except Exception as exc:
        return {"manager_id": args.manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": str(exc)}
    process_mode = tmux_manager.manager_process_mode_value(getattr(args, "process_mode", "foreground"))
    dashboard_renderer = tmux_manager.manager_dashboard_renderer_value(getattr(args, "dashboard_renderer", "pane"))
    manager_launcher, manager_exit_watch = tmux_manager.manager_launcher_for_mode(process_mode)
    existing_record, record_error = tmux_manager.read_manager_record(paths, manager_id)
    if record_error:
        return {"manager_id": manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": record_error}
    if existing_record and notify.get("mode") == "tmux-inject":
        existing_codex_pane_id = tmux_state.one_line_text(existing_record.get("codex_pane_id"))
        if existing_codex_pane_id and codex_pane_id and existing_codex_pane_id != codex_pane_id:
            return {
                "manager_id": manager_id,
                "job_id": job_id,
                "started": False,
                "status": "failed",
                "reason": "manager tmux-inject notification is already bound to a different Codex pane",
                "codex_pane_id": existing_codex_pane_id,
                "requested_codex_pane_id": codex_pane_id,
            }
    if existing_record and existing_record.get("pending_job") and has_command:
        return {"manager_id": manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": "manager already has a pending job"}
    if existing_record and existing_record.get("status") == "running" and has_command:
        return {"manager_id": manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": "manager is already running a job"}
    if has_command:
        if existing_record:
            gate_record = dict(existing_record)
            gate_record.update({"notify": notify, "workspace": str(paths["workspace"]), "state_dir": str(paths["root"])})
            gate_record = tmux_manager.normalize_manager_record(gate_record, paths)
            allowed, gate_reason = tmux_manager.manager_queue_gate(gate_record)
        else:
            allowed = notify.get("mode") != "bridge"
            gate_reason = "bridge receipt is not verified: unverified" if not allowed else None
        if not allowed:
            return {"manager_id": manager_id, "job_id": job_id, "started": False, "status": "failed", "reason": gate_reason}
    existing_manager_alive = False
    if existing_record and existing_record.get("status") in {"starting", "idle", "queued", "running", "waiting_for_codex"}:
        existing_manager_alive = pid_is_running(parse_int(str(existing_record.get("manager_pid") or "")))

    if existing_manager_alive and existing_record:
        layout = layout_from_existing_manager_record(existing_record, cwd)
    else:
        layout = manager_layout(cwd, paths=paths, manager_id=manager_id, existing_record=existing_record)
    request_path = None
    pending_job = None
    if job_id and command_text is not None:
        request_path = tmux_manager.write_command_request(paths, manager_id, job_id, str(command_text))
        pending_job = tmux_manager.build_pending_job(
            job_id,
            request_path,
            str(cwd),
            layout.get("worker_pane_id"),
            layout.get("worker_pane_index"),
        )
    if existing_record:
        record = dict(existing_record)
        effective_pending_job = pending_job if pending_job is not None else record.get("pending_job")
        if effective_pending_job:
            next_status = "queued"
        elif record.get("current_job_id"):
            next_status = str(record.get("status") or "running")
        else:
            next_status = str(record.get("status") or "idle") if existing_manager_alive else "idle"
        record.update(
            {
                "status": next_status,
                "manager_pane_id": layout["manager_pane_id"],
                "manager_pane_index": layout.get("manager_pane_index"),
                "worker_pane_id": layout["worker_pane_id"],
                "worker_pane_index": layout.get("worker_pane_index"),
                "pending_job": effective_pending_job,
                "notify": notify,
                "codex_pane_id": codex_pane_id or record.get("codex_pane_id"),
                "workspace": str(paths["workspace"]),
                "state_dir": str(paths["root"]),
                "attach_command": layout.get("attach_command"),
                "poll_seconds": args.poll_seconds,
                "manager_process_mode": process_mode,
                "manager_launcher": manager_launcher,
                "manager_exit_watch": manager_exit_watch,
                "manager_dashboard_owner": "manager-loop",
                "dashboard_renderer": dashboard_renderer,
                "last_error": None,
                "log_max_bytes": getattr(args, "log_max_bytes", tmux_manager.DEFAULT_MANAGER_LOG_MAX_BYTES),
            }
        )
        if not existing_manager_alive:
            record["manager_pid"] = os.getpid()
            record["manager_process_started_at"] = tmux_state.utc_now()
        record = tmux_manager.normalize_manager_record(record, paths)
    else:
        record = tmux_manager.build_manager_record(
            manager_id=manager_id,
            manager_pane_id=layout["manager_pane_id"],
            worker_pane_id=layout["worker_pane_id"],
            manager_pane_index=layout.get("manager_pane_index"),
            worker_pane_index=layout.get("worker_pane_index"),
            pending_job=pending_job,
            notify=notify,
            codex_pane_id=codex_pane_id,
            workspace=str(paths["workspace"]),
            state_dir=str(paths["root"]),
            attach_command=layout.get("attach_command"),
            poll_seconds=args.poll_seconds,
            log_max_bytes=getattr(args, "log_max_bytes", tmux_manager.DEFAULT_MANAGER_LOG_MAX_BYTES),
            process_mode=process_mode,
            dashboard_renderer=dashboard_renderer,
        )
    record = tmux_manager.write_manager_record(paths, record)
    dashboard_path = Path(str(record["dashboard_path"]))
    tmux_manager.write_dashboard_file(dashboard_path, tmux_manager.dashboard_text(record))
    record, viewer_result = tmux_manager.ensure_dashboard_viewer(record, paths)
    if viewer_result.get("reason"):
        record["last_dashboard_viewer_error"] = viewer_result["reason"]
    record = tmux_manager.write_manager_record(paths, record)
    return {
        "manager_id": manager_id,
        "job_id": job_id,
        "started": True,
        "status": record["status"],
        "manager_path": record["manager_path"],
        "dashboard_path": record["dashboard_path"],
        "manager_process_mode": record.get("manager_process_mode") or "foreground",
        "dashboard_renderer": record.get("dashboard_renderer") or "pane",
        "dashboard_viewer_pid": record.get("dashboard_viewer_pid"),
        "dashboard_viewer_state_path": record.get("dashboard_viewer_state_path"),
        "dashboard_viewer_heartbeat_at": record.get("dashboard_viewer_heartbeat_at"),
        "dashboard_viewer": viewer_result,
        "codex_pane_id": record.get("codex_pane_id"),
        "start_process_mode": "existing" if existing_manager_alive else process_mode,
        "queued_on_existing_manager": existing_manager_alive,
        "command_request_path": str(request_path) if request_path else None,
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "record": record,
        **layout,
    }


def manager_submit(args: argparse.Namespace) -> dict[str, Any]:
    manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
    paths = tmux_manager.manager_paths(args.workspace, args.state_dir)
    record, error = tmux_manager.read_manager_record(paths, manager_id)
    if error:
        return {"manager_id": manager_id, "job_id": args.job_id, "queued": False, "reason": error}
    if record is None:
        return {"manager_id": manager_id, "job_id": args.job_id, "queued": False, "reason": "manager record not found"}

    if getattr(args, "new_worker", False):
        item_id = tmux_state.safe_id(args.job_id) if tmux_state.one_line_text(args.job_id) else ""
        if not item_id:
            return {"manager_id": manager_id, "job_id": args.job_id, "queued": False, "reason": "manager submit requires nonblank --job-id"}
        if record.get("pending_job"):
            return {"manager_id": manager_id, "job_id": item_id, "queued": False, "reason": "manager already has a pending job"}
        allowed, gate_reason = tmux_manager.manager_queue_gate(record)
        if not allowed:
            return {"manager_id": manager_id, "job_id": item_id, "queued": False, "reason": gate_reason}
        text, read_error = tmux_manager.command_text_from_source(args.command_text, args.command_file)
        if read_error:
            return {"manager_id": manager_id, "job_id": item_id, "queued": False, "reason": read_error}
        if not tmux_state.one_line_text(text):
            return {"manager_id": manager_id, "job_id": item_id, "queued": False, "reason": "command is blank"}

    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else paths["workspace"]
    pane_id = tmux_state.one_line_text(getattr(args, "pane", None))
    pane_index = None
    if pane_id:
        pane = current_info(pane_id)
        if not pane or pane.get("pane_dead"):
            return {"manager_id": manager_id, "job_id": args.job_id, "queued": False, "reason": f"pane not found: {pane_id}"}
        pane_index = pane_index_value(pane)
    elif getattr(args, "new_worker", False):
        current = current_info() if inside_tmux() else None
        if not current:
            anchor_id = tmux_state.one_line_text(record.get("worker_pane_id")) or tmux_state.one_line_text(record.get("manager_pane_id"))
            current = current_info(anchor_id) if anchor_id else None
        if not current:
            return {
                "manager_id": manager_id,
                "job_id": args.job_id,
                "queued": False,
                "reason": "manager submit --new-worker could not find an anchor pane",
            }
        original_pane_id = str(current.get("pane_id") or "")
        _session_name, _window_id, pane_id = split_pane(cwd, target=original_pane_id, vertical=False, percent=50, full_size=True)
        pane_index = pane_index_value(current_info(pane_id))
        if original_pane_id:
            run_tmux(["select-pane", "-t", original_pane_id], check=False)
    else:
        candidate_ids = list(record.get("worker_pane_ids") or [])
        if record.get("worker_pane_id"):
            candidate_ids.insert(0, str(record.get("worker_pane_id")))
        for candidate_id in tmux_manager.unique_text_values(candidate_ids):
            if tmux_manager.pane_has_active_job(record, candidate_id):
                continue
            pane = current_info(candidate_id)
            if pane and not pane.get("pane_dead") and pane_is_reusable_idle(candidate_id):
                pane_id = candidate_id
                pane_index = pane_index_value(pane)
                break
        if not pane_id:
            pane_id = tmux_state.one_line_text(record.get("worker_pane_id"))
            pane_index = tmux_state.one_line_text(record.get("worker_pane_index")) or None

    result = tmux_manager.queue_manager_job(
        manager_id=manager_id,
        job_id=args.job_id,
        command_text=args.command_text,
        command_file=args.command_file,
        workspace=args.workspace,
        state_dir=args.state_dir,
        cwd=args.cwd,
        pane_id=pane_id,
        pane_index=pane_index,
        allow_parallel=True,
    )
    result["pane_id"] = pane_id
    result["pane_index"] = pane_index
    return result


def manager(args: argparse.Namespace) -> dict[str, Any]:
    if args.manager_action == "start":
        return manager_start(args)
    if args.manager_action == "status":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        return tmux_manager.manager_status(manager_id, args.workspace, args.state_dir)
    if args.manager_action == "ps-poc":
        return tmux_manager.manager_ps_poc(args.workspace, args.state_dir)
    if args.manager_action == "bridge-check":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        return tmux_manager.bridge_check_manager(
            manager_id=manager_id,
            workspace=args.workspace,
            state_dir=args.state_dir,
            ack_timeout_seconds=args.ack_timeout_seconds,
        )
    if args.manager_action == "ack":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        return tmux_manager.ack_manager_event(
            manager_id=manager_id,
            event_id=args.event_id,
            workspace=args.workspace,
            state_dir=args.state_dir,
            turn_id=args.turn_id,
            note=args.note,
        )
    if args.manager_action == "run-next":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        return tmux_manager.queue_manager_job(
            manager_id=manager_id,
            job_id=args.job_id,
            command_text=args.command_text,
            command_file=args.command_file,
            workspace=args.workspace,
            state_dir=args.state_dir,
            cwd=args.cwd,
        )
    if args.manager_action == "submit":
        return manager_submit(args)
    if args.manager_action == "cancel":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        return tmux_manager.cancel_manager(
            manager_id,
            workspace=args.workspace,
            state_dir=args.state_dir,
            stop_worker=args.stop_worker,
            job_id=args.job_id,
            all_workers=args.all_workers,
        )
    if args.manager_action == "cleanup":
        manager_id = resolve_manager_id_arg(args.manager_id, args.workspace, args.state_dir)
        paths = tmux_manager.manager_paths(args.workspace, args.state_dir)
        record, error = tmux_manager.read_manager_record(paths, manager_id)
        if error:
            return {"manager_id": manager_id, "cleaned": False, "reason": error}
        if record is not None and not args.force:
            manager_pid = parse_int(str(record.get("manager_pid") or ""))
            if pid_is_running(manager_pid):
                return {
                    "manager_id": manager_id,
                    "cleaned": False,
                    "reason": "manager cleanup refuses live manager without --force; run manager cancel first",
                    "manager_pid": manager_pid,
                }
        return tmux_manager.cleanup_manager(
            manager_id,
            workspace=args.workspace,
            state_dir=args.state_dir,
            include_jobs=args.jobs,
            force=args.force,
        )
    die(f"unknown manager command: {args.manager_action}")


def monitor(args: argparse.Namespace) -> dict[str, Any]:
    if not tmux_state.one_line_text(getattr(args, "pane", None)):
        die("monitor requires nonblank --pane")
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    monitor_id = tmux_state.safe_id(f"monitor-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    script_dir = Path(__file__).resolve().parent
    status_lines = getattr(args, "status_lines", tmux_state.DEFAULT_STATUS_LINES)
    status_max_chars = getattr(args, "status_max_chars", tmux_state.DEFAULT_STATUS_MAX_CHARS)
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
        "--status-lines",
        str(status_lines),
        "--status-max-chars",
        str(status_max_chars),
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

    status_path = tmux_state.status_path(paths, monitor_id)
    log_path = tmux_state.log_path(paths, monitor_id)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=tmux_env())
    except Exception as exc:
        reason = f"monitor worker failed to start: {exc}"
        failed = tmux_state.build_status(
            kind="monitor",
            item_id=monitor_id,
            attempt=1,
            name=getattr(args, "name", None),
            status="failed",
            pane_id=args.pane,
            command_preview_text=args.match_regex or ("idle-shell" if args.idle_shell else "monitor"),
            cwd=str(paths["workspace"]),
            status_file=status_path,
            log_file=log_path,
            exit_code=1,
            last_output=reason,
        )
        failed.update({"status_lines": status_lines, "status_max_chars": status_max_chars})
        tmux_state.write_status(status_path, failed)
        return {
            "monitor_id": monitor_id,
            "started": False,
            "reason": reason,
            "pane_id": args.pane,
            "status_path": str(status_path),
            "log_path": str(log_path),
            "workspace": str(paths["workspace"]),
            "state_dir": str(paths["root"]),
        }
    return {
        "monitor_id": monitor_id,
        "started": True,
        "pid": proc.pid,
        "pane_id": args.pane,
        "status_path": str(status_path),
        "log_path": str(log_path),
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }


LOCK_TIMEOUT_SECONDS = 5.0
LOCK_STALE_SECONDS = 30.0
AUTOPILOT_LEASE_SECONDS = 30 * 60
AUTOPILOT_TICK_MAX_CHARS = tmux_state.DEFAULT_STATUS_MAX_CHARS
AUTOPILOT_EVIDENCE_MAX_CHARS = 8000
AUTOPILOT_POLICY = {
    "name": "bounded-repair",
    "allowed": [
        "inspect tmux status, logs, and workspace files",
        "edit workspace code or configuration",
        "run focused tests or diagnostics",
        "rerun the objective command",
    ],
    "blocked_without_user_approval": [
        "destructive cleanup",
        "force git operations",
        "push or deploy",
        "dependency installation",
        "secrets or authentication changes",
        "expanding to higher-cost or longer training",
    ],
}


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


def validate_row_specs(rows: list[str] | None, flag_name: str) -> None:
    for row in rows or []:
        if not tmux_state.one_line_text(row):
            die(f"{flag_name} is blank")


def validate_optional_status_file(args: argparse.Namespace, command_name: str) -> None:
    if getattr(args, "status_file", None) is not None and not tmux_state.one_line_text(args.status_file):
        die(f"{command_name} requires nonblank --status-file when provided")


def validate_managed_identity(args: argparse.Namespace) -> None:
    if not tmux_state.one_line_text(getattr(args, "job_id", None)):
        die("managed worker requires nonblank --job-id")
    if not tmux_state.one_line_text(getattr(args, "pane", None)):
        die("managed worker requires nonblank --pane")


def require_job_id(value: Any) -> str:
    if not tmux_state.one_line_text(value):
        die("job command requires nonblank --job-id")
    return tmux_state.safe_id(str(value))


def require_task_id(value: Any) -> str:
    if not tmux_state.one_line_text(value):
        die("task command requires nonblank --task-id")
    return tmux_state.safe_id(str(value))


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
def directory_lock(
    lock_dir: Path,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = LOCK_STALE_SECONDS,
    description: str = "lock",
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    while True:
        try:
            lock_dir.mkdir(parents=True)
            try:
                tmux_state.atomic_write_json(lock_dir / "owner.json", lock_metadata())
            except BaseException:
                shutil.rmtree(lock_dir, ignore_errors=True)
                raise
            acquired = True
            break
        except FileExistsError:
            if lock_dir_is_stale(lock_dir, stale_seconds):
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {description}: {lock_dir}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if acquired:
            shutil.rmtree(lock_dir, ignore_errors=True)


@contextlib.contextmanager
def registry_lock(paths: dict[str, Path], *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS, stale_seconds: float = LOCK_STALE_SECONDS) -> Any:
    with directory_lock(
        tmux_state.job_registry_lock_path(paths),
        timeout_seconds=timeout_seconds,
        stale_seconds=stale_seconds,
        description="managed job registry lock",
    ):
        yield


@contextlib.contextmanager
def task_lock(paths: dict[str, Path], task_id: str) -> Any:
    lock_dir = paths["tasks"] / f".{tmux_state.safe_id(task_id)}.lock"
    with directory_lock(lock_dir, description=f"task lock {tmux_state.safe_id(task_id)}"):
        yield


@contextlib.contextmanager
def objective_lock(paths: dict[str, Path], objective_id: str) -> Any:
    lock_dir = paths["objectives"] / f".{tmux_state.safe_id(objective_id)}.lock"
    with directory_lock(lock_dir, description=f"objective lock {tmux_state.safe_id(objective_id)}"):
        yield


def pid_is_running(pid: int | None) -> bool:
    return tmux_state.pid_is_running(pid)


def annotate_job_record(record: dict[str, Any]) -> dict[str, Any]:
    return tmux_state.managed_job_with_effective_state(record)


def load_job_records(paths: dict[str, Path], kind: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths["jobs"].glob("*.json")):
        data, error = tmux_state.read_json(path)
        if error or not data:
            records.append({"job_path": str(path), "error": error or "empty job record"})
            continue
        record = tmux_state.normalize_managed_job(data, path)
        if not job_kind_matches(str(record.get("kind") or ""), kind):
            continue
        records.append(annotate_job_record(record))
    records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return records


def managed_record_is_reclaimable(record: dict[str, Any]) -> bool:
    return tmux_state.is_active_managed_job(record) and str(record.get("effective_status") or "") in {"dead", "orphaned"}


def mark_managed_job_stale(
    paths: dict[str, Path],
    record: dict[str, Any],
    *,
    stale_reason: str,
    replaced_by: str | None = None,
) -> dict[str, Any] | None:
    job_id = str(record.get("job_id") or record.get("id") or "")
    if not job_id:
        return None
    now = tmux_state.utc_now()
    record_path = tmux_state.job_path(paths, job_id)
    stored, error = tmux_state.read_json(record_path)
    if error or not stored:
        return None
    stored = tmux_state.normalize_managed_job(stored, record_path)
    stored.update({"status": "stale", "updated_at": now, "heartbeat_at": now, "stale_reason": stale_reason})
    if replaced_by:
        stored["replaced_by"] = replaced_by
    tmux_state.strip_managed_transient_fields(stored)
    tmux_state.atomic_write_json(record_path, stored)

    status_path = tmux_state.status_path(paths, job_id)
    status, _status_error = tmux_state.read_json(status_path)
    if status:
        status = tmux_state.normalize_status(status, status_path)
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
    if replaced_by:
        status["replaced_by"] = replaced_by
    tmux_state.write_status(status_path, status)
    return stored


def reclaim_matching_inactive_jobs(paths: dict[str, Path], *, dedupe_key: str, replacement_job_id: str) -> list[dict[str, Any]]:
    reclaimed: list[dict[str, Any]] = []
    for record in load_job_records(paths):
        if record.get("dedupe_key") != dedupe_key:
            continue
        if not managed_record_is_reclaimable(record):
            continue
        reason = str(record.get("stale_reason") or f"reclaimed {record.get('effective_status')} managed job")
        marked = mark_managed_job_stale(paths, record, stale_reason=reason, replaced_by=replacement_job_id)
        if marked:
            reclaimed.append({"job_id": marked.get("job_id"), "stale_reason": reason})
    return reclaimed


def active_duplicate_record(paths: dict[str, Path], *, dedupe_key: str, item_id: str | None = None) -> dict[str, Any] | None:
    for record in load_job_records(paths):
        if record.get("dedupe_key") != dedupe_key:
            continue
        if item_id and record.get("job_id") == item_id:
            continue
        if tmux_state.is_verified_active_managed_job(record):
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


COMPACT_JOB_FIELDS = {
    "job_id",
    "id",
    "kind",
    "status",
    "effective_status",
    "process_state",
    "pid",
    "pid_running",
    "pid_matches",
    "pane_id",
    "dedupe_key",
    "created_at",
    "updated_at",
    "heartbeat_at",
    "stale_reason",
    "error",
    "job_path",
    "status_path",
    "log_path",
    "replaced_by",
    "pane_state",
}

COMPACT_STATUS_FIELDS = {
    "id",
    "job_id",
    "kind",
    "status",
    "exit_code",
    "pane_id",
    "updated_at",
    "ended_at",
    "heartbeat_at",
    "last_output",
    "stale_reason",
    "status_path",
    "log_path",
    "replaced_by",
}


def truncate_strings(value: Any, max_chars: int | None) -> Any:
    if max_chars is None:
        return value
    if isinstance(value, str):
        if max_chars <= 0:
            return ""
        return value if len(value) <= max_chars else value[: max_chars - 3] + "..." if max_chars > 3 else value[:max_chars]
    if isinstance(value, list):
        return [truncate_strings(item, max_chars) for item in value]
    if isinstance(value, dict):
        return {key: truncate_strings(item, max_chars) for key, item in value.items()}
    return value


def remove_observed_tail(value: Any) -> Any:
    if isinstance(value, list):
        return [remove_observed_tail(item) for item in value]
    if isinstance(value, dict):
        return {key: remove_observed_tail(item) for key, item in value.items() if key != "observed_status_tail"}
    return value


def compact_mapping(value: Any, fields: set[str]) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value[key] for key in sorted(fields) if key in value}


def compact_job_output(data: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = dict(data)
    if getattr(args, "compact", False):
        if isinstance(result.get("jobs"), list):
            result["jobs"] = [compact_mapping(job, COMPACT_JOB_FIELDS) for job in result["jobs"]]
        if isinstance(result.get("stale_jobs"), list):
            result["stale_jobs"] = [compact_mapping(job, COMPACT_JOB_FIELDS) for job in result["stale_jobs"]]
        if isinstance(result.get("record"), dict):
            result["record"] = compact_mapping(result["record"], COMPACT_JOB_FIELDS)
        if isinstance(result.get("status"), dict):
            result["status"] = compact_mapping(result["status"], COMPACT_STATUS_FIELDS)
    if getattr(args, "no_observed_tail", False):
        result = remove_observed_tail(result)
    return truncate_strings(result, getattr(args, "max_chars", None))


def pane_state_for(pane_id: Any) -> dict[str, Any]:
    pane_text = str(pane_id or "")
    if not pane_text:
        return {"pane_id": None, "pane_exists": False}
    info = current_info(pane_text)
    if not info:
        return {"pane_id": pane_text, "pane_exists": False}
    return {
        "pane_id": info.get("pane_id"),
        "pane_exists": True,
        "pane_dead": info.get("pane_dead"),
        "current_command": info.get("current_command"),
        "window_id": info.get("window_id"),
        "window_name": info.get("window_name"),
    }


def attach_pane_state(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return record
    updated = dict(record)
    updated["pane_state"] = pane_state_for(updated.get("pane_id"))
    return updated


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
        "kind": tmux_state.token_text(kind) or "job",
        "status": tmux_state.token_text(status) or "unknown",
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
    record["kind"] = tmux_state.token_text(kind) or "job"
    record["status"] = tmux_state.token_text(status) or "unknown"
    tmux_state.strip_managed_transient_fields(record)
    tmux_state.atomic_write_json(path, record)
    return record


def write_managed_start_failure(
    paths: dict[str, Path],
    *,
    job_id: str,
    kind: str,
    pane_id: str | None,
    name: str | None,
    reason: str,
    command_path_value: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged_extra = {**(extra or {}), "error": reason}
    record = write_managed_job_record(
        paths,
        job_id=job_id,
        kind=kind,
        pid=0,
        pane_id=pane_id,
        status="failed",
        command_path_value=command_path_value,
        extra=merged_extra,
    )
    status_path = tmux_state.status_path(paths, job_id)
    status = tmux_state.build_status(
        kind=kind,
        item_id=job_id,
        attempt=1,
        name=name,
        status="failed",
        pane_id=pane_id,
        command_preview_text=str(command_path_value or kind),
        cwd=str(paths["workspace"]),
        status_file=status_path,
        log_file=tmux_state.log_path(paths, job_id),
        exit_code=1,
        last_output=reason,
    )
    status.update(merged_extra)
    status["kind"] = tmux_state.token_text(kind) or "job"
    status["status"] = "failed"
    status["exit_code"] = 1
    status["last_output"] = reason
    tmux_state.write_status(status_path, status)
    return record


def command_text_for_worker(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if getattr(args, "command_text", None) is not None:
        command_text = str(args.command_text)
        if not tmux_state.one_line_text(command_text):
            raise ValueError("command is blank")
        return command_text, None
    command_file_arg = getattr(args, "command_file", None)
    if command_file_arg is not None:
        if not tmux_state.one_line_text(command_file_arg):
            raise ValueError("command file path is blank")
        command_file = Path(str(command_file_arg)).expanduser().resolve()
        command_text = command_file.read_text(encoding="utf-8")
        if not tmux_state.one_line_text(command_text):
            raise ValueError("command is blank")
        return command_text, str(command_file)
    return None, None


def check_interval_seconds(args: argparse.Namespace, worker_action: str) -> float:
    if worker_action == "watch":
        return float(getattr(args, "interval", 180.0))
    return float(getattr(args, "poll_seconds", 2.0))


def terminate_started_process(proc: Any, *, timeout_seconds: float = 5.0) -> None:
    try:
        if proc.poll() is not None:
            return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout_seconds)
        except Exception:
            pass
    except Exception:
        pass


def start_managed_worker(args: argparse.Namespace, worker_action: str, kind: str) -> dict[str, Any]:
    validate_managed_identity(args)
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = tmux_state.safe_id(args.job_id)
    record_path = tmux_state.job_path(paths, item_id)
    owner = owner_identity(args)
    interval = check_interval_seconds(args, worker_action)

    script_dir = Path(__file__).resolve().parent
    try:
        with registry_lock(paths):
            existing, _error = tmux_state.read_json(record_path)
            existing = annotate_job_record(tmux_state.normalize_managed_job(existing, record_path)) if existing else None
            existing_active = bool(existing and tmux_state.is_active_managed_job(existing) and not existing.get("stale"))
            if (
                existing
                and getattr(args, "replace", False)
                and tmux_state.is_active_managed_job(existing)
                and str(existing.get("process_state") or "") == "foreign_pid"
            ):
                return {
                    "job_id": item_id,
                    "started": False,
                    "reason": "existing pid is running but no longer looks like this tmux-skills worker",
                    "existing": existing,
                }

            try:
                command_text, source_command_path = command_text_for_worker(args)
            except Exception as exc:
                command_file_arg = getattr(args, "command_file", None)
                command_path_value = (
                    str(Path(str(command_file_arg)).expanduser())
                    if command_file_arg is not None and tmux_state.one_line_text(command_file_arg)
                    else None
                )
                if isinstance(exc, ValueError):
                    reason = str(exc)
                else:
                    reason = f"could not read managed worker command file: {exc}"
                if existing_active:
                    if not getattr(args, "replace", False):
                        return duplicate_result(
                            item_id,
                            dedupe_key=str(existing.get("dedupe_key") or ""),
                            existing=existing,
                            reason="managed job already appears active; use --replace or cancel it first",
                        )
                    return {
                        "job_id": item_id,
                        "kind": kind,
                        "started": False,
                        "duplicate": False,
                        "dedupe_key": str(existing.get("dedupe_key") or ""),
                        "pane_id": getattr(args, "pane", None),
                        "job_path": str(record_path),
                        "status_path": str(tmux_state.status_path(paths, item_id)),
                        "log_path": str(tmux_state.log_path(paths, item_id)),
                        "workspace": str(paths["workspace"]),
                        "state_dir": str(paths["root"]),
                        "reason": reason,
                        "existing": existing,
                    }
                record = write_managed_start_failure(
                    paths,
                    job_id=item_id,
                    kind=kind,
                    pane_id=getattr(args, "pane", None),
                    name=getattr(args, "name", None),
                    reason=reason,
                    command_path_value=command_path_value,
                )
                return {
                    "job_id": item_id,
                    "kind": kind,
                    "started": False,
                    "duplicate": False,
                    "dedupe_key": None,
                    "pane_id": getattr(args, "pane", None),
                    "job_path": str(record_path),
                    "status_path": str(tmux_state.status_path(paths, item_id)),
                    "log_path": str(tmux_state.log_path(paths, item_id)),
                    "workspace": str(paths["workspace"]),
                    "state_dir": str(paths["root"]),
                    "reason": reason,
                    "record": record,
                }

            payload = managed_dedupe_payload(paths, args, kind=kind, command_text=command_text)
            dedupe_key = managed_dedupe_key(payload)
            command_path_value: str | None = source_command_path

            if existing_active:
                if not getattr(args, "replace", False):
                    return duplicate_result(
                        item_id,
                        dedupe_key=dedupe_key,
                        existing=existing,
                        reason="managed job already appears active; use --replace or cancel it first",
                    )
                existing_pid = parse_int(str(existing.get("pid") or ""))
                if existing_pid and pid_is_running(existing_pid):
                    if not tmux_state.managed_worker_pid_matches(existing):
                        return {
                            "job_id": item_id,
                            "started": False,
                            "reason": "existing pid is running but no longer looks like this tmux-skills worker",
                            "existing": existing,
                        }
                    signal_sent = False
                    try:
                        os.kill(existing_pid, signal.SIGTERM)
                        signal_sent = True
                    except ProcessLookupError:
                        pass
                    except PermissionError as exc:
                        return {
                            "job_id": item_id,
                            "started": False,
                            "reason": f"could not signal existing managed job: {exc}",
                            "existing": existing,
                        }
                    if signal_sent:
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

            reclaimed = reclaim_matching_inactive_jobs(paths, dedupe_key=dedupe_key, replacement_job_id=item_id)
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
                try:
                    write_command_file(command_path, command_text)
                except Exception as exc:
                    reason = f"could not write managed worker command file: {exc}"
                    failure_record: dict[str, Any] | None = None
                    try:
                        failure_record = write_managed_start_failure(
                            paths,
                            job_id=item_id,
                            kind=kind,
                            pane_id=args.pane,
                            name=getattr(args, "name", None),
                            reason=reason,
                            command_path_value=str(command_path),
                        )
                    except Exception:
                        failure_record = None
                    return {
                        "job_id": item_id,
                        "kind": kind,
                        "started": False,
                        "duplicate": False,
                        "dedupe_key": dedupe_key,
                        "pane_id": args.pane,
                        "job_path": str(record_path),
                        "status_path": str(tmux_state.status_path(paths, item_id)),
                        "log_path": str(tmux_state.log_path(paths, item_id)),
                        "workspace": str(paths["workspace"]),
                        "state_dir": str(paths["root"]),
                        "reason": reason,
                        "record": failure_record,
                    }
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
                status_lines = getattr(args, "status_lines", tmux_state.DEFAULT_STATUS_LINES)
                status_max_chars = getattr(args, "status_max_chars", tmux_state.DEFAULT_STATUS_MAX_CHARS)
                argv.extend(
                    [
                        "--interval",
                        str(args.interval),
                        "--capture-lines",
                        str(args.capture_lines),
                        "--status-lines",
                        str(status_lines),
                        "--status-max-chars",
                        str(status_max_chars),
                    ]
                )
                if args.status_file:
                    argv.extend(["--status-file", args.status_file])
                if getattr(args, "low_token", False):
                    argv.append("--low-token")
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
                    if getattr(args, "low_token", False):
                        argv.append("--low-token")
            if args.timeout_seconds is not None:
                argv.extend(["--timeout-seconds", str(args.timeout_seconds)])

            record_extra = {
                "argv": argv,
                "dedupe_key": dedupe_key,
                "dedupe_payload": payload,
                "owner": owner,
                "check_interval_seconds": interval,
            }
            if getattr(args, "low_token", False):
                record_extra["low_token"] = True
            if duplicate_allowed and duplicate_of:
                record_extra.update({"duplicate_allowed": True, "duplicate_of": duplicate_of})
            try:
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
            except Exception as exc:
                reason = f"managed worker state update failed before start: {exc}"
                return {
                    "job_id": item_id,
                    "kind": kind,
                    "started": False,
                    "duplicate": False,
                    "dedupe_key": dedupe_key,
                    "pane_id": args.pane,
                    "job_path": str(record_path),
                    "status_path": str(tmux_state.status_path(paths, item_id)),
                    "log_path": str(tmux_state.log_path(paths, item_id)),
                    "workspace": str(paths["workspace"]),
                    "state_dir": str(paths["root"]),
                    "reason": reason,
                    "record": None,
                }
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=tmux_env())
            except Exception as exc:
                reason = f"managed worker failed to start: {exc}"
                record: dict[str, Any] | None = None
                try:
                    record = write_managed_start_failure(
                        paths,
                        job_id=item_id,
                        kind=kind,
                        pane_id=args.pane,
                        name=getattr(args, "name", None),
                        reason=reason,
                        command_path_value=command_path_value,
                        extra=record_extra,
                    )
                except Exception:
                    try:
                        record_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                return {
                    "job_id": item_id,
                    "kind": kind,
                    "started": False,
                    "duplicate": False,
                    "dedupe_key": dedupe_key,
                    "pane_id": args.pane,
                    "job_path": str(record_path),
                    "status_path": str(tmux_state.status_path(paths, item_id)),
                    "log_path": str(tmux_state.log_path(paths, item_id)),
                    "workspace": str(paths["workspace"]),
                    "state_dir": str(paths["root"]),
                    "reason": reason,
                    "record": record,
                }
            status = "running" if kind == "watch" else "waiting"
            try:
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
            except Exception as exc:
                terminate_started_process(proc)
                reason = f"managed worker state update failed after start: {exc}"
                failure_record: dict[str, Any] | None = None
                try:
                    failure_record = write_managed_start_failure(
                        paths,
                        job_id=item_id,
                        kind=kind,
                        pane_id=args.pane,
                        name=getattr(args, "name", None),
                        reason=reason,
                        command_path_value=command_path_value,
                        extra=record_extra,
                    )
                except Exception:
                    failure_record = None
                    try:
                        record_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                return {
                    "job_id": item_id,
                    "kind": kind,
                    "started": False,
                    "duplicate": False,
                    "dedupe_key": dedupe_key,
                    "pane_id": args.pane,
                    "job_path": str(record_path),
                    "status_path": str(tmux_state.status_path(paths, item_id)),
                    "log_path": str(tmux_state.log_path(paths, item_id)),
                    "workspace": str(paths["workspace"]),
                    "state_dir": str(paths["root"]),
                    "reason": reason,
                    "record": failure_record,
                }
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
        "reclaimed": reclaimed,
        "record": record,
    }


def job_list(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    result = {"jobs": load_job_records(paths, kind), "workspace": str(paths["workspace"]), "state_dir": str(paths["root"])}
    return compact_job_output(result, args)


def job_kind_matches(actual: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    actual_kind = tmux_state.token_text(actual)
    expected_kind = tmux_state.token_text(expected)
    return actual_kind == expected_kind or actual_kind.startswith(f"{expected_kind}-")


def job_status(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_job_id(args.job_id)
    record_path = tmux_state.job_path(paths, item_id)
    status_path = tmux_state.status_path(paths, item_id)
    record, record_error = tmux_state.read_json(record_path)
    if record:
        record = annotate_job_record(tmux_state.normalize_managed_job(record, record_path))
    status, status_error = tmux_state.read_json(status_path)
    if status:
        status = tmux_state.normalize_status(status, status_path)
    if record and not job_kind_matches(str(record.get("kind") or ""), kind):
        result = {"job_id": item_id, "found": False, "reason": f"job is not a {kind} job", "record": record}
        return compact_job_output(result, args)
    if not record and status and not job_kind_matches(str(status.get("kind") or ""), kind):
        result = {"job_id": item_id, "found": False, "reason": f"status is not a {kind} job", "status": status}
        return compact_job_output(result, args)
    if getattr(args, "include_pane_state", False):
        record = attach_pane_state(record)
    result = {
        "job_id": item_id,
        "found": bool(record or status),
        "record": record,
        "record_error": record_error,
        "status": status,
        "status_error": status_error,
        "pid_running": bool((record or {}).get("pid_running")),
    }
    return compact_job_output(result, args)


def job_cancel(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_job_id(args.job_id)
    record_path = tmux_state.job_path(paths, item_id)
    record, error = tmux_state.read_json(record_path)
    if error or not record:
        return {"job_id": item_id, "cancelled": False, "reason": error or "job record not found"}
    record = tmux_state.normalize_managed_job(record, record_path)
    record_kind = tmux_state.token_text(record.get("kind")) or "job"
    if not job_kind_matches(record_kind, kind):
        return {"job_id": item_id, "cancelled": False, "reason": f"job is not a {kind} job", "record": record}

    record_status = tmux_state.token_text(record.get("status"))
    if record_status in tmux_state.TERMINAL_STATUSES:
        if getattr(args, "include_pane_state", False):
            record = attach_pane_state(record)
        result = {"job_id": item_id, "cancelled": False, "reason": f"job already {record_status}", "record": record}
        return compact_job_output(result, args)
    pid = parse_int(str(record.get("pid") or ""))
    running = pid_is_running(pid)
    if running and pid:
        if not tmux_state.managed_worker_pid_matches(record):
            if getattr(args, "include_pane_state", False):
                record = attach_pane_state(record)
            result = {
                "job_id": item_id,
                "cancelled": False,
                "reason": "recorded pid is running but no longer looks like this tmux-skills worker",
                "record": record,
            }
            return compact_job_output(result, args)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            running = False
        except PermissionError as exc:
            if getattr(args, "include_pane_state", False):
                record = attach_pane_state(record)
            result = {
                "job_id": item_id,
                "cancelled": False,
                "reason": f"could not signal recorded pid: {exc}",
                "record": record,
            }
            return compact_job_output(result, args)

    now = tmux_state.utc_now()
    record.update({"status": "cancelled", "updated_at": now, "heartbeat_at": now})
    tmux_state.strip_managed_transient_fields(record)
    tmux_state.atomic_write_json(record_path, record)

    status_file = tmux_state.status_path(paths, item_id)
    status, _status_error = tmux_state.read_json(status_file)
    if status:
        status = tmux_state.normalize_status(status, status_file)
        status.update(
            {
                "status": "cancelled",
                "exit_code": 1,
                "updated_at": now,
                "ended_at": now,
                "last_output": "cancelled by tmux_control.py",
            }
        )
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
    status = tmux_state.write_status(status_file, status)
    if getattr(args, "include_pane_state", False):
        record = attach_pane_state(record)
    result = {"job_id": item_id, "cancelled": True, "signal_sent": running, "record": record, "status": status}
    return compact_job_output(result, args)


def job_gc(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    if not args.stale:
        die("job gc requires --stale")

    stale_records = [
        record
        for record in load_job_records(paths, kind)
        if tmux_state.is_active_managed_job(record) and record.get("stale")
    ]
    result: dict[str, Any] = {
        "dry_run": bool(args.dry_run),
        "stale_jobs": stale_records,
        "marked": [],
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }
    if getattr(args, "include_pane_state", False):
        result["stale_jobs"] = [attach_pane_state(record) for record in result["stale_jobs"]]
    if args.dry_run:
        return compact_job_output(result, args)

    for record in stale_records:
        stale_reason = str(record.get("stale_reason") or "stale managed job")
        marked = mark_managed_job_stale(paths, record, stale_reason=stale_reason)
        if marked:
            result["marked"].append({"job_id": marked.get("job_id"), "stale_reason": stale_reason})
    return compact_job_output(result, args)


def watch(args: argparse.Namespace) -> dict[str, Any]:
    if args.watch_action == "list":
        return job_list(args, "watch")
    if args.watch_action == "status":
        return job_status(args, "watch")
    if args.watch_action == "cancel":
        return job_cancel(args, "watch")
    if args.watch_action == "gc":
        return job_gc(args, "watch")
    if not args.job_id or not args.pane:
        die("watch start requires --job-id and --pane")
    validate_managed_identity(args)
    validate_optional_status_file(args, "watch")
    if getattr(args, "low_token", False) and not tmux_state.one_line_text(getattr(args, "status_file", None)):
        die("watch --low-token requires --status-file")
    return start_managed_worker(args, "watch", "watch")


def queue_after_idle(args: argparse.Namespace) -> dict[str, Any]:
    validate_managed_identity(args)
    return start_managed_worker(args, "queue-after-idle", "queue-after-idle")


def queue_after_status(args: argparse.Namespace) -> dict[str, Any]:
    validate_managed_identity(args)
    if not tmux_state.one_line_text(getattr(args, "status_file", None)):
        die("queue-after-status requires nonblank --status-file")
    validate_row_specs(getattr(args, "require_row", None), "--require-row")
    validate_row_specs(getattr(args, "fail_row", None), "--fail-row")
    if not canonical_row_specs(getattr(args, "require_row", None)):
        die("queue-after-status requires at least one --require-row")
    return start_managed_worker(args, "queue-after-status", "queue-after-status")


def read_text_arg(text: str | None, path: str | None) -> str | None:
    if text is not None and path is not None:
        raise ValueError("provide only one of text or path")
    if path is not None:
        if not tmux_state.one_line_text(path):
            raise ValueError("file path is blank")
        return Path(path).expanduser().read_text(encoding="utf-8")
    return text


def require_objective_id(objective_id: str) -> str:
    if not tmux_state.one_line_text(objective_id):
        die("autopilot requires nonblank --objective-id")
    item_id = tmux_state.safe_id(objective_id)
    if item_id != objective_id:
        die("autopilot --objective-id must contain only letters, numbers, '.', '_', or '-'")
    return item_id


def objective_attempt_job_id(objective_id: str, attempt_index: int) -> str:
    return f"{objective_id}-attempt-{attempt_index}"


def objective_attempt_record(paths: dict[str, Path], job_id: str, attempt_index: int) -> dict[str, Any]:
    return {
        "attempt": attempt_index,
        "job_id": job_id,
        "status_path": str(tmux_state.status_path(paths, job_id)),
        "log_path": str(tmux_state.log_path(paths, job_id)),
        "started_at": tmux_state.utc_now(),
    }


def read_command_snapshot(command_text: str | None, command_file: str | None) -> str:
    snapshot = read_text_arg(command_text, command_file)
    if snapshot is None or not tmux_state.one_line_text(snapshot):
        raise ValueError("autopilot command is blank")
    return snapshot


def load_objective(paths: dict[str, Path], objective_id: str) -> dict[str, Any]:
    item_id = require_objective_id(objective_id)
    path = tmux_state.objective_path(paths, item_id)
    objective, error = tmux_state.read_json(path)
    if error or not objective:
        die(f"could not load objective {item_id}: {error or 'not found'}")
    return tmux_state.normalize_objective(objective, path)


def current_attempt(objective: dict[str, Any]) -> dict[str, Any] | None:
    current = objective.get("current_attempt")
    if isinstance(current, dict):
        return current
    attempts = objective.get("attempts")
    if isinstance(attempts, list) and attempts:
        last = attempts[-1]
        return last if isinstance(last, dict) else None
    return None


def attempt_status(paths: dict[str, Path], attempt: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if not attempt:
        return None, None
    job_id = str(attempt.get("job_id") or "")
    if not job_id:
        return None, "attempt has no job_id"
    status_path = tmux_state.status_path(paths, job_id)
    status, error = tmux_state.read_json(status_path)
    if error:
        return None, error
    if status:
        return tmux_state.normalize_status(status, status_path), None
    return None, None


def objective_evidence(paths: dict[str, Path], objective: dict[str, Any], status: dict[str, Any] | None = None) -> list[str]:
    evidence: list[str] = []
    attempt = current_attempt(objective)
    for value in (
        (attempt or {}).get("status_path"),
        (attempt or {}).get("log_path"),
        (status or {}).get("status_path"),
        (status or {}).get("log_path"),
        objective.get("objective_path"),
    ):
        if value and value not in evidence:
            evidence.append(str(value))
    return evidence


def bounded_tail_payload(text: Any, max_chars: int) -> dict[str, Any]:
    value = "" if text is None else str(text)
    if max_chars <= 0:
        return {
            "content": "",
            "content_omitted": bool(value),
            "truncated": bool(value),
            "total_chars_known": len(value),
            "max_chars": max_chars,
        }
    if len(value) <= max_chars:
        return {
            "content": value,
            "content_omitted": False,
            "truncated": False,
            "total_chars_known": len(value),
            "max_chars": max_chars,
        }
    return {
        "content": value[-max_chars:],
        "content_omitted": False,
        "truncated": True,
        "total_chars_known": len(value),
        "max_chars": max_chars,
    }


def evidence_commands(paths: dict[str, Path], objective_id: Any, *, max_chars: int = AUTOPILOT_EVIDENCE_MAX_CHARS) -> list[dict[str, str]]:
    item_id = str(objective_id)
    state_args = f"--workspace {shlex.quote(str(paths['workspace']))} --state-dir {shlex.quote(str(paths['root']))}"
    return [
        {
            "kind": "status",
            "command": (
                f"python scripts/tmux_control.py autopilot evidence --objective-id {shlex.quote(item_id)} "
                f"--kind status --max-chars {max_chars} {state_args}"
            ),
        },
        {
            "kind": "log",
            "command": (
                f"python scripts/tmux_control.py autopilot evidence --objective-id {shlex.quote(item_id)} "
                f"--kind log --max-chars {max_chars} {state_args}"
            ),
        },
    ]


def status_core(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if not status:
        return None
    fields = ("status", "exit_code", "pane_id", "cwd", "command_preview", "status_path", "log_path")
    return {field: status.get(field) for field in fields if field in status}


def attempt_summary(
    paths: dict[str, Path],
    objective: dict[str, Any],
    status: dict[str, Any] | None,
    *,
    max_chars: int,
) -> dict[str, Any]:
    attempt = current_attempt(objective)
    payload = bounded_tail_payload((status or {}).get("last_output") if status else "", max_chars)
    attempt_status = (status or {}).get("status")
    return {
        "objective_status": objective.get("status"),
        "attempt_index": (attempt or {}).get("attempt"),
        "attempt_job_id": (attempt or {}).get("job_id"),
        "attempt_status": attempt_status,
        "terminal": bool(status and tmux_state.is_terminal(status)),
        "exit_code": (status or {}).get("exit_code"),
        "command_preview": (status or {}).get("command_preview") or tmux_state.command_preview(str(objective.get("command_snapshot") or "")),
        "status_path": (attempt or {}).get("status_path") or (status or {}).get("status_path"),
        "log_path": (attempt or {}).get("log_path") or (status or {}).get("log_path"),
        "last_output_tail": payload["content"],
        "content_omitted": payload["content_omitted"],
        "truncated": payload["truncated"],
        "total_chars_known": payload["total_chars_known"] if status else None,
        "max_chars": payload["max_chars"],
        "source": "status.last_output" if status else "none",
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
    }


def objective_attempt_by_selector(objective: dict[str, Any], selector: str) -> dict[str, Any]:
    if selector == "current":
        attempt = current_attempt(objective)
        if attempt:
            return attempt
        die("autopilot evidence objective has no current attempt")
    try:
        attempt_index = int(selector)
    except (TypeError, ValueError):
        die("autopilot evidence --attempt must be 'current' or a positive attempt number")
    if attempt_index < 1:
        die("autopilot evidence --attempt must be positive")
    for attempt in objective.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        try:
            stored_attempt = int(attempt.get("attempt") or 0)
        except (TypeError, ValueError):
            continue
        if stored_attempt == attempt_index:
            return attempt
    die(f"autopilot evidence attempt not found: {attempt_index}")
    raise AssertionError("unreachable")


def evidence_file_payload(path: Path, *, max_chars: int, missing_label: str) -> dict[str, Any]:
    exists = path.exists()
    if not exists:
        return {
            "path": str(path),
            "exists": False,
            "readable": False,
            "error": f"missing {missing_label}",
            "content": "",
            "content_omitted": False,
            "truncated": False,
            "total_chars_known": None,
            "max_chars": max_chars,
        }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": str(path),
            "exists": True,
            "readable": False,
            "error": str(exc),
            "content": "",
            "content_omitted": False,
            "truncated": False,
            "total_chars_known": None,
            "max_chars": max_chars,
        }
    payload = bounded_tail_payload(text, max_chars)
    return {
        "path": str(path),
        "exists": True,
        "readable": True,
        "error": None,
        **payload,
    }


def lease_expired(lease: dict[str, Any] | None) -> bool:
    if not lease:
        return True
    expires_at = lease.get("expires_at")
    age = tmux_state.age_seconds(expires_at)
    return age is None or age >= 0


def build_repair_lease(attempt: dict[str, Any]) -> dict[str, Any]:
    now = tmux_state.utc_now()
    expires = datetime.now(timezone.utc) + timedelta(seconds=AUTOPILOT_LEASE_SECONDS)
    return {
        "owner": owner_identity(),
        "claimed_at": now,
        "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "attempt_job_id": attempt.get("job_id"),
    }


def autopilot_tick_output(
    paths: dict[str, Path],
    objective: dict[str, Any],
    *,
    action: str,
    reason: str,
    status: dict[str, Any] | None = None,
    status_error: str | None = None,
    max_chars: int = AUTOPILOT_TICK_MAX_CHARS,
) -> dict[str, Any]:
    attempt = current_attempt(objective)
    state_args = f"--workspace {shlex.quote(str(paths['workspace']))} --state-dir {shlex.quote(str(paths['root']))}"
    commands = [
        f"python scripts/tmux_control.py autopilot status --objective-id {shlex.quote(str(objective.get('objective_id')))} {state_args}",
    ]
    if action in {"repair", "rerun_failed"}:
        commands.append(
            f"python scripts/tmux_control.py autopilot rerun --objective-id {shlex.quote(str(objective.get('objective_id')))} {state_args}"
        )
        commands.append(
            f"python scripts/tmux_control.py autopilot block --objective-id {shlex.quote(str(objective.get('objective_id')))} --reason TEXT {state_args}"
        )
    result = {
        "objective_id": objective.get("objective_id"),
        "status": objective.get("status"),
        "action": action,
        "reason": reason,
        "attempt_job_id": (attempt or {}).get("job_id"),
        "attempt_status": (status or {}).get("status"),
        "attempt_index": (attempt or {}).get("attempt"),
        "max_attempts": objective.get("max_attempts"),
        "evidence_paths": objective_evidence(paths, objective, status),
        "policy": objective.get("policy") or AUTOPILOT_POLICY,
        "lease": objective.get("lease"),
        "commands": commands,
        "status_error": status_error,
        "workspace": str(paths["workspace"]),
        "state_dir": str(paths["root"]),
        "attempt_summary": attempt_summary(paths, objective, status, max_chars=max_chars),
    }
    if action in {"repair", "rerun_failed", "blocked"}:
        result["evidence_commands"] = evidence_commands(paths, objective.get("objective_id"))
    if action in {"repair", "rerun_failed"}:
        result["agent_instruction"] = (
            "Use attempt_summary first. If the failure cause is unclear, run the status evidence command, "
            "then the log evidence command with bounded --max-chars. Make only bounded workspace repairs, "
            "run focused verification, then use the rerun command. Block instead of acting if the repair requires a blocked policy action."
        )
    elif action == "no_action":
        result["agent_instruction"] = "Report the current state briefly, do not open evidence files, and do not modify files."
    elif action == "blocked":
        result["agent_instruction"] = (
            "Report the blocked objective state. Use evidence_commands only if attempt_summary is insufficient to explain the block; "
            "remind the user the heartbeat can be paused or removed."
        )
    elif action in {"completed", "cancelled"}:
        result["agent_instruction"] = "Report the terminal objective state and remind the user the heartbeat can be paused or removed."
    return result


def start_objective_attempt(
    args: argparse.Namespace,
    paths: dict[str, Path],
    objective: dict[str, Any],
    *,
    attempt_index: int,
    command_snapshot: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    job_id = objective_attempt_job_id(str(objective["objective_id"]), attempt_index)
    run_args = argparse.Namespace(
        pane=objective["pane_id"],
        command_text=command_snapshot,
        command_file=None,
        job_id=job_id,
        name=f"autopilot:{objective['objective_id']}",
        cwd=objective.get("cwd") or str(paths["workspace"]),
        workspace=str(paths["workspace"]),
        state_dir=str(paths["root"]),
        require_idle_shell=getattr(args, "require_idle_shell", False),
        next_instruction=None,
        next_instruction_file=None,
        next_on="terminal",
    )
    result = run_job(run_args)
    attempt = objective_attempt_record(paths, job_id, attempt_index)
    attempt["sent"] = bool(result.get("sent"))
    if result.get("reason"):
        attempt["reason"] = result.get("reason")
    attempts = list(objective.get("attempts") or [])
    attempts.append(attempt)
    objective["attempts"] = attempts
    objective["current_attempt"] = attempt
    objective["status"] = "active"
    objective["lease"] = None
    objective["completed_at"] = None
    objective["blocked_reason"] = None
    if command_snapshot != objective.get("command_snapshot"):
        objective["command_snapshot"] = command_snapshot
    return tmux_state.write_objective(paths, objective), result


def autopilot_start(args: argparse.Namespace) -> dict[str, Any]:
    item_id = require_objective_id(args.objective_id)
    if not tmux_state.one_line_text(args.goal):
        die("autopilot start requires nonblank --goal")
    if not tmux_state.one_line_text(args.pane):
        die("autopilot start requires nonblank --pane")
    if args.max_attempts < 1:
        die("autopilot --max-attempts must be positive")
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    try:
        command_snapshot = read_command_snapshot(args.command_text, args.command_file)
    except Exception as exc:
        die(str(exc))
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else str(paths["workspace"])
    with objective_lock(paths, item_id):
        path = tmux_state.objective_path(paths, item_id)
        if path.exists():
            die(f"objective already exists: {item_id}")
        objective = tmux_state.build_objective(
            objective_id=item_id,
            goal=args.goal,
            pane_id=args.pane,
            cwd=cwd,
            command_snapshot=command_snapshot,
            max_attempts=args.max_attempts,
            policy=AUTOPILOT_POLICY,
        )
        objective = tmux_state.write_objective(paths, objective)
        objective, run_result = start_objective_attempt(args, paths, objective, attempt_index=1, command_snapshot=command_snapshot)
    return {
        "objective_id": item_id,
        "started": bool(run_result.get("sent")),
        "objective": objective,
        "run": run_result,
        "heartbeat_prompt_command": (
            f"python scripts/tmux_control.py autopilot heartbeat-prompt --objective-id {shlex.quote(item_id)} "
            f"--workspace {shlex.quote(str(paths['workspace']))} --state-dir {shlex.quote(str(paths['root']))}"
        ),
    }


def autopilot_tick(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_objective_id(args.objective_id)
    max_chars = int(getattr(args, "max_chars", AUTOPILOT_TICK_MAX_CHARS))
    with objective_lock(paths, item_id):
        objective = load_objective(paths, item_id)
        if objective["status"] in {"succeeded", "blocked", "cancelled"}:
            if objective["status"] == "succeeded":
                action = "completed"
            elif objective["status"] == "cancelled":
                action = "cancelled"
            else:
                action = "blocked"
            status, status_error = attempt_status(paths, current_attempt(objective))
            return autopilot_tick_output(
                paths,
                objective,
                action=action,
                reason=f"objective is {objective['status']}",
                status=status,
                status_error=status_error,
                max_chars=max_chars,
            )
        if objective["status"] == "repairing" and not lease_expired(objective.get("lease")):
            status, status_error = attempt_status(paths, current_attempt(objective))
            return autopilot_tick_output(
                paths,
                objective,
                action="no_action",
                reason="repair already claimed",
                status=status,
                status_error=status_error,
                max_chars=max_chars,
            )
        attempt = current_attempt(objective)
        status, status_error = attempt_status(paths, attempt)
        attempt_state = tmux_state.token_text((status or {}).get("status"))
        if status_error:
            return autopilot_tick_output(paths, objective, action="no_action", reason=f"could not read attempt status: {status_error}", status_error=status_error, max_chars=max_chars)
        if not attempt or not attempt_state:
            return autopilot_tick_output(paths, objective, action="no_action", reason="attempt status is not available yet", status=status, max_chars=max_chars)
        if attempt_state in tmux_state.RUNNING_STATUSES:
            return autopilot_tick_output(paths, objective, action="no_action", reason=f"attempt is {attempt_state}", status=status, max_chars=max_chars)
        if attempt_state == "succeeded":
            objective["status"] = "succeeded"
            objective["completed_at"] = tmux_state.utc_now()
            objective["lease"] = None
            objective = tmux_state.write_objective(paths, objective)
            return autopilot_tick_output(paths, objective, action="completed", reason="attempt succeeded", status=status, max_chars=max_chars)
        if attempt_state in {"failed", "stopped", "timeout", "cancelled", "stale"}:
            if len(objective.get("attempts") or []) >= int(objective.get("max_attempts") or 1):
                objective["status"] = "blocked"
                objective["blocked_reason"] = "maximum attempts reached"
                objective["lease"] = None
                objective = tmux_state.write_objective(paths, objective)
                return autopilot_tick_output(paths, objective, action="blocked", reason="maximum attempts reached", status=status, max_chars=max_chars)
            objective["status"] = "repairing"
            objective["lease"] = build_repair_lease(attempt)
            objective = tmux_state.write_objective(paths, objective)
            return autopilot_tick_output(paths, objective, action="repair", reason=f"attempt ended with {attempt_state}", status=status, max_chars=max_chars)
        return autopilot_tick_output(paths, objective, action="no_action", reason=f"attempt status is {attempt_state}", status=status, max_chars=max_chars)


def autopilot_rerun(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_objective_id(args.objective_id)
    with objective_lock(paths, item_id):
        objective = load_objective(paths, item_id)
        if objective["status"] != "repairing":
            die(f"objective is not repairing: {objective['status']}")
        attempts = list(objective.get("attempts") or [])
        if len(attempts) >= int(objective.get("max_attempts") or 1):
            objective["status"] = "blocked"
            objective["blocked_reason"] = "maximum attempts reached"
            objective["lease"] = None
            objective = tmux_state.write_objective(paths, objective)
            return autopilot_tick_output(paths, objective, action="blocked", reason="maximum attempts reached")
        try:
            command_snapshot = read_command_snapshot(args.command_text, args.command_file) if (args.command_text is not None or args.command_file is not None) else str(objective.get("command_snapshot") or "")
        except Exception as exc:
            die(str(exc))
        if not tmux_state.one_line_text(command_snapshot):
            die("autopilot rerun has no command snapshot")
        objective, run_result = start_objective_attempt(
            args,
            paths,
            objective,
            attempt_index=len(attempts) + 1,
            command_snapshot=command_snapshot,
        )
        if not run_result.get("sent"):
            attempts_after = list(objective.get("attempts") or [])
            attempt = current_attempt(objective)
            reason = f"rerun command was not sent to pane: {run_result.get('reason') or 'unknown reason'}"
            status, _status_error = attempt_status(paths, attempt)
            if len(attempts_after) >= int(objective.get("max_attempts") or 1):
                objective["status"] = "blocked"
                objective["blocked_reason"] = f"{reason}; maximum attempts reached"
                objective["lease"] = None
                objective = tmux_state.write_objective(paths, objective)
                result = autopilot_tick_output(paths, objective, action="blocked", reason=objective["blocked_reason"], status=status)
                result["objective"] = objective
                result["run"] = run_result
                return result
            objective["status"] = "repairing"
            objective["lease"] = build_repair_lease(attempt or {})
            objective = tmux_state.write_objective(paths, objective)
            result = autopilot_tick_output(paths, objective, action="rerun_failed", reason=reason, status=status)
            result["objective"] = objective
            result["run"] = run_result
            return result
    return {
        "objective_id": item_id,
        "action": "rerun_started",
        "objective": objective,
        "run": run_result,
    }


def autopilot_status(args: argparse.Namespace) -> Any:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    if args.autopilot_action == "list":
        objectives, errors = tmux_state.load_objectives(paths["root"])
        return {"objectives": objectives, "errors": errors, "workspace": str(paths["workspace"]), "state_dir": str(paths["root"])}
    return load_objective(paths, args.objective_id)


def autopilot_evidence(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_objective_id(args.objective_id)
    max_chars = int(getattr(args, "max_chars", AUTOPILOT_EVIDENCE_MAX_CHARS))
    with objective_lock(paths, item_id):
        objective = load_objective(paths, item_id)
        attempt = objective_attempt_by_selector(objective, str(getattr(args, "attempt", "current") or "current"))
        job_id = str(attempt.get("job_id") or "")
        if not job_id:
            die("autopilot evidence attempt has no job_id")
        if args.kind == "status":
            path = tmux_state.status_path(paths, job_id)
            exists = path.exists()
            data, error = tmux_state.read_json(path)
            if error or not data:
                return {
                    "objective_id": item_id,
                    "kind": "status",
                    "attempt": attempt.get("attempt"),
                    "attempt_job_id": job_id,
                    "path": str(path),
                    "exists": exists,
                    "readable": False,
                    "error": error or "missing status file",
                    "max_chars": max_chars,
                    "content": "",
                    "content_omitted": False,
                    "truncated": False,
                    "total_chars_known": None,
                    "status_core": None,
                }
            status = tmux_state.normalize_status(data, path)
            payload = bounded_tail_payload(status.get("last_output"), max_chars)
            return {
                "objective_id": item_id,
                "kind": "status",
                "attempt": attempt.get("attempt"),
                "attempt_job_id": job_id,
                "path": str(path),
                "exists": True,
                "readable": True,
                "error": None,
                "max_chars": max_chars,
                "content": payload["content"],
                "content_omitted": payload["content_omitted"],
                "truncated": payload["truncated"],
                "total_chars_known": payload["total_chars_known"],
                "status_core": status_core(status),
            }
        path = tmux_state.log_path(paths, job_id)
        payload = evidence_file_payload(path, max_chars=max_chars, missing_label="log file")
        return {
            "objective_id": item_id,
            "kind": "log",
            "attempt": attempt.get("attempt"),
            "attempt_job_id": job_id,
            **payload,
            "status_core": None,
        }


def autopilot_finish(args: argparse.Namespace, status: str) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_objective_id(args.objective_id)
    with objective_lock(paths, item_id):
        objective = load_objective(paths, item_id)
        if objective["status"] in {"succeeded", "blocked", "cancelled"}:
            return objective
        objective["status"] = status
        objective["lease"] = None
        if status in {"succeeded", "cancelled"}:
            objective["completed_at"] = tmux_state.utc_now()
            objective["blocked_reason"] = None
        elif status == "blocked":
            if not tmux_state.one_line_text(getattr(args, "reason", None)):
                die("autopilot block requires nonblank --reason")
            objective["blocked_reason"] = args.reason
            objective["completed_at"] = None
        return tmux_state.write_objective(paths, objective)


def autopilot_heartbeat_prompt(args: argparse.Namespace) -> str:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    objective = load_objective(paths, args.objective_id)
    tick_command = (
        "python scripts/tmux_control.py autopilot tick "
        f"--objective-id {shlex.quote(str(objective['objective_id']))} "
        f"--workspace {shlex.quote(str(paths['workspace']))} "
        f"--state-dir {shlex.quote(str(paths['root']))} "
        f"--for-agent --json --max-chars {AUTOPILOT_TICK_MAX_CHARS}"
    )
    return "\n".join(
        [
            f"Continue tmux Autopilot objective `{objective['objective_id']}` in workspace `{paths['workspace']}`.",
            f"Goal: {objective.get('goal')}",
            f"First run exactly: `{tick_command}`",
            "Use attempt_summary first. If action is no_action, do not open evidence files; report the current state briefly and do not modify files.",
            "If action is repair or rerun_failed and attempt_summary is insufficient, run the status evidence command first, then the log evidence command with bounded --max-chars.",
            "Do not guess a repair when evidence is insufficient; increase --max-chars only as needed, and treat a full log dump as the last resort.",
            "If action is completed or cancelled, report the result without extra evidence and remind the user the heartbeat can be paused or removed.",
            "If action is blocked, use evidence_commands only when attempt_summary is insufficient to explain the block, then remind the user the heartbeat can be paused or removed.",
            "Bounded repair allows workspace diagnostics, code/config edits, focused tests, and rerun. Block before destructive cleanup, force git operations, push/deploy, dependency installation, secrets/auth changes, or expanding to higher-cost/longer training.",
        ]
    )


def autopilot(args: argparse.Namespace) -> Any:
    if args.autopilot_action == "start":
        return autopilot_start(args)
    if args.autopilot_action == "tick":
        return autopilot_tick(args)
    if args.autopilot_action == "rerun":
        return autopilot_rerun(args)
    if args.autopilot_action == "evidence":
        return autopilot_evidence(args)
    if args.autopilot_action in {"status", "list"}:
        return autopilot_status(args)
    if args.autopilot_action == "complete":
        return autopilot_finish(args, "succeeded")
    if args.autopilot_action == "block":
        return autopilot_finish(args, "blocked")
    if args.autopilot_action == "cancel":
        return autopilot_finish(args, "cancelled")
    if args.autopilot_action == "heartbeat-prompt":
        return autopilot_heartbeat_prompt(args)
    die(f"unknown autopilot command: {args.autopilot_action}")


def task_add(args: argparse.Namespace) -> dict[str, Any]:
    if args.task_id is not None and not tmux_state.one_line_text(args.task_id):
        die("task add requires nonblank --task-id when provided")
    after_job = tmux_state.one_line_text(args.after_job) if args.after_job is not None else None
    after_event = str(args.after_event).strip() if args.after_event is not None else None
    instruction = str(args.instruction)
    after_job = after_job or None
    after_event = after_event or None
    if bool(after_job) == bool(after_event):
        die("task add requires exactly one of --after-job or --after-event")
    if not tmux_state.one_line_text(instruction):
        die("task add requires a non-empty --instruction")
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    task = tmux_state.build_task(
        task_id=args.task_id,
        instruction=instruction,
        summary=args.summary,
        intent=args.intent,
        after_job_id=after_job,
        after_event_id=after_event,
        trigger_on=args.trigger_on,
    )
    with task_lock(paths, task["task_id"]):
        if tmux_state.task_path(paths, task["task_id"]).exists():
            die(f"task already exists: {task['task_id']}")
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
    task_state_args = f"--workspace {shlex.quote(str(paths['workspace']))} --state-dir {shlex.quote(str(paths['root']))}"
    return [
        f"python scripts/tmux_control.py task load --for-skill {task_state_args}",
        f"python scripts/tmux_control.py task next --json {task_state_args}",
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
    return tmux_state.bounded_one_line_text(item.get("last_output"), limit=limit, keep_tail=True)


def task_load_text(value: Any, *, limit: int = tmux_state.TASK_DISPLAY_TEXT_LIMIT) -> str:
    return tmux_state.bounded_one_line_text(value, limit=limit)


def task_load_running_line(item: dict[str, Any]) -> str:
    identifier = task_load_text(item.get("id") or item.get("job_id") or item.get("task_id") or "unknown")
    status = task_load_text(item.get("status") or item.get("effective_status"))
    parts = [identifier]
    if status:
        parts.append(status)
    if item.get("kind"):
        parts.append(f"kind={task_load_text(item.get('kind'))}")
    if item.get("pane_id"):
        parts.append(f"pane={task_load_text(item.get('pane_id'))}")
    return " ".join(parts)


def task_load_recent_job_line(job: dict[str, Any], *, include_log: bool = False) -> str:
    identifier = task_load_text(job.get("id") or "unknown")
    status = task_load_text(job.get("status"))
    line = f"- {identifier} {status}".rstrip()
    if job.get("exit_code") is not None:
        line += f" exit={task_load_text(job.get('exit_code'))}"
    if include_log and job.get("log_path"):
        line += f" log={task_load_text(job.get('log_path'))}"
    return line


def render_task_load(data: dict[str, Any], *, for_skill: bool = False) -> str:
    if for_skill:
        lines = [
            "# tmux-skills Load Report",
            "",
            "## What happened",
        ]
        if data["recent_jobs"]:
            for job in data["recent_jobs"]:
                line = task_load_recent_job_line(job)
                tail = task_item_tail(job)
                if tail:
                    line += f" tail={tail}"
                lines.append(line)
        else:
            lines.append("- No terminal jobs recorded.")
        lines.extend(["", "## Current state"])
        for item in data["running"]:
            lines.append(f"- {task_load_running_line(item)}")
        if not data["running"]:
            lines.append("- No running work recorded.")
        lines.extend(["", "## Next actionable instruction"])
        if data["ready_tasks"]:
            for task in data["ready_tasks"]:
                lines.append(f"- task_id={task.get('task_id')}: {tmux_state.bounded_one_line_text(task.get('instruction'))}")
        else:
            lines.append("- No ready task.")
        lines.extend(["", "## Blocked or stale"])
        if data["blocked"]:
            lines.extend(f"- {tmux_state.task_summary_line(task)}" for task in data["blocked"])
        else:
            lines.append("- None")
        lines.extend(["", "## Evidence files"])
        lines.extend(f"- {task_load_text(path)}" for path in data["evidence_files"][:10])
        if not data["evidence_files"]:
            lines.append("- None")
        if data.get("errors"):
            lines.extend(["", "## State warnings"])
            lines.extend(
                f"- Skipped unreadable state file: {task_load_text(error.get('path'))} ({task_load_text(error.get('error'))})"
                for error in data["errors"][:10]
            )
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
        lines.append(f"- {task_load_running_line(item)}")
    if not data["running"]:
        lines.append("- None")
    lines.extend(["", "## Recent Jobs"])
    for job in data["recent_jobs"]:
        lines.append(task_load_recent_job_line(job, include_log=True))
    if not data["recent_jobs"]:
        lines.append("- None")
    lines.extend(["", "## Blocked or Stale"])
    lines.extend(f"- {tmux_state.task_summary_line(task)}" for task in data["blocked"])
    if not data["blocked"]:
        lines.append("- None")
    lines.extend(["", "## Evidence Files"])
    lines.extend(f"- {task_load_text(path)}" for path in data["evidence_files"][:10])
    if not data["evidence_files"]:
        lines.append("- None")
    if data.get("errors"):
        lines.extend(["", "## State Warnings"])
        lines.extend(
            f"- Skipped unreadable state file: {task_load_text(error.get('path'))} ({task_load_text(error.get('error'))})"
            for error in data["errors"][:10]
        )
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
    item_id = require_task_id(task_id)
    task, error = tmux_state.read_json(tmux_state.task_path(paths, item_id))
    if error or not task:
        die(f"could not load task {item_id}: {error or 'not found'}")
    task = dict(task)
    task["task_id"] = item_id
    return tmux_state.normalize_task(task, tmux_state.task_path(paths, item_id))


def task_claim(args: argparse.Namespace) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_task_id(args.task_id)
    with task_lock(paths, item_id):
        state = tmux_state.load_task_state(paths)
        task = find_task(paths, item_id)
        enriched = tmux_state.task_with_effective_state(task, state["statuses"])
        if enriched.get("effective_status") != "ready" and not (args.reclaim_stale and enriched.get("stale")):
            die(f"task is not ready: {item_id}")
        enriched["status"] = "in_progress"
        enriched["claimed_at"] = tmux_state.utc_now()
        enriched["completed_at"] = None
        enriched["blocked_reason"] = None
        return tmux_state.write_task(paths, enriched)


def task_finish(args: argparse.Namespace, status: str) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    item_id = require_task_id(args.task_id)
    with task_lock(paths, item_id):
        task = find_task(paths, item_id)
        task["status"] = status
        if status == "blocked":
            task["completed_at"] = None
            task["blocked_reason"] = args.note
        else:
            task["completed_at"] = tmux_state.utc_now() if status in {"done", "cancelled"} else task.get("completed_at")
            task["blocked_reason"] = None
            if args.note:
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
            if args.current_window:
                if not inside_tmux():
                    die("--current-window requires running inside tmux")
                target = current_window_target()
            elif inside_tmux():
                target = current_window_target()
            else:
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
        print(json.dumps(data, indent=2, sort_keys=True), flush=True)
    except BrokenPipeError:
        raise SystemExit(0)


def print_json_line(data: Any) -> None:
    try:
        print(json.dumps(data, sort_keys=True), flush=True)
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
    resolve_group.add_argument("--pane-index", type=nonnegative_int, help="tmux pane_index value, usually 0-based")
    resolve_group.add_argument("--ordinal", type=positive_int, help="Human 1-based pane number within the selected window")

    spawn_parser = subparsers.add_parser("spawn", help="Create a pane for long-running work")
    spawn_parser.add_argument("--target", help="tmux target such as SESSION:WINDOW")
    spawn_parser.add_argument("--cwd", help="Working directory for the new pane")
    orientation = spawn_parser.add_mutually_exclusive_group()
    orientation.add_argument("--vertical", action="store_true", help="Split vertically")
    orientation.add_argument("--horizontal", action="store_true", help="Split horizontally")
    spawn_parser.add_argument("--percent", type=split_percent, help="Pane size percentage from 1 to 99")

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
    capture_parser.add_argument("--lines", type=positive_int, default=200, help="Number of recent lines")
    capture_parser.add_argument("--strip-ansi", action="store_true", help="Remove ANSI/control escape sequences from output")
    capture_parser.add_argument("--max-chars", type=nonnegative_int, help="Return only the last N characters after optional ANSI stripping")

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
    next_instruction_group = run_parser.add_mutually_exclusive_group()
    next_instruction_group.add_argument("--next-instruction", help="Codex instruction to make ready after this job finishes")
    next_instruction_group.add_argument("--next-instruction-file", help="File containing the follow-up Codex instruction")
    run_parser.add_argument(
        "--next-on",
        choices=["succeeded", "failed", "terminal"],
        default="succeeded",
        help="Job terminal state that makes the follow-up task ready",
    )

    manager_parser = subparsers.add_parser("manager", help="Start or inspect a visible long-task manager pane")
    manager_subparsers = manager_parser.add_subparsers(dest="manager_action", required=True)
    manager_start_parser = manager_subparsers.add_parser("start", help="Start a visible manager dashboard and worker pane")
    manager_start_parser.add_argument("--manager-id")
    manager_start_parser.add_argument("--job-id")
    manager_start_command = manager_start_parser.add_mutually_exclusive_group()
    manager_start_command.add_argument("--command", dest="command_text")
    manager_start_command.add_argument("--command-file")
    manager_start_parser.add_argument("--notify", choices=["bridge", "tmux-inject", "none"], default="bridge")
    manager_start_parser.add_argument("--thread-id")
    manager_start_parser.add_argument("--endpoint")
    manager_start_parser.add_argument("--codex-pane", help="Codex tmux pane for --notify tmux-inject, or 'current' when safe")
    manager_start_parser.add_argument("--cwd")
    manager_start_parser.add_argument("--workspace")
    manager_start_parser.add_argument("--state-dir")
    manager_start_parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    manager_start_parser.add_argument("--dashboard-renderer", choices=["pane", "none"], default="pane")
    manager_start_parser.add_argument("--log-max-bytes", type=positive_int, default=tmux_manager.DEFAULT_MANAGER_LOG_MAX_BYTES)
    manager_start_parser.add_argument("--process-mode", choices=["foreground", "background"], default="foreground")

    manager_ps_poc_parser = manager_subparsers.add_parser("ps-poc", help="Write manager /ps background-terminal PoC evidence")
    manager_ps_poc_parser.add_argument("--workspace")
    manager_ps_poc_parser.add_argument("--state-dir")

    manager_status_parser = manager_subparsers.add_parser("status", help="Show one manager record")
    manager_status_parser.add_argument("--manager-id")
    manager_status_parser.add_argument("--workspace")
    manager_status_parser.add_argument("--state-dir")

    manager_bridge_check_parser = manager_subparsers.add_parser("bridge-check", help="Verify that bridge prompts reach target Codex")
    manager_bridge_check_parser.add_argument("--manager-id")
    manager_bridge_check_parser.add_argument("--ack-timeout-seconds", type=positive_float, default=30.0)
    manager_bridge_check_parser.add_argument("--workspace")
    manager_bridge_check_parser.add_argument("--state-dir")

    manager_ack_parser = manager_subparsers.add_parser("ack", help="Acknowledge that Codex received a manager terminal event")
    manager_ack_parser.add_argument("--manager-id")
    manager_ack_parser.add_argument("--event-id", required=True)
    manager_ack_parser.add_argument("--turn-id")
    manager_ack_parser.add_argument("--note")
    manager_ack_parser.add_argument("--workspace")
    manager_ack_parser.add_argument("--state-dir")

    manager_next_parser = manager_subparsers.add_parser("run-next", help="Queue follow-up work for a manager")
    manager_next_parser.add_argument("--manager-id")
    manager_next_parser.add_argument("--job-id", required=True)
    manager_next_command = manager_next_parser.add_mutually_exclusive_group(required=True)
    manager_next_command.add_argument("--command", dest="command_text")
    manager_next_command.add_argument("--command-file")
    manager_next_parser.add_argument("--cwd")
    manager_next_parser.add_argument("--workspace")
    manager_next_parser.add_argument("--state-dir")

    manager_submit_parser = manager_subparsers.add_parser("submit", help="Submit managed work to one visible worker pane")
    manager_submit_parser.add_argument("--manager-id")
    manager_submit_parser.add_argument("--job-id", required=True)
    manager_submit_target = manager_submit_parser.add_mutually_exclusive_group()
    manager_submit_target.add_argument("--pane", help="Existing stable worker pane ID, such as %%3")
    manager_submit_target.add_argument("--new-worker", action="store_true", help="Create a new tall worker pane for this job")
    manager_submit_command = manager_submit_parser.add_mutually_exclusive_group(required=True)
    manager_submit_command.add_argument("--command", dest="command_text")
    manager_submit_command.add_argument("--command-file")
    manager_submit_parser.add_argument("--cwd")
    manager_submit_parser.add_argument("--workspace")
    manager_submit_parser.add_argument("--state-dir")

    manager_cancel_parser = manager_subparsers.add_parser("cancel", help="Stop a manager dashboard loop")
    manager_cancel_parser.add_argument("--manager-id")
    manager_cancel_parser.add_argument("--stop-worker", action="store_true")
    manager_cancel_parser.add_argument("--job-id")
    manager_cancel_parser.add_argument("--all-workers", action="store_true")
    manager_cancel_parser.add_argument("--workspace")
    manager_cancel_parser.add_argument("--state-dir")

    manager_cleanup_parser = manager_subparsers.add_parser("cleanup", help="Remove cancelled manager records and optional job evidence")
    manager_cleanup_parser.add_argument("--manager-id")
    manager_cleanup_parser.add_argument("--jobs", action="store_true")
    manager_cleanup_parser.add_argument("--force", action="store_true")
    manager_cleanup_parser.add_argument("--workspace")
    manager_cleanup_parser.add_argument("--state-dir")

    monitor_parser = subparsers.add_parser("monitor", help="Start a background single-trigger pane monitor")
    monitor_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    monitor_parser.add_argument("--match-regex")
    monitor_parser.add_argument("--idle-shell", action="store_true")
    monitor_parser.add_argument("--timeout-seconds", type=positive_float)
    monitor_parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    monitor_parser.add_argument("--lines", type=positive_int, default=200)
    monitor_parser.add_argument("--status-lines", type=positive_int, default=tmux_state.DEFAULT_STATUS_LINES)
    monitor_parser.add_argument("--status-max-chars", type=positive_int, default=tmux_state.DEFAULT_STATUS_MAX_CHARS)
    monitor_parser.add_argument("--workspace")
    monitor_parser.add_argument("--state-dir")

    watch_parser = subparsers.add_parser("watch", help="Start or inspect a managed recurring pane watch")
    watch_parser.add_argument("watch_action", nargs="?", choices=["start", "list", "status", "cancel", "gc"], default="start")
    watch_parser.add_argument("--job-id")
    watch_parser.add_argument("--pane", help="Stable tmux pane ID, such as %%3")
    watch_parser.add_argument("--interval", type=positive_float, default=180.0)
    watch_parser.add_argument("--capture-lines", type=positive_int, default=80)
    watch_parser.add_argument("--status-lines", type=positive_int, default=tmux_state.DEFAULT_STATUS_LINES)
    watch_parser.add_argument("--status-max-chars", type=positive_int, default=tmux_state.DEFAULT_STATUS_MAX_CHARS)
    watch_parser.add_argument("--status-file")
    watch_parser.add_argument("--low-token", action="store_true", help="Poll only the status file during normal watch heartbeats")
    watch_parser.add_argument("--timeout-seconds", type=positive_float)
    watch_parser.add_argument("--name")
    watch_parser.add_argument("--workspace")
    watch_parser.add_argument("--state-dir")
    watch_parser.add_argument("--replace", action="store_true", help="Replace a running managed worker with the same job id")
    watch_parser.add_argument("--allow-duplicate", action="store_true", help="Allow another active worker with the same dedupe key")
    watch_parser.add_argument("--owner", help="Owner metadata for this managed worker")
    watch_parser.add_argument("--stale", action="store_true", help="For watch gc, mark stale watch records")
    watch_parser.add_argument("--dry-run", action="store_true", help="For watch gc, only report stale watch records")
    watch_parser.add_argument("--compact", action="store_true")
    watch_parser.add_argument("--no-observed-tail", action="store_true")
    watch_parser.add_argument("--max-chars", type=nonnegative_int)
    watch_parser.add_argument("--include-pane-state", action="store_true")

    queue_idle_parser = subparsers.add_parser("queue-after-idle", help="Submit a command after a pane becomes an idle shell")
    queue_idle_parser.add_argument("--job-id", required=True)
    queue_idle_parser.add_argument("--pane", "--then-pane", dest="pane", required=True, help="Stable tmux pane ID, such as %%3")
    queue_idle_command = queue_idle_parser.add_mutually_exclusive_group(required=True)
    queue_idle_command.add_argument("--command", "--then-command", dest="command_text")
    queue_idle_command.add_argument("--command-file")
    queue_idle_parser.add_argument("--poll-seconds", "--interval", dest="poll_seconds", type=positive_float, default=2.0)
    queue_idle_parser.add_argument("--timeout-seconds", type=positive_float)
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
    queue_status_command = queue_status_parser.add_mutually_exclusive_group(required=True)
    queue_status_command.add_argument("--command", "--then-command", dest="command_text")
    queue_status_command.add_argument("--command-file")
    queue_status_parser.add_argument("--status-file", required=True)
    queue_status_parser.add_argument("--require-row", action="append", default=[])
    queue_status_parser.add_argument("--fail-row", action="append", default=[])
    queue_status_parser.add_argument("--poll-seconds", "--interval", dest="poll_seconds", type=positive_float, default=2.0)
    queue_status_parser.add_argument("--timeout-seconds", type=positive_float)
    queue_status_parser.add_argument("--low-token", action="store_true", help="Record low-token status summaries")
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
    job_list_parser.add_argument("--compact", action="store_true")
    job_list_parser.add_argument("--no-observed-tail", action="store_true")
    job_list_parser.add_argument("--max-chars", type=nonnegative_int)
    job_status_parser = job_subparsers.add_parser("status", help="Show one managed worker")
    job_status_parser.add_argument("--job-id", required=True)
    job_status_parser.add_argument("--workspace")
    job_status_parser.add_argument("--state-dir")
    job_status_parser.add_argument("--compact", action="store_true")
    job_status_parser.add_argument("--no-observed-tail", action="store_true")
    job_status_parser.add_argument("--max-chars", type=nonnegative_int)
    job_status_parser.add_argument("--include-pane-state", action="store_true")
    job_cancel_parser = job_subparsers.add_parser("cancel", help="Cancel one managed worker")
    job_cancel_parser.add_argument("--job-id", required=True)
    job_cancel_parser.add_argument("--workspace")
    job_cancel_parser.add_argument("--state-dir")
    job_cancel_parser.add_argument("--compact", action="store_true")
    job_cancel_parser.add_argument("--no-observed-tail", action="store_true")
    job_cancel_parser.add_argument("--max-chars", type=nonnegative_int)
    job_cancel_parser.add_argument("--include-pane-state", action="store_true")
    job_gc_parser = job_subparsers.add_parser("gc", help="Mark stale managed workers")
    job_gc_parser.add_argument("--stale", action="store_true", help="Mark stale active managed jobs")
    job_gc_parser.add_argument("--dry-run", action="store_true", help="Only report stale active managed jobs")
    job_gc_parser.add_argument("--workspace")
    job_gc_parser.add_argument("--state-dir")
    job_gc_parser.add_argument("--compact", action="store_true")
    job_gc_parser.add_argument("--no-observed-tail", action="store_true")
    job_gc_parser.add_argument("--max-chars", type=nonnegative_int)
    job_gc_parser.add_argument("--include-pane-state", action="store_true")

    bridge_parser = subparsers.add_parser("bridge", help="Register, start, inspect, or cancel Codex wake bridges")
    bridge_subparsers = bridge_parser.add_subparsers(dest="bridge_action", required=True)
    bridge_register_parser = bridge_subparsers.add_parser("register", help="Register a Codex thread wake bridge")
    bridge_register_parser.add_argument("--thread-id", required=True)
    bridge_register_parser.add_argument("--endpoint", required=True)
    bridge_register_parser.add_argument("--bridge-id")
    bridge_register_parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    bridge_register_parser.add_argument("--quiet-seconds", type=positive_float, default=10.0)
    bridge_register_parser.add_argument("--replace", action="store_true")
    bridge_register_parser.add_argument("--workspace")
    bridge_register_parser.add_argument("--state-dir")
    bridge_start_parser = bridge_subparsers.add_parser("start", help="Start a registered bridge daemon")
    bridge_start_parser.add_argument("--bridge-id", required=True)
    start_mode = bridge_start_parser.add_mutually_exclusive_group()
    start_mode.add_argument("--foreground", action="store_true", help="Run daemon in the current process")
    start_mode.add_argument("--background", action="store_true", help="Run daemon in the background")
    bridge_start_parser.add_argument("--replace", action="store_true")
    bridge_start_parser.add_argument("--workspace")
    bridge_start_parser.add_argument("--state-dir")
    bridge_status_parser = bridge_subparsers.add_parser("status", help="Show one bridge record")
    bridge_status_parser.add_argument("--bridge-id", required=True)
    bridge_status_parser.add_argument("--json", action="store_true")
    bridge_status_parser.add_argument("--workspace")
    bridge_status_parser.add_argument("--state-dir")
    bridge_cancel_parser = bridge_subparsers.add_parser("cancel", help="Cancel one bridge daemon")
    bridge_cancel_parser.add_argument("--bridge-id", required=True)
    bridge_cancel_parser.add_argument("--workspace")
    bridge_cancel_parser.add_argument("--state-dir")

    autopilot_parser = subparsers.add_parser("autopilot", help="Manage heartbeat-driven tmux objectives")
    autopilot_subparsers = autopilot_parser.add_subparsers(dest="autopilot_action", required=True)

    autopilot_start_parser = autopilot_subparsers.add_parser("start", help="Start an Autopilot objective")
    autopilot_start_parser.add_argument("--objective-id", required=True)
    autopilot_start_parser.add_argument("--pane", required=True, help="Stable tmux pane ID, such as %%3")
    autopilot_start_command = autopilot_start_parser.add_mutually_exclusive_group(required=True)
    autopilot_start_command.add_argument("--command", dest="command_text")
    autopilot_start_command.add_argument("--command-file")
    autopilot_start_parser.add_argument("--goal", required=True)
    autopilot_start_parser.add_argument("--cwd")
    autopilot_start_parser.add_argument("--max-attempts", type=positive_int, default=3)
    autopilot_start_parser.add_argument("--require-idle-shell", action="store_true")
    autopilot_start_parser.add_argument("--workspace")
    autopilot_start_parser.add_argument("--state-dir")

    autopilot_tick_parser = autopilot_subparsers.add_parser("tick", help="Inspect an Autopilot objective and claim repair if needed")
    autopilot_tick_parser.add_argument("--objective-id", required=True)
    autopilot_tick_parser.add_argument("--for-agent", action="store_true")
    autopilot_tick_parser.add_argument("--json", action="store_true")
    autopilot_tick_parser.add_argument("--max-chars", type=nonnegative_int, default=AUTOPILOT_TICK_MAX_CHARS)
    autopilot_tick_parser.add_argument("--workspace")
    autopilot_tick_parser.add_argument("--state-dir")

    autopilot_evidence_parser = autopilot_subparsers.add_parser("evidence", help="Read bounded Autopilot evidence for the current or numbered attempt")
    autopilot_evidence_parser.add_argument("--objective-id", required=True)
    autopilot_evidence_parser.add_argument("--kind", choices=["status", "log"], required=True)
    autopilot_evidence_parser.add_argument("--attempt", default="current")
    autopilot_evidence_parser.add_argument("--max-chars", type=nonnegative_int, default=AUTOPILOT_EVIDENCE_MAX_CHARS)
    autopilot_evidence_parser.add_argument("--workspace")
    autopilot_evidence_parser.add_argument("--state-dir")

    autopilot_rerun_parser = autopilot_subparsers.add_parser("rerun", help="Start another attempt after a bounded repair")
    autopilot_rerun_parser.add_argument("--objective-id", required=True)
    autopilot_rerun_command = autopilot_rerun_parser.add_mutually_exclusive_group()
    autopilot_rerun_command.add_argument("--command", dest="command_text")
    autopilot_rerun_command.add_argument("--command-file")
    autopilot_rerun_parser.add_argument("--require-idle-shell", action="store_true")
    autopilot_rerun_parser.add_argument("--workspace")
    autopilot_rerun_parser.add_argument("--state-dir")

    autopilot_status_parser = autopilot_subparsers.add_parser("status", help="Show one Autopilot objective")
    autopilot_status_parser.add_argument("--objective-id", required=True)
    autopilot_status_parser.add_argument("--workspace")
    autopilot_status_parser.add_argument("--state-dir")

    autopilot_list_parser = autopilot_subparsers.add_parser("list", help="List Autopilot objectives")
    autopilot_list_parser.add_argument("--workspace")
    autopilot_list_parser.add_argument("--state-dir")

    for action in ("complete", "cancel", "heartbeat-prompt"):
        objective_parser = autopilot_subparsers.add_parser(action, help=f"Autopilot {action}")
        objective_parser.add_argument("--objective-id", required=True)
        objective_parser.add_argument("--workspace")
        objective_parser.add_argument("--state-dir")

    autopilot_block_parser = autopilot_subparsers.add_parser("block", help="Block an Autopilot objective")
    autopilot_block_parser.add_argument("--objective-id", required=True)
    autopilot_block_parser.add_argument("--reason", required=True)
    autopilot_block_parser.add_argument("--workspace")
    autopilot_block_parser.add_argument("--state-dir")

    task_parser = subparsers.add_parser("task", help="Manage tmux-skills follow-up tasks")
    task_subparsers = task_parser.add_subparsers(dest="task_action", required=True)

    task_add_parser = task_subparsers.add_parser("add", help="Add a Codex follow-up task")
    task_add_parser.add_argument("--task-id")
    task_anchor_group = task_add_parser.add_mutually_exclusive_group(required=True)
    task_anchor_group.add_argument("--after-job")
    task_anchor_group.add_argument("--after-event")
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
    task_load_parser.add_argument("--max-items", type=positive_int, default=5)
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
                "tmux_socket": selected_tmux_socket() or tmux_socket_from_env(),
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
    elif args.action == "manager":
        result = manager(args)
        if args.manager_action == "start":
            print_json_line(result)
        else:
            print_json(result)
        if args.manager_action == "start" and not result.get("started"):
            raise SystemExit(2)
        if args.manager_action == "bridge-check" and not result.get("verified"):
            raise SystemExit(2)
        if args.manager_action == "ack" and not result.get("acked"):
            raise SystemExit(2)
        if args.manager_action in {"run-next", "submit"} and not result.get("queued"):
            raise SystemExit(2)
        if args.manager_action == "cancel" and not result.get("cancelled"):
            raise SystemExit(2)
        if args.manager_action == "cleanup" and not result.get("cleaned"):
            raise SystemExit(2)
        if args.manager_action == "start" and result.get("start_process_mode") != "existing":
            loop_args = argparse.Namespace(
                manager_id=result["manager_id"],
                workspace=result["workspace"],
                state_dir=result["state_dir"] if "state_dir" in result else (result.get("record") or {}).get("state_dir"),
                poll_seconds=args.poll_seconds,
                dashboard_file=result.get("dashboard_path"),
            )
            try:
                raise SystemExit(tmux_manager.dashboard_loop(loop_args))
            except KeyboardInterrupt:
                paths = tmux_manager.manager_paths(loop_args.workspace, loop_args.state_dir)
                record, _error = tmux_manager.read_manager_record(paths, loop_args.manager_id)
                if record is not None:
                    record["status"] = "cancelled"
                    record["cancel_requested_at"] = tmux_state.utc_now()
                    tmux_manager.write_manager_record(paths, record)
                raise SystemExit(130)
    elif args.action == "monitor":
        if not args.match_regex and not args.idle_shell and args.timeout_seconds is None:
            die("monitor requires --match-regex, --idle-shell, or --timeout-seconds")
        result = monitor(args)
        print_json(result)
        if not result.get("started"):
            raise SystemExit(2)
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
    elif args.action == "bridge":
        workspace = args.workspace or os.getcwd()
        if args.bridge_action == "register":
            result = tmux_bridge.register_bridge(
                thread_id=args.thread_id,
                endpoint=args.endpoint,
                workspace=workspace,
                state_dir=args.state_dir,
                bridge_id=args.bridge_id,
                poll_seconds=args.poll_seconds,
                quiet_seconds=args.quiet_seconds,
                replace=args.replace,
            )
            print_json(result)
            if not result.get("registered"):
                raise SystemExit(2)
        elif args.bridge_action == "start":
            result = tmux_bridge.start_bridge(
                bridge_id=args.bridge_id,
                workspace=workspace,
                state_dir=args.state_dir,
                foreground=not args.background,
                replace=args.replace,
            )
            print_json(result)
            if not result.get("started", True) and not result.get("stopped"):
                raise SystemExit(2)
        elif args.bridge_action == "status":
            result = tmux_bridge.bridge_status(bridge_id=args.bridge_id, workspace=workspace, state_dir=args.state_dir)
            print_json(result)
        elif args.bridge_action == "cancel":
            result = tmux_bridge.cancel_bridge(bridge_id=args.bridge_id, workspace=workspace, state_dir=args.state_dir)
            print_json(result)
        else:
            parser.error(f"unknown bridge command: {args.bridge_action}")
    elif args.action == "autopilot":
        result = autopilot(args)
        print_result(result)
        if args.autopilot_action == "start" and isinstance(result, dict) and not result.get("started"):
            raise SystemExit(2)
        if args.autopilot_action == "rerun" and isinstance(result, dict) and result.get("action") in {"blocked", "rerun_failed"}:
            raise SystemExit(2)
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
