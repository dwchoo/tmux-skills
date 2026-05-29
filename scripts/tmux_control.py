#!/usr/bin/env python3
"""Small tmux helper for Codex skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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


def send(args: argparse.Namespace) -> dict[str, Any]:
    guard: dict[str, Any] | None = None
    if args.require_idle_shell:
        guard = idle_shell_check(args.pane)
        if not guard.get("ok"):
            return {
                "pane_id": args.pane,
                "sent": False,
                "sent_to_pane": False,
                "entered": False,
                "command_text": args.command_text,
                "reason": guard.get("reason"),
                "idle_shell_check": guard,
            }

    run_tmux(["send-keys", "-t", args.pane, "-l", args.command_text])
    if args.enter:
        run_tmux(["send-keys", "-t", args.pane, "Enter"])
    return {
        "pane_id": args.pane,
        "sent": True,
        "sent_to_pane": True,
        "command_text": args.command_text,
        "entered": bool(args.enter),
        "idle_shell_check": guard,
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
