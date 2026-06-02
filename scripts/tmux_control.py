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
from pathlib import Path
from typing import Any

import tmux_state
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
        "record": record,
    }


def job_list(args: argparse.Namespace, kind: str | None = None) -> dict[str, Any]:
    paths = tmux_state.state_paths(args.workspace, args.state_dir)
    tmux_state.ensure_state_dirs(paths)
    return {"jobs": load_job_records(paths, kind), "workspace": str(paths["workspace"]), "state_dir": str(paths["root"])}


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
        return {"job_id": item_id, "found": False, "reason": f"job is not a {kind} job", "record": record}
    if not record and status and not job_kind_matches(str(status.get("kind") or ""), kind):
        return {"job_id": item_id, "found": False, "reason": f"status is not a {kind} job", "status": status}
    return {
        "job_id": item_id,
        "found": bool(record or status),
        "record": record,
        "record_error": record_error,
        "status": status,
        "status_error": status_error,
        "pid_running": bool((record or {}).get("pid_running")),
    }


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
        return {"job_id": item_id, "cancelled": False, "reason": f"job already {record_status}", "record": record}
    pid = parse_int(str(record.get("pid") or ""))
    running = pid_is_running(pid)
    if running and pid:
        if not tmux_state.managed_worker_pid_matches(record):
            return {
                "job_id": item_id,
                "cancelled": False,
                "reason": "recorded pid is running but no longer looks like this tmux-skills worker",
                "record": record,
            }
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            running = False
        except PermissionError as exc:
            return {
                "job_id": item_id,
                "cancelled": False,
                "reason": f"could not signal recorded pid: {exc}",
                "record": record,
            }

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
        stored = tmux_state.normalize_managed_job(stored, record_path)
        stored.update({"status": "stale", "updated_at": now, "heartbeat_at": now, "stale_reason": stale_reason})
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
    validate_managed_identity(args)
    validate_optional_status_file(args, "watch")
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
    watch_parser.add_argument("watch_action", nargs="?", choices=["start", "list", "status", "cancel"], default="start")
    watch_parser.add_argument("--job-id")
    watch_parser.add_argument("--pane", help="Stable tmux pane ID, such as %%3")
    watch_parser.add_argument("--interval", type=positive_float, default=180.0)
    watch_parser.add_argument("--capture-lines", type=positive_int, default=80)
    watch_parser.add_argument("--status-lines", type=positive_int, default=tmux_state.DEFAULT_STATUS_LINES)
    watch_parser.add_argument("--status-max-chars", type=positive_int, default=tmux_state.DEFAULT_STATUS_MAX_CHARS)
    watch_parser.add_argument("--status-file")
    watch_parser.add_argument("--timeout-seconds", type=positive_float)
    watch_parser.add_argument("--name")
    watch_parser.add_argument("--workspace")
    watch_parser.add_argument("--state-dir")
    watch_parser.add_argument("--replace", action="store_true", help="Replace a running managed worker with the same job id")
    watch_parser.add_argument("--allow-duplicate", action="store_true", help="Allow another active worker with the same dedupe key")
    watch_parser.add_argument("--owner", help="Owner metadata for this managed worker")

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
