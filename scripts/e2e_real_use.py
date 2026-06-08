#!/usr/bin/env python3
"""Real-use E2E scenarios for tmux-skills managed workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import tmux_state
import tmux_manager
import codex_app_server_client


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "tmux_control.py"
SKIP_EXIT_CODE = 77
COMMAND_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_EXIT_CODE = 124

SMOKE_SCENARIOS = [
    "idle-continuation",
    "status-chain",
    "status-chain-waits-for-busy-pane",
    "concurrent-duplicate-race",
    "preflight-strict",
    "watch-visibility",
    "capture-strips-ansi",
]

FULL_ONLY_SCENARIOS = [
    "busy-pane-wait",
    "queue-command-file",
    "status-fail-blocks",
    "duplicate-block",
    "allow-duplicate",
    "watch-duplicate-block",
    "watch-concurrent-race",
    "replace-same-job-only",
    "cancel-active-queue",
    "stale-gc-recovery",
    "corrupted-state-degrades",
    "replace-rejects-foreign-pid",
    "pane-missing-failure",
    "status-timeout-blocks",
    "pane-dies-mid-wait",
    "task-followup-flow",
    "autopilot-repair-rerun",
    "manager-visible-success",
    "manager-visible-failure",
    "manager-run-next",
    "manager-multi-pane",
    "manager-tui-delete-completed",
    "manager-bridge-random-notify",
    "manager-tmux-inject-wakes-current-codex",
    "manager-random-repeat-until-zero-one",
    "manager-start-reuses-live-process",
    "manager-cancel",
    "manager-process-exit-keeps-worker",
]

ALL_SCENARIOS = SMOKE_SCENARIOS + FULL_ONLY_SCENARIOS


def is_active_managed_job(job: dict[str, Any]) -> bool:
    return tmux_state.is_active_managed_job(job) and not job.get("stale")


def tmux_state_compatible_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timeout_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def append_timeout_message(stderr: str, timeout: float) -> str:
    message = f"timed out after {timeout:.1f}s"
    return f"{stderr.rstrip()}\n{message}\n" if stderr else f"{message}\n"


def workspace_match_texts(workspace: Path) -> set[str]:
    return {str(workspace), str(workspace.expanduser().resolve())}


def command_has_workspace_arg(command: str, workspace_texts: set[str]) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    for index, arg in enumerate(argv):
        if arg == "--workspace" and index + 1 < len(argv) and argv[index + 1] in workspace_texts:
            return True
        if arg.startswith("--workspace=") and arg.split("=", 1)[1] in workspace_texts:
            return True
    return False


def tmux_queue_pids_for_workspace(workspace: Path) -> list[int]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,command="],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return []

    workspace_texts = workspace_match_texts(workspace)
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if "tmux_queue.py" not in command or not command_has_workspace_arg(command, workspace_texts):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    json_data: Any = None

    def summary(self) -> dict[str, Any]:
        return {
            "args": self.args,
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "json": self.json_data,
        }


class ScenarioFailure(Exception):
    def __init__(self, scenario: str, step: str, message: str, command: CommandResult | None = None) -> None:
        super().__init__(message)
        self.scenario = scenario
        self.step = step
        self.message = message
        self.command = command


def unexpected_failure(scenario: str, exc: Exception, *, step: str = "unexpected-exception") -> ScenarioFailure:
    return ScenarioFailure(scenario, step, f"{type(exc).__name__}: {exc}")


def safe_diagnostics(harness: "Harness", failure: ScenarioFailure) -> dict[str, Any]:
    try:
        return harness.diagnostics(failure)
    except Exception as exc:
        return {
            "scenario": failure.scenario,
            "step": failure.step,
            "message": failure.message,
            "diagnostics_error": f"{type(exc).__name__}: {exc}",
        }


def cleanup_failure_info(harness: "Harness", exc: Exception) -> dict[str, Any]:
    return {
        "session_absent": False,
        "server_absent": False,
        "temp_dir_removed": False,
        "repo_runtime_artifacts": [],
        "removed_repo_runtime_artifacts": list(getattr(harness, "removed_repo_artifacts", [])),
        "artifact_dir": str(getattr(harness, "base_dir", "")),
        "cleanup_error": f"{type(exc).__name__}: {exc}",
    }


def scenario_failure_result(
    harness: "Harness",
    *,
    scenario: str,
    failure: ScenarioFailure,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "status": "failed",
        "elapsed_seconds": elapsed_seconds,
        "failure": safe_diagnostics(harness, failure),
    }


class Harness:
    def __init__(self, *, keep_artifacts: bool = False) -> None:
        self.keep_artifacts = keep_artifacts
        self.base_dir = Path(tempfile.mkdtemp(prefix="tmux-skills-e2e-"))
        self.workspace = self.base_dir / "workspace"
        self.tmux_tmp = self.base_dir / "tmux"
        self.workspace.mkdir()
        self.tmux_tmp.mkdir()
        self.session = f"tmux-skills-e2e-{os.getpid()}-{int(time.time() * 1000)}"
        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env["TMUX_TMPDIR"] = str(self.tmux_tmp)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.pane: str | None = None
        self.jobs: list[str] = []
        self.current_scenario = "setup"
        self.last_command: CommandResult | None = None
        self.manager_processes: list[tuple[subprocess.Popen[str], list[str]]] = []
        self.app_server_processes: list[subprocess.Popen[str]] = []
        self.app_server_sockets: list[Path] = []
        self.removed_repo_artifacts: list[str] = []
        self.remove_repo_runtime_artifacts()

    def run(
        self,
        args: list[str],
        *,
        cwd: Path = ROOT,
        input_text: str | None = None,
        timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    ) -> CommandResult:
        try:
            result = subprocess.run(
                args,
                cwd=str(cwd),
                env=self.env,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
            )
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = timeout_output_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = append_timeout_message(timeout_output_text(getattr(exc, "stderr", None)), timeout_seconds)
            returncode = COMMAND_TIMEOUT_EXIT_CODE

        parsed: Any = None
        if stdout.strip():
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None
        command = CommandResult(args=args, returncode=returncode, stdout=stdout, stderr=stderr, json_data=parsed)
        self.last_command = command
        return command

    def control_args(self, args: list[str]) -> list[str]:
        return [sys.executable, str(CONTROL), *args]

    def popen_control(self, args: list[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.control_args(args),
            cwd=str(ROOT),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def start_manager_process(self, manager_id: str, job_id: str, command_text: str) -> tuple[subprocess.Popen[str], CommandResult, list[str]]:
        args = [
            "manager",
            "start",
            "--manager-id",
            manager_id,
            "--job-id",
            job_id,
            "--command",
            command_text,
            "--notify",
            "none",
            "--workspace",
            str(self.workspace),
            "--poll-seconds",
            "0.1",
        ]
        proc = self.popen_control(args)
        self.manager_processes.append((proc, args))
        start = self.read_process_json(proc, args)
        if not isinstance(start.json_data, dict) or start.json_data.get("started") is not True:
            raise ScenarioFailure(self.current_scenario, f"manager-start-{manager_id}", "manager did not start", start)
        return proc, start, args

    def collect_manager_process(
        self,
        proc: subprocess.Popen[str],
        args: list[str],
        *,
        step: str,
        reset_tmux: bool = True,
    ) -> CommandResult:
        command = self.collect_process(proc, args)
        self.manager_processes = [(item_proc, item_args) for item_proc, item_args in self.manager_processes if item_proc is not proc]
        if command.returncode not in {0, 130}:
            raise ScenarioFailure(self.current_scenario, step, f"manager process exited with {command.returncode}", command)
        if reset_tmux:
            self.reset_after_manager_process()
        return command

    def terminate_one_manager_process(
        self,
        proc: subprocess.Popen[str],
        args: list[str],
        *,
        step: str,
        reset_tmux: bool = True,
    ) -> CommandResult:
        if proc.poll() is None:
            proc.terminate()
        command = self.collect_process(proc, args)
        self.manager_processes = [(item_proc, item_args) for item_proc, item_args in self.manager_processes if item_proc is not proc]
        if command.returncode == COMMAND_TIMEOUT_EXIT_CODE:
            raise ScenarioFailure(self.current_scenario, step, "manager process did not terminate", command)
        if reset_tmux:
            self.reset_after_manager_process()
        return command

    def start_app_server(self, socket_path: Path) -> subprocess.Popen[str]:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self.app_server_sockets.append(socket_path)
        stdout_path = self.base_dir / "app-server.out"
        stderr_path = self.base_dir / "app-server.err"
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                ["codex", "app-server", "--listen", f"unix://{socket_path}"],
                cwd=str(ROOT),
                env=self.env,
                text=True,
                stdout=stdout_file,
                stderr=stderr_file,
            )
        self.app_server_processes.append(proc)

        def socket_ready() -> bool:
            if proc.poll() is not None:
                command = CommandResult(
                    args=["codex", "app-server", "--listen", f"unix://{socket_path}"],
                    returncode=proc.returncode or 0,
                    stdout=stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else "",
                    stderr=stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else "",
                    json_data=None,
                )
                raise ScenarioFailure(self.current_scenario, "app-server-start", "codex app-server exited before socket was ready", command)
            return socket_path.exists()

        self.poll_until("app-server-socket", 10.0, socket_ready)
        return proc

    def start_bridge_thread(self, socket_path: Path) -> str:
        instructions = "\n".join(
            [
                "You are the tmux-skills manager bridge handler for this E2E run.",
                "When a prompt starts with 'tmux-skills manager observed a bridge event.', run manager status once for the listed manager id.",
                f"Use this command shape for status: python3 {CONTROL} manager status --manager-id MANAGER_ID --workspace {self.workspace}",
                "Use manager observe with the Event read token from the prompt to inspect only that event when the token is not 'none'.",
                f"Use this command shape for observe: python3 {CONTROL} manager observe --manager-id MANAGER_ID --workspace {self.workspace} --event-id EVENT_ID --observe-token EVENT_READ_TOKEN",
                "If the Event read token is 'none', skip observe and acknowledge the listed event id immediately after the status step.",
                "Otherwise, acknowledge the listed event id with manager ack immediately after manager observe succeeds.",
                f"Use this command shape for ack: python3 {CONTROL} manager ack --manager-id MANAGER_ID --workspace {self.workspace} --event-id EVENT_ID",
                "For terminal events that contain a single random digit, extract the digit from manager observe output, run manager ack before writing any file, then write exactly '숫자는 N이 나왔습니다.' to bridge-number-response.txt in the workspace, replacing N with the digit.",
                "Use this command shape for that file write after ack: printf '숫자는 N이 나왔습니다.' > bridge-number-response.txt",
                "Do not edit repository files or close panes.",
            ]
        )
        client = codex_app_server_client.AppServerClient(f"unix://{socket_path}", timeout_seconds=10)
        try:
            client.connect()
            client.initialize("tmux-skills-e2e")
            response = client.start_thread(
                cwd=str(self.workspace),
                developer_instructions=instructions,
                sandbox="danger-full-access",
                approval_policy="never",
            )
            thread_id = codex_app_server_client.response_thread_id(response)
            if not thread_id:
                raise ScenarioFailure(self.current_scenario, "bridge-thread-id", "thread/start did not return a thread id")
            return thread_id
        finally:
            client.close()

    def start_bridge_manager(self, manager_id: str) -> tuple[subprocess.Popen[str], CommandResult, list[str], str]:
        socket_dir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        socket_path = socket_dir / f"ts-{os.getpid()}-{int(time.time() * 1000)}.sock"
        self.start_app_server(socket_path)
        thread_id = self.start_bridge_thread(socket_path)
        args = [
            "manager",
            "start",
            "--manager-id",
            manager_id,
            "--notify",
            "bridge",
            "--thread-id",
            thread_id,
            "--endpoint",
            f"unix://{socket_path}",
            "--workspace",
            str(self.workspace),
            "--poll-seconds",
            "0.1",
            "--process-mode",
            "background",
            "--dashboard-renderer",
            "none",
        ]
        proc = self.popen_control(args)
        self.manager_processes.append((proc, args))
        start = self.read_process_json(proc, args)
        if not isinstance(start.json_data, dict) or start.json_data.get("started") is not True:
            raise ScenarioFailure(self.current_scenario, f"manager-start-{manager_id}", "bridge manager did not start", start)
        bridge = self.control(
            ["manager", "bridge-check", "--manager-id", manager_id, "--ack-timeout-seconds", "60", "--workspace", str(self.workspace)],
            step="manager-bridge-check",
            timeout_seconds=120.0,
        )
        bridge_data = bridge.json_data if isinstance(bridge.json_data, dict) else {}
        if bridge_data.get("verified") is not True:
            raise ScenarioFailure(self.current_scenario, "manager-bridge-check-verified", "bridge-check did not verify", bridge)
        return proc, start, args, thread_id

    def submit_bridge_random_job(
        self,
        manager_id: str,
        job_id: str,
        command: str | None = None,
        *,
        action: str = "submit",
    ) -> tuple[int, dict[str, Any]]:
        response_path = self.workspace / "bridge-number-response.txt"
        response_path.unlink(missing_ok=True)
        command_text = command or 'python3 -c "import random,time; time.sleep(1); print(random.randint(0, 9))"'
        manager_action = "run-next" if action == "run-next" else "submit"
        submitted = self.control(
            [
                "manager",
                manager_action,
                "--manager-id",
                manager_id,
                "--job-id",
                job_id,
                "--command",
                command_text,
                "--workspace",
                str(self.workspace),
            ],
            step=f"manager-random-{manager_action}-{job_id}",
        )
        if not isinstance(submitted.json_data, dict) or submitted.json_data.get("queued") is not True:
            raise ScenarioFailure(self.current_scenario, f"manager-random-{manager_action}-{job_id}", f"manager {manager_action} did not queue random job", submitted)

        def acknowledged() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            last_event_id = str(record.get("last_terminal_event_id") or "")
            last_ack = record.get("last_ack") if isinstance(record.get("last_ack"), dict) else {}
            notification = record.get("last_notification") if isinstance(record.get("last_notification"), dict) else {}
            events = record.get("events") if isinstance(record.get("events"), dict) else {}
            event = events.get(last_event_id) if isinstance(events.get(last_event_id), dict) else {}
            active_job_ids = record.get("active_job_ids") if isinstance(record.get("active_job_ids"), list) else []
            current_status = data.get("current_job_status") if isinstance(data.get("current_job_status"), dict) else {}
            if (
                last_event_id
                and not active_job_ids
                and record.get("status") in {"waiting_for_codex", "idle"}
                and current_status.get("id") == job_id
                and current_status.get("status") in tmux_manager.MANAGER_TERMINAL_JOB_STATUSES
                and last_ack.get("event_id") == last_event_id
                and notification.get("mode") == "bridge"
                and notification.get("submitted_to_app_server") is True
                and notification.get("acknowledged_by_codex") is True
                and event.get("event_read_consumed_at")
                and event.get("acknowledged_by_codex") is True
            ):
                return data
            return None

        status = self.poll_until(f"manager-random-ack-{job_id}", 75.0, acknowledged)
        if response_path.exists():
            text = response_path.read_text(encoding="utf-8").strip()
            match = re.search(r"숫자는 ([0-9])이 나왔습니다\.", text)
            if not match:
                raise ScenarioFailure(self.current_scenario, f"manager-random-response-{job_id}", f"target Codex wrote an unexpected number response: {text!r}")
            return int(match.group(1)), status
        record = status.get("record") if isinstance(status.get("record"), dict) else {}
        last_event_id = str(record.get("last_terminal_event_id") or "")
        events = record.get("events") if isinstance(record.get("events"), dict) else {}
        event = events.get(last_event_id) if isinstance(events.get(last_event_id), dict) else {}
        current_status = status.get("current_job_status") if isinstance(status.get("current_job_status"), dict) else {}
        observed_text = str(event.get("last_output") or current_status.get("last_output") or "")
        match = re.search(r"([0-9])", observed_text)
        if not match:
            raise ScenarioFailure(self.current_scenario, f"manager-random-observed-number-{job_id}", f"manager observe evidence did not contain a random digit: {observed_text!r}")
        return int(match.group(1)), status

    def terminate_app_servers(self) -> None:
        for proc in list(self.app_server_processes):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        self.app_server_processes = []
        for socket_path in self.app_server_sockets:
            socket_path.unlink(missing_ok=True)
        self.app_server_sockets = []

    def collect_process(self, proc: subprocess.Popen[str], args: list[str]) -> CommandResult:
        try:
            stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            stderr = append_timeout_message(stderr, COMMAND_TIMEOUT_SECONDS)
            returncode = COMMAND_TIMEOUT_EXIT_CODE

        parsed: Any = None
        if stdout.strip():
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None
        command = CommandResult(
            args=self.control_args(args),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            json_data=parsed,
        )
        self.last_command = command
        return command

    def read_process_json(self, proc: subprocess.Popen[str], args: list[str], *, timeout: float = 5.0) -> CommandResult:
        if proc.stdout is None:
            raise ScenarioFailure(self.current_scenario, "process-json-stdout", "process stdout is not captured")
        deadline = time.monotonic() + timeout
        stdout_parts: list[str] = []
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.1)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                stdout_parts.append(line)
                text = "".join(stdout_parts)
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                command = CommandResult(
                    args=self.control_args(args),
                    returncode=proc.poll() if proc.poll() is not None else 0,
                    stdout=text,
                    stderr="",
                    json_data=parsed,
                )
                self.last_command = command
                return command
            if proc.poll() is not None:
                break
        stderr = ""
        if proc.poll() is not None:
            try:
                _stdout, stderr = proc.communicate(timeout=0.1)
            except subprocess.TimeoutExpired:
                stderr = ""
        command = CommandResult(args=self.control_args(args), returncode=proc.poll() or 1, stdout="".join(stdout_parts), stderr=stderr)
        self.last_command = command
        raise ScenarioFailure(self.current_scenario, "process-json-timeout", "process did not print a JSON start payload", command)

    def require_success(self, command: CommandResult, *, step: str) -> CommandResult:
        if command.returncode != 0:
            raise ScenarioFailure(self.current_scenario, step, f"command failed with exit {command.returncode}", command)
        return command

    def control(self, args: list[str], *, step: str, check: bool = True, timeout_seconds: float = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
        command = self.run(self.control_args(args), timeout_seconds=timeout_seconds)
        if check:
            self.require_success(command, step=step)
        return command

    def setup_tmux(self) -> None:
        self.require_success(
            self.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    self.session,
                    "-c",
                    str(self.workspace),
                    "env",
                    "PS1=$ ",
                    "bash",
                    "--noprofile",
                    "--norc",
                ]
            ),
            step="tmux-new-session",
        )
        time.sleep(0.3)
        panes = self.control(["list"], step="list-panes").json_data.get("panes", [])
        candidates = [pane for pane in panes if pane.get("session_name") == self.session]
        if not candidates:
            raise ScenarioFailure(self.current_scenario, "find-pane", f"session pane not listed: {panes}")
        self.pane = str(candidates[0]["pane_id"])

    def reset_tmux_session(self) -> None:
        self.run(["tmux", "kill-session", "-t", self.session])
        self.pane = None
        self.setup_tmux()

    def manager_tmux_session_name(self) -> str:
        base = self.workspace.resolve().name or "workspace"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-._")
        return f"codex-{safe or 'workspace'}"

    def reset_after_manager_process(self) -> None:
        self.run(["tmux", "kill-session", "-t", self.manager_tmux_session_name()])
        self.reset_tmux_session()

    def tmux_send(self, command: str, *, step: str) -> None:
        if not self.pane:
            raise ScenarioFailure(self.current_scenario, step, "pane is not initialized")
        self.require_success(self.run(["tmux", "send-keys", "-t", self.pane, command, "Enter"]), step=step)

    def tmux_kill_pane(self, pane: str, *, step: str) -> None:
        self.require_success(self.run(["tmux", "kill-pane", "-t", pane]), step=step)

    def poll_until(self, label: str, timeout: float, predicate: Callable[[], Any]) -> Any:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
            except Exception as exc:  # noqa: BLE001 - E2E diagnostics preserve arbitrary failures.
                last_error = exc
            else:
                if value:
                    return value
            time.sleep(0.1)
        detail = f"timed out after {timeout:.1f}s"
        if last_error:
            detail += f"; last error: {last_error}"
        raise ScenarioFailure(self.current_scenario, label, detail, self.last_command)

    def job_status(self, job_id: str, *, check: bool = False) -> dict[str, Any]:
        command = self.control(
            ["job", "status", "--job-id", job_id, "--workspace", str(self.workspace)],
            step=f"job-status-{job_id}",
            check=check,
        )
        return command.json_data or {}

    def job_list(self) -> list[dict[str, Any]]:
        command = self.control(["job", "list", "--workspace", str(self.workspace)], step="job-list", check=False)
        data = command.json_data if isinstance(command.json_data, dict) else {}
        jobs = data.get("jobs") or []
        return jobs if isinstance(jobs, list) else []

    def active_jobs(self) -> list[dict[str, Any]]:
        return [job for job in self.job_list() if is_active_managed_job(job)]

    def active_job_ids(self) -> list[str]:
        job_ids: list[str] = []
        for job in self.active_jobs():
            job_id = str(job.get("job_id") or "")
            if job_id and job_id not in job_ids:
                job_ids.append(job_id)
        return job_ids

    def cancel_active_jobs(self) -> None:
        job_ids = self.active_job_ids()
        for job_id in self.jobs:
            if job_id not in job_ids:
                job_ids.append(job_id)
        for job_id in reversed(job_ids):
            self.control(["job", "cancel", "--job-id", job_id, "--workspace", str(self.workspace)], step=f"cancel-{job_id}", check=False)
        self.jobs = self.active_job_ids()

    def interrupt_pane(self) -> None:
        if self.pane:
            self.run(["tmux", "send-keys", "-t", self.pane, "C-c"])
            time.sleep(0.2)

    def terminate_manager_processes(self) -> None:
        for proc, _args in self.manager_processes:
            if proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        self.manager_processes = []

    def tmux_pane_exists(self, pane_id: str) -> bool:
        return self.run(["tmux", "display-message", "-p", "-t", pane_id, "#{pane_id}"]).returncode == 0

    def manager_status_data(self, manager_id: str) -> dict[str, Any]:
        command = self.control(
            [
                "manager",
                "status",
                "--manager-id",
                manager_id,
                "--workspace",
                str(self.workspace),
                "--manual-override",
                "--reason",
                "real-use e2e status assertion",
            ],
            step=f"manager-status-{manager_id}",
            check=False,
        )
        return command.json_data if isinstance(command.json_data, dict) else {}

    def wait_manager_status(
        self,
        manager_id: str,
        expected_manager_status: str,
        *,
        expected_job_status: str | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        def check_manager() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            current_status = data.get("current_job_status") if isinstance(data.get("current_job_status"), dict) else {}
            if record.get("status") != expected_manager_status:
                return None
            if expected_job_status and current_status.get("status") != expected_job_status:
                return None
            return data

        return self.poll_until(f"wait-manager-{manager_id}-{expected_manager_status}", timeout, check_manager)

    def assert_manager_layout_geometry(self, manager_pane_id: str, worker_pane_id: str) -> dict[str, Any]:
        panes = self.control(["list"], step=f"{self.current_scenario}-layout-list").json_data.get("panes", [])
        manager_pane = next((pane for pane in panes if pane.get("pane_id") == manager_pane_id), None)
        worker_pane = next((pane for pane in panes if pane.get("pane_id") == worker_pane_id), None)
        if not isinstance(manager_pane, dict) or not isinstance(worker_pane, dict):
            raise ScenarioFailure(self.current_scenario, "manager-layout-panes", "manager or worker pane was not listed")
        codex_candidates = [
            pane
            for pane in panes
            if pane.get("session_name") == manager_pane.get("session_name")
            and pane.get("window_id") == manager_pane.get("window_id")
            and pane.get("pane_id") not in {manager_pane_id, worker_pane_id}
        ]
        if len(codex_candidates) != 1:
            raise ScenarioFailure(
                self.current_scenario,
                "manager-layout-codex-pane",
                f"expected one Codex pane candidate, found {len(codex_candidates)}",
            )
        codex_pane = codex_candidates[0]
        codex_right = int(codex_pane.get("pane_left") or 0) + int(codex_pane.get("pane_width") or 0)
        worker_left = int(worker_pane.get("pane_left") or 0)
        if worker_left < codex_right - 2:
            raise ScenarioFailure(self.current_scenario, "manager-layout-worker-side", "worker pane is not to the right of Codex")
        if int(worker_pane.get("pane_height") or 0) < int(codex_pane.get("pane_height") or 0):
            raise ScenarioFailure(self.current_scenario, "manager-layout-worker-height", "worker pane is not tall enough for long output")
        codex_bottom = int(codex_pane.get("pane_top") or 0) + int(codex_pane.get("pane_height") or 0)
        manager_top = int(manager_pane.get("pane_top") or 0)
        if manager_top < codex_bottom or manager_top - codex_bottom > 2:
            raise ScenarioFailure(self.current_scenario, "manager-layout-manager-below", "manager pane is not directly below Codex")
        if int(manager_pane.get("pane_height") or 0) > int(codex_pane.get("pane_height") or 0):
            raise ScenarioFailure(self.current_scenario, "manager-layout-manager-compact", "manager pane is not compact relative to Codex")
        return {
            "codex_pane_id": codex_pane.get("pane_id"),
            "codex_height": codex_pane.get("pane_height"),
            "manager_height": manager_pane.get("pane_height"),
            "worker_height": worker_pane.get("pane_height"),
        }

    def before_scenario(self, name: str) -> None:
        self.current_scenario = name
        self.cancel_active_jobs()
        self.interrupt_pane()

    def after_scenario(self) -> None:
        self.terminate_manager_processes()
        self.terminate_app_servers()
        self.cancel_active_jobs()
        self.interrupt_pane()

    def start_queue_idle(self, job_id: str, command_text: str, *extra: str, check: bool = True) -> CommandResult:
        self.jobs.append(job_id)
        return self.control(
            [
                "queue-after-idle",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--command",
                command_text,
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
                *extra,
            ],
            step=f"start-{job_id}",
            check=check,
        )

    def wait_status(self, job_id: str, expected: str, *, timeout: float = 10.0) -> dict[str, Any]:
        def check_status() -> dict[str, Any] | None:
            data = self.job_status(job_id)
            status = (data.get("status") or {}).get("status") or (data.get("record") or {}).get("status")
            return data if status == expected else None

        return self.poll_until(f"wait-{job_id}-{expected}", timeout, check_status)

    def wait_file(self, path: Path, expected_text: str, *, timeout: float = 10.0) -> bool:
        def check_file() -> bool:
            return path.exists() and path.read_text(encoding="utf-8") == expected_text

        return bool(self.poll_until(f"wait-file-{path.name}", timeout, check_file))

    def state_tree(self) -> list[str]:
        root = self.workspace / ".codex" / "tmux-skills"
        if not root.exists():
            return []
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())[:100]

    def pane_capture(self) -> str:
        if not self.pane:
            return ""
        command = self.control(["capture", "--pane", self.pane, "--lines", "80", "--strip-ansi", "--max-chars", "4000"], step="pane-capture", check=False)
        data = command.json_data if isinstance(command.json_data, dict) else {}
        return str(data.get("output") or command.stdout)[-4000:]

    def tmux_pane_capture_text(self, pane_id: str, *, lines: int = 120) -> str:
        command = self.run(["tmux", "capture-pane", "-p", "-e", "-t", pane_id, "-S", f"-{max(1, lines)}"], timeout_seconds=5.0)
        return command.stdout if command.returncode == 0 else command.stderr

    def diagnostics(self, failure: ScenarioFailure) -> dict[str, Any]:
        return {
            "scenario": failure.scenario,
            "step": failure.step,
            "message": failure.message,
            "command": failure.command.summary() if failure.command else None,
            "workspace": str(self.workspace),
            "state_dir": str(self.workspace / ".codex" / "tmux-skills"),
            "session": self.session,
            "pane": self.pane,
            "pane_capture": self.pane_capture(),
            "state_tree": self.state_tree(),
            "jobs": {job_id: self.job_status(job_id) for job_id in self.jobs},
        }

    def repo_runtime_artifacts(self) -> list[str]:
        artifacts: list[str] = []
        ignored_parts = {".git"}
        for path in ROOT.rglob("__pycache__"):
            if path.is_dir() and not (set(path.relative_to(ROOT).parts) & ignored_parts):
                artifacts.append(str(path.relative_to(ROOT)))
        for path in ROOT.rglob("*.pyc"):
            if path.is_file() and not (set(path.relative_to(ROOT).parts) & ignored_parts):
                artifacts.append(str(path.relative_to(ROOT)))
        return sorted(artifacts)

    def remove_repo_runtime_artifacts(self) -> list[str]:
        before = self.repo_runtime_artifacts()
        for path in ROOT.rglob("*.pyc"):
            if ".git" not in path.relative_to(ROOT).parts:
                path.unlink(missing_ok=True)
        for path in sorted(ROOT.rglob("__pycache__"), key=lambda item: len(item.parts), reverse=True):
            if ".git" not in path.relative_to(ROOT).parts and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        self.removed_repo_artifacts.extend(item for item in before if item not in self.removed_repo_artifacts)
        return before

    def session_exists(self) -> bool:
        return self.run(["tmux", "has-session", "-t", self.session]).returncode == 0

    def server_exists(self) -> bool:
        return self.run(["tmux", "list-sessions"]).returncode == 0

    def terminate_workspace_workers(self) -> list[int]:
        signalled: list[int] = []
        for pid in tmux_queue_pids_for_workspace(self.workspace):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
            signalled.append(pid)
        return signalled

    def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, Any]:
        self.terminate_manager_processes()
        self.terminate_app_servers()
        self.cancel_active_jobs()
        self.run(["tmux", "kill-session", "-t", self.session])
        self.run(["tmux", "kill-server"])
        worker_pids_signalled = self.terminate_workspace_workers()
        session_absent = not self.session_exists()
        server_absent = not self.server_exists()
        if remove_artifacts:
            shutil.rmtree(self.base_dir, ignore_errors=True)
        self.remove_repo_runtime_artifacts()
        repo_artifacts = self.repo_runtime_artifacts()
        temp_dir_removed = not self.base_dir.exists()
        return {
            "session_absent": session_absent,
            "server_absent": server_absent,
            "temp_dir_removed": temp_dir_removed,
            "repo_runtime_artifacts": repo_artifacts,
            "removed_repo_runtime_artifacts": self.removed_repo_artifacts,
            "artifact_dir": str(self.base_dir),
            "worker_pids_signalled": worker_pids_signalled,
        }

    def scenario_idle_continuation(self) -> dict[str, Any]:
        self.current_scenario = "idle-continuation"
        job_id = "idle-e2e"
        output = self.workspace / "idle.out"
        start = self.start_queue_idle(job_id, "printf idle-ok > idle.out")
        self.wait_status(job_id, "submitted")
        self.wait_file(output, "idle-ok")
        record = self.job_status(job_id).get("record") or {}
        for key in ("dedupe_key", "owner", "check_interval_seconds"):
            if key not in record:
                raise ScenarioFailure(self.current_scenario, "record-contract", f"missing record key: {key}")
        return {"job_id": job_id, "status_path": start.json_data.get("status_path"), "output": str(output)}

    def scenario_status_chain(self) -> dict[str, Any]:
        self.current_scenario = "status-chain"
        job_id = "status-e2e"
        status_file = self.workspace / "status.tsv"
        output = self.workspace / "status.out"
        status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\trunning\n", encoding="utf-8")
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-status",
                "--job-id",
                job_id,
                "--then-pane",
                str(self.pane),
                "--then-command",
                "printf status-ok > status.out",
                "--status-file",
                "status.tsv",
                "--require-row",
                "run_cfg=configs/msec.toml,status=done",
                "--interval",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-status-chain",
        )
        time.sleep(0.3)
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "premature-submit", "status command ran before TSV was done")
        status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tdone\n", encoding="utf-8")
        self.wait_status(job_id, "submitted")
        self.wait_file(output, "status-ok")
        return {"job_id": job_id, "output": str(output)}

    def scenario_status_chain_waits_for_busy_pane(self) -> dict[str, Any]:
        self.current_scenario = "status-chain-waits-for-busy-pane"
        job_id = "status-busy-e2e"
        status_file = self.workspace / "status-busy.tsv"
        output = self.workspace / "status-busy.out"
        status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tdone\n", encoding="utf-8")
        self.tmux_send("sleep 2.5", step="busy-pane")
        time.sleep(0.2)
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-status",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--command",
                "printf status-busy-ok > status-busy.out",
                "--status-file",
                "status-busy.tsv",
                "--require-row",
                "run_cfg=configs/msec.toml,status=done",
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-status-busy",
        )
        waiting = self.wait_status(job_id, "waiting_pane_idle", timeout=2.0)
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "premature-submit", "status command ran while target pane was busy")
        self.wait_status(job_id, "submitted", timeout=15.0)
        self.wait_file(output, "status-busy-ok", timeout=15.0)
        return {"job_id": job_id, "waiting_status": (waiting.get("status") or {}).get("status"), "output": str(output)}

    def scenario_concurrent_duplicate_race(self) -> dict[str, Any]:
        self.current_scenario = "concurrent-duplicate-race"
        job_ids = ["race-a", "race-b"]
        self.jobs.extend(job_ids)
        self.tmux_send("sleep 3", step="busy-pane")
        time.sleep(0.2)
        base_args = [
            "queue-after-idle",
            "--pane",
            str(self.pane),
            "--command",
            "printf race-ok > race.out",
            "--poll-seconds",
            "0.1",
            "--workspace",
            str(self.workspace),
        ]
        procs = [
            self.popen_control([*base_args, "--job-id", job_ids[0], "--owner", "codex-a"]),
            self.popen_control([*base_args, "--job-id", job_ids[1], "--owner", "codex-b"]),
        ]
        results = [
            self.collect_process(procs[0], [*base_args, "--job-id", job_ids[0], "--owner", "codex-a"]),
            self.collect_process(procs[1], [*base_args, "--job-id", job_ids[1], "--owner", "codex-b"]),
        ]
        started = [result for result in results if isinstance(result.json_data, dict) and result.json_data.get("started") is True]
        duplicates = [
            result
            for result in results
            if result.returncode == 2 and isinstance(result.json_data, dict) and result.json_data.get("duplicate") is True
        ]
        if len(started) != 1 or len(duplicates) != 1:
            raise ScenarioFailure(
                self.current_scenario,
                "exactly-one-start",
                f"expected one started and one duplicate; got {[result.summary() for result in results]}",
            )
        dedupe_key = started[0].json_data.get("dedupe_key")
        active_same_key = [
            job
            for job in self.active_jobs()
            if job.get("dedupe_key") == dedupe_key
        ]
        if len(active_same_key) != 1:
            raise ScenarioFailure(
                self.current_scenario,
                "active-record-count",
                f"expected one active record for dedupe key; got {active_same_key}",
            )
        return {
            "started_job_id": started[0].json_data.get("job_id"),
            "duplicate_job_id": duplicates[0].json_data.get("job_id"),
            "duplicate_exit_code": duplicates[0].returncode,
            "active_job_id": active_same_key[0].get("job_id"),
        }

    def scenario_duplicate_block(self) -> dict[str, Any]:
        self.current_scenario = "duplicate-block"
        self.tmux_send("sleep 4", step="busy-pane")
        time.sleep(0.2)
        self.start_queue_idle("dup-first", "printf dup > dup.out", "--owner", "codex-a")
        second = self.start_queue_idle("dup-second", "printf dup > dup.out", "--owner", "codex-b", check=False)
        if second.returncode != 2 or not isinstance(second.json_data, dict) or not second.json_data.get("duplicate"):
            raise ScenarioFailure(self.current_scenario, "duplicate-contract", "duplicate was not rejected with exit 2", second)
        if second.json_data.get("existing_job_id") != "dup-first":
            raise ScenarioFailure(self.current_scenario, "duplicate-existing", "wrong existing_job_id", second)
        return {"duplicate_exit_code": second.returncode, "existing_job_id": second.json_data.get("existing_job_id")}

    def scenario_preflight_strict(self) -> dict[str, Any]:
        self.current_scenario = "preflight-strict"
        scripts_dir = self.workspace / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "mock_train.sh"
        script.write_text("printf should-not-run > preflight.out\n", encoding="utf-8")
        command = self.control(
            [
                "send",
                "--pane",
                str(self.pane),
                "--command",
                str(script),
                "--enter",
                "--strict-preflight",
            ],
            step="strict-preflight",
            check=False,
        )
        data = command.json_data if isinstance(command.json_data, dict) else {}
        if command.returncode != 2 or data.get("sent_to_pane") is not False:
            raise ScenarioFailure(self.current_scenario, "strict-contract", "strict preflight did not reject send", command)
        if (self.workspace / "preflight.out").exists():
            raise ScenarioFailure(self.current_scenario, "preflight-output", "non-executable script was run")
        return {"returncode": command.returncode, "reason": data.get("reason")}

    def scenario_watch_visibility(self) -> dict[str, Any]:
        self.current_scenario = "watch-visibility"
        job_id = "watch-e2e"
        self.jobs.append(job_id)
        self.control(
            [
                "watch",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--interval",
                "0.1",
                "--timeout-seconds",
                "0.8",
                "--workspace",
                str(self.workspace),
            ],
            step="start-watch",
        )

        def job_list_has_active_watch() -> bool:
            for job in self.job_list():
                if job.get("job_id") == job_id and is_active_managed_job(job):
                    return True
            return False

        self.poll_until("job-list-active-watch", 3.0, job_list_has_active_watch)
        self.wait_status(job_id, "timeout", timeout=5.0)
        status = self.job_status(job_id).get("status") or {}
        log_path = Path(str(status.get("log_path") or ""))
        if not log_path.exists():
            raise ScenarioFailure(self.current_scenario, "watch-log", "watch log was not written")
        return {"job_id": job_id, "log_path": str(log_path)}

    def scenario_capture_strips_ansi(self) -> dict[str, Any]:
        self.current_scenario = "capture-strips-ansi"
        ready = self.workspace / "capture.ready"
        self.control(
            [
                "send",
                "--pane",
                str(self.pane),
                "--command",
                "printf '\\033[31mERR\\033[0m line1\\r\\033[KDONE\\n'",
                "--enter",
            ],
            step="send-ansi-noise",
        )

        def mark_when_idle() -> CommandResult | None:
            command = self.control(
                [
                    "send",
                    "--pane",
                    str(self.pane),
                    "--command",
                    "printf ready > capture.ready",
                    "--enter",
                    "--require-idle-shell",
                ],
                step="capture-ready-marker",
                check=False,
            )
            data = command.json_data if isinstance(command.json_data, dict) else {}
            return command if command.returncode == 0 and data.get("sent_to_pane") is True else None

        self.poll_until("pane-idle-after-ansi", 5.0, mark_when_idle)
        self.wait_file(ready, "ready", timeout=5.0)
        stripped = self.control(
            ["capture", "--pane", str(self.pane), "--lines", "50", "--strip-ansi"],
            step="capture-stripped",
        )
        plain = self.control(["capture", "--pane", str(self.pane), "--lines", "50"], step="capture-plain")
        stripped_output = str((stripped.json_data or {}).get("output") or "")
        if "DONE" not in stripped_output:
            raise ScenarioFailure(self.current_scenario, "capture-visible-token", "stripped capture did not include visible output", stripped)
        if "\x1b" in stripped_output or "\x1b[" in stripped_output:
            raise ScenarioFailure(self.current_scenario, "capture-ansi-stripped", "stripped capture still contains ANSI escape bytes", stripped)
        plain_output = str((plain.json_data or {}).get("output") or "")
        return {
            "stripped_contains_done": "DONE" in stripped_output,
            "stripped_has_esc": "\x1b" in stripped_output,
            "plain_length": len(plain_output),
        }

    def scenario_busy_pane_wait(self) -> dict[str, Any]:
        self.current_scenario = "busy-pane-wait"
        output = self.workspace / "busy.out"
        self.tmux_send("sleep 2", step="busy-pane")
        time.sleep(0.2)
        self.start_queue_idle("busy-e2e", "printf busy-ok > busy.out")
        time.sleep(0.5)
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "premature-submit", "command submitted while pane was busy")
        self.wait_status("busy-e2e", "submitted", timeout=15.0)
        self.wait_file(output, "busy-ok")
        return {"job_id": "busy-e2e", "output": str(output)}

    def scenario_queue_command_file(self) -> dict[str, Any]:
        self.current_scenario = "queue-command-file"
        job_id = "command-file-e2e"
        command_file = self.workspace / "queued-command.sh"
        output = self.workspace / "command-file.out"
        command_file.write_text("printf file-ok > command-file.out\n", encoding="utf-8")
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-idle",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--command-file",
                str(command_file),
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-command-file-queue",
        )
        final = self.wait_status(job_id, "submitted", timeout=10.0)
        self.wait_file(output, "file-ok", timeout=5.0)
        record = final.get("record") or {}
        copied_command = Path(str(record.get("command_path") or ""))
        if copied_command.resolve() == command_file.resolve():
            raise ScenarioFailure(self.current_scenario, "command-copy", "command-file was not copied into managed state")
        if not copied_command.exists():
            raise ScenarioFailure(self.current_scenario, "command-copy-exists", "managed command copy does not exist")
        if copied_command.read_text(encoding="utf-8") != command_file.read_text(encoding="utf-8"):
            raise ScenarioFailure(self.current_scenario, "command-copy-content", "managed command copy changed content")
        return {"job_id": job_id, "output": str(output), "command_path": str(copied_command)}

    def scenario_status_fail_blocks(self) -> dict[str, Any]:
        self.current_scenario = "status-fail-blocks"
        job_id = "status-fail-e2e"
        status_file = self.workspace / "fail.tsv"
        output = self.workspace / "fail.out"
        status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\trunning\n", encoding="utf-8")
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-status",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--command",
                "printf fail-bad > fail.out",
                "--status-file",
                "fail.tsv",
                "--require-row",
                "run_cfg=configs/msec.toml,status=done",
                "--fail-row",
                "run_cfg=configs/msec.toml,status=failed",
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-status-fail",
        )
        status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tfailed\n", encoding="utf-8")
        self.wait_status(job_id, "failed")
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "fail-output", "fail condition still submitted command")
        status = self.job_status(job_id).get("status") or {}
        return {"job_id": job_id, "matched_fail_rows": status.get("matched_fail_rows")}

    def scenario_allow_duplicate(self) -> dict[str, Any]:
        self.current_scenario = "allow-duplicate"
        self.tmux_send("sleep 4", step="busy-pane")
        time.sleep(0.2)
        self.start_queue_idle("allow-first", "printf allow > allow.out")
        second = self.start_queue_idle("allow-second", "printf allow > allow.out", "--allow-duplicate")
        record = (second.json_data or {}).get("record") or {}
        if record.get("duplicate_allowed") is not True or record.get("duplicate_of") != "allow-first":
            raise ScenarioFailure(self.current_scenario, "allow-contract", "duplicate metadata missing", second)
        return {"duplicate_of": record.get("duplicate_of")}

    def scenario_watch_duplicate_block(self) -> dict[str, Any]:
        self.current_scenario = "watch-duplicate-block"
        first_id = "watch-dupe-first"
        second_id = "watch-dupe-second"
        allowed_id = "watch-dupe-allowed"
        status_file = self.workspace / "watch-dupe.status"
        status_file.write_text("watching\n", encoding="utf-8")
        self.jobs.extend([first_id, second_id, allowed_id])
        self.control(
            [
                "watch",
                "--job-id",
                first_id,
                "--pane",
                str(self.pane),
                "--interval",
                "0.2",
                "--timeout-seconds",
                "5",
                "--status-file",
                "watch-dupe.status",
                "--workspace",
                str(self.workspace),
            ],
            step="watch-dupe-first",
        )
        second = self.control(
            [
                "watch",
                "--job-id",
                second_id,
                "--pane",
                str(self.pane),
                "--interval",
                "0.2",
                "--timeout-seconds",
                "5",
                "--status-file",
                "watch-dupe.status",
                "--workspace",
                str(self.workspace),
            ],
            step="watch-dupe-second",
            check=False,
        )
        if second.returncode != 2 or not (isinstance(second.json_data, dict) and second.json_data.get("duplicate")):
            raise ScenarioFailure(self.current_scenario, "watch-duplicate-contract", "watch duplicate was not rejected", second)
        allowed = self.control(
            [
                "watch",
                "--job-id",
                allowed_id,
                "--pane",
                str(self.pane),
                "--interval",
                "0.2",
                "--timeout-seconds",
                "5",
                "--status-file",
                "watch-dupe.status",
                "--workspace",
                str(self.workspace),
                "--allow-duplicate",
            ],
            step="watch-dupe-allowed",
        )
        record = (allowed.json_data or {}).get("record") or {}
        if record.get("duplicate_allowed") is not True or record.get("duplicate_of") != first_id:
            raise ScenarioFailure(self.current_scenario, "watch-allow-contract", "watch allow-duplicate metadata missing", allowed)
        return {"duplicate_exit_code": second.returncode, "duplicate_of": record.get("duplicate_of")}

    def scenario_watch_concurrent_race(self) -> dict[str, Any]:
        self.current_scenario = "watch-concurrent-race"
        job_ids = ["watch-race-a", "watch-race-b"]
        self.jobs.extend(job_ids)
        base_args = [
            "watch",
            "--pane",
            str(self.pane),
            "--interval",
            "0.1",
            "--capture-lines",
            "20",
            "--timeout-seconds",
            "2",
            "--workspace",
            str(self.workspace),
        ]
        procs = [
            self.popen_control([*base_args, "--job-id", job_ids[0], "--owner", "codex-a"]),
            self.popen_control([*base_args, "--job-id", job_ids[1], "--owner", "codex-b"]),
        ]
        results = [
            self.collect_process(procs[0], [*base_args, "--job-id", job_ids[0], "--owner", "codex-a"]),
            self.collect_process(procs[1], [*base_args, "--job-id", job_ids[1], "--owner", "codex-b"]),
        ]
        started = [result for result in results if isinstance(result.json_data, dict) and result.json_data.get("started") is True]
        duplicates = [
            result
            for result in results
            if result.returncode == 2 and isinstance(result.json_data, dict) and result.json_data.get("duplicate") is True
        ]
        if len(started) != 1 or len(duplicates) != 1:
            raise ScenarioFailure(
                self.current_scenario,
                "exactly-one-start",
                f"expected one started and one duplicate; got {[result.summary() for result in results]}",
            )
        dedupe_key = started[0].json_data.get("dedupe_key")
        active_same_key = [
            job
            for job in self.active_jobs()
            if tmux_state.token_text(job.get("kind")) == "watch" and job.get("dedupe_key") == dedupe_key
        ]
        if len(active_same_key) > 1:
            raise ScenarioFailure(
                self.current_scenario,
                "active-record-count",
                f"expected at most one active watch record for dedupe key; got {active_same_key}",
            )
        return {
            "started_job_id": started[0].json_data.get("job_id"),
            "duplicate_job_id": duplicates[0].json_data.get("job_id"),
            "duplicate_exit_code": duplicates[0].returncode,
            "active_job_ids": [job.get("job_id") for job in active_same_key],
        }

    def scenario_replace_same_job_only(self) -> dict[str, Any]:
        self.current_scenario = "replace-same-job-only"
        output = self.workspace / "replace.out"
        self.tmux_send("sleep 2", step="busy-pane-same")
        time.sleep(0.2)
        self.start_queue_idle("replace-job", "printf first > replace.out")
        self.start_queue_idle("replace-job", "printf second > replace.out", "--replace")
        self.wait_status("replace-job", "submitted", timeout=15.0)
        self.wait_file(output, "second", timeout=15.0)

        self.tmux_send("sleep 4", step="busy-pane-different")
        time.sleep(0.2)
        self.start_queue_idle("replace-first", "printf same > replace-dupe.out")
        second = self.start_queue_idle("replace-second", "printf same > replace-dupe.out", "--replace", check=False)
        if second.returncode != 2 or not (isinstance(second.json_data, dict) and second.json_data.get("duplicate")):
            raise ScenarioFailure(self.current_scenario, "replace-different-contract", "different job id was not duplicate-rejected", second)
        return {"same_job_output": output.read_text(encoding="utf-8"), "different_job_exit": second.returncode}

    def scenario_cancel_active_queue(self) -> dict[str, Any]:
        self.current_scenario = "cancel-active-queue"
        job_id = "cancel-e2e"
        output = self.workspace / "cancel.out"
        self.tmux_send("sleep 5", step="busy-pane")
        time.sleep(0.2)
        self.start_queue_idle(job_id, "printf should-not-run > cancel.out")
        self.wait_status(job_id, "waiting_pane_idle", timeout=5.0)
        cancelled = self.control(["job", "cancel", "--job-id", job_id, "--workspace", str(self.workspace)], step="cancel-active")
        if not (cancelled.json_data or {}).get("cancelled"):
            raise ScenarioFailure(self.current_scenario, "cancel-contract", "job cancel did not report cancelled", cancelled)

        def stopped() -> dict[str, Any] | None:
            data = self.job_status(job_id)
            status = (data.get("status") or {}).get("status") or (data.get("record") or {}).get("status")
            return data if status == "cancelled" and data.get("pid_running") is False else None

        final_status = self.poll_until("cancel-pid-stopped", 5.0, stopped)
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "cancel-output", "cancelled queue still submitted the command")
        return {
            "job_id": job_id,
            "status": (final_status.get("status") or {}).get("status"),
            "pid_running": final_status.get("pid_running"),
        }

    def scenario_stale_gc_recovery(self) -> dict[str, Any]:
        self.current_scenario = "stale-gc-recovery"
        seed_id = "stale-seed"
        stale_id = "stale-old"
        self.start_queue_idle(seed_id, "printf stale > stale.out")
        self.wait_status(seed_id, "submitted")
        seed_record_path = self.workspace / ".codex" / "tmux-skills" / "jobs" / f"{seed_id}.json"
        seed_record = json.loads(seed_record_path.read_text(encoding="utf-8"))

        record_path = self.workspace / ".codex" / "tmux-skills" / "jobs" / f"{stale_id}.json"
        record = dict(seed_record)
        old = "2000-01-01T00:00:00Z"
        record.update(
            {
                "job_id": stale_id,
                "status": "waiting_pane_idle",
                "pid": 0,
                "heartbeat_at": old,
                "updated_at": old,
                "created_at": old,
                "check_interval_seconds": 1,
                "status_path": str(self.workspace / ".codex" / "tmux-skills" / "status" / f"{stale_id}.json"),
                "log_path": str(self.workspace / ".codex" / "tmux-skills" / "logs" / f"{stale_id}.log"),
            }
        )
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        dry = self.control(["job", "gc", "--stale", "--dry-run", "--workspace", str(self.workspace)], step="gc-dry-run")
        stale_jobs = (dry.json_data or {}).get("stale_jobs") or []
        if not any(job.get("job_id") == stale_id for job in stale_jobs):
            raise ScenarioFailure(self.current_scenario, "gc-dry-run-contract", "stale job not reported", dry)
        self.control(["job", "gc", "--stale", "--workspace", str(self.workspace)], step="gc-mark")
        marked = json.loads(record_path.read_text(encoding="utf-8"))
        if marked.get("status") != "stale":
            raise ScenarioFailure(self.current_scenario, "gc-mark-contract", "stale job was not marked")
        new = self.start_queue_idle("stale-new", "printf stale > stale.out")
        if not (new.json_data or {}).get("started"):
            raise ScenarioFailure(self.current_scenario, "stale-recreate", "same dedupe was not reusable after stale gc", new)
        return {"old_status": marked.get("status"), "new_job": "stale-new"}

    def scenario_corrupted_state_degrades(self) -> dict[str, Any]:
        self.current_scenario = "corrupted-state-degrades"
        run_id = "corrupt-good-run"
        managed_id = "corrupt-good-managed"
        self.jobs.append(run_id)
        run = self.control(
            [
                "run",
                "--job-id",
                run_id,
                "--pane",
                str(self.pane),
                "--command",
                "printf corrupt-ok > corrupt-good.out",
                "--workspace",
                str(self.workspace),
            ],
            step="run-good-job",
        )
        self.wait_status(run_id, "succeeded", timeout=10.0)
        self.start_queue_idle(managed_id, "printf corrupt-managed > corrupt-managed.out")
        self.wait_status(managed_id, "submitted", timeout=10.0)
        state_dir = self.workspace / ".codex" / "tmux-skills"
        corrupt_status = state_dir / "status" / "corrupt.json"
        corrupt_job = state_dir / "jobs" / "corrupt.json"
        corrupt_encoding = state_dir / "jobs" / "corrupt-encoding.json"
        corrupt_status.parent.mkdir(parents=True, exist_ok=True)
        corrupt_job.parent.mkdir(parents=True, exist_ok=True)
        corrupt_status.write_text("{ not: valid json", encoding="utf-8")
        corrupt_job.write_text("{ not: valid json", encoding="utf-8")
        corrupt_encoding.write_bytes(b"\xff\xfe{")

        listed = self.control(["job", "list", "--workspace", str(self.workspace)], step="job-list-corrupt", check=False)
        if listed.returncode != 0:
            raise ScenarioFailure(self.current_scenario, "job-list-returncode", "job list failed on corrupt state", listed)
        if "Traceback (most recent call last)" in listed.stderr:
            raise ScenarioFailure(self.current_scenario, "job-list-traceback", "job list emitted a traceback", listed)
        listed_data = listed.json_data if isinstance(listed.json_data, dict) else {}
        jobs = listed_data.get("jobs") or []
        if not any(isinstance(job, dict) and job.get("job_id") == managed_id for job in jobs):
            raise ScenarioFailure(self.current_scenario, "job-list-valid-entry", "valid job missing from job list", listed)

        loaded = self.control(["task", "load", "--for-skill", "--workspace", str(self.workspace)], step="task-load-corrupt", check=False)
        if loaded.returncode != 0:
            raise ScenarioFailure(self.current_scenario, "task-load-returncode", "task load failed on corrupt state", loaded)
        if "Traceback (most recent call last)" in loaded.stderr:
            raise ScenarioFailure(self.current_scenario, "task-load-traceback", "task load emitted a traceback", loaded)
        if "unreadable" not in loaded.stdout:
            raise ScenarioFailure(self.current_scenario, "task-load-unreadable", "task load did not report unreadable state files", loaded)
        return {
            "run_id": run_id,
            "managed_id": managed_id,
            "run_status_path": (run.json_data or {}).get("status_path"),
            "corrupt_status": str(corrupt_status),
            "corrupt_job": str(corrupt_job),
            "corrupt_encoding": str(corrupt_encoding),
        }

    def scenario_replace_rejects_foreign_pid(self) -> dict[str, Any]:
        self.current_scenario = "replace-rejects-foreign-pid"
        job_id = "foreign-pid-e2e"
        self.jobs.append(job_id)
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            record_path = self.workspace / ".codex" / "tmux-skills" / "jobs" / f"{job_id}.json"
            record_path.parent.mkdir(parents=True, exist_ok=True)
            now = tmux_state_compatible_now()
            record = {
                "version": 1,
                "job_id": job_id,
                "kind": "queue-after-idle",
                "status": "waiting_pane_idle",
                "pid": foreign.pid,
                "pane_id": str(self.pane),
                "workspace": str(self.workspace),
                "state_dir": str(self.workspace / ".codex" / "tmux-skills"),
                "status_path": str(self.workspace / ".codex" / "tmux-skills" / "status" / f"{job_id}.json"),
                "log_path": str(self.workspace / ".codex" / "tmux-skills" / "logs" / f"{job_id}.log"),
                "created_at": now,
                "updated_at": now,
                "heartbeat_at": now,
                "check_interval_seconds": 1,
            }
            record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = self.start_queue_idle(job_id, "printf foreign > foreign.out", "--replace", check=False)
            data = result.json_data if isinstance(result.json_data, dict) else {}
            reason = str(data.get("reason") or "")
            if result.returncode != 2 or data.get("started") is not False or "no longer looks like this tmux-skills worker" not in reason:
                raise ScenarioFailure(self.current_scenario, "foreign-pid-contract", "foreign pid replace was not rejected safely", result)
            if foreign.poll() is not None:
                raise ScenarioFailure(self.current_scenario, "foreign-pid-still-running", "foreign process was killed by --replace")
            return {"job_id": job_id, "foreign_pid": foreign.pid, "reason": reason}
        finally:
            if foreign.poll() is None:
                foreign.terminate()
                try:
                    foreign.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    foreign.kill()
                    foreign.wait(timeout=5)

    def scenario_pane_missing_failure(self) -> dict[str, Any]:
        self.current_scenario = "pane-missing-failure"
        job_id = "missing-pane-e2e"
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-idle",
                "--job-id",
                job_id,
                "--pane",
                "%999999",
                "--command",
                "printf missing > missing.out",
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-missing-pane",
        )
        self.wait_status(job_id, "failed", timeout=5.0)
        if (self.workspace / "missing.out").exists():
            raise ScenarioFailure(self.current_scenario, "missing-output", "missing pane command produced output")
        return {"job_id": job_id, "status": "failed"}

    def scenario_status_timeout_blocks(self) -> dict[str, Any]:
        self.current_scenario = "status-timeout-blocks"
        job_id = "status-timeout-e2e"
        status_file = self.workspace / "status-timeout.tsv"
        output = self.workspace / "status-timeout.out"
        status_file.write_text("path\tstate\nconfigs/run.toml\trunning\n", encoding="utf-8")
        self.jobs.append(job_id)
        self.control(
            [
                "queue-after-status",
                "--job-id",
                job_id,
                "--pane",
                str(self.pane),
                "--command",
                "printf should-not-run > status-timeout.out",
                "--status-file",
                "status-timeout.tsv",
                "--require-row",
                "configs/run.toml:done",
                "--timeout-seconds",
                "1",
                "--poll-seconds",
                "0.1",
                "--workspace",
                str(self.workspace),
            ],
            step="start-status-timeout",
        )
        final = self.wait_status(job_id, "timeout", timeout=5.0)
        if output.exists():
            raise ScenarioFailure(self.current_scenario, "timeout-output", "timed out status wait still submitted command")
        record = final.get("record") or {}
        status = final.get("status") or {}
        for source_name, source in (("record", record), ("status", status)):
            if "matched_required_rows" not in source:
                raise ScenarioFailure(self.current_scenario, "timeout-diagnostics", f"{source_name} missing matched_required_rows")
        if record.get("send_result") is not None or status.get("send_result") is not None:
            raise ScenarioFailure(self.current_scenario, "timeout-submit-contract", "timed out status wait recorded a send_result")
        if tmux_state.token_text(record.get("status")) == "submitted" or tmux_state.token_text(status.get("status")) == "submitted":
            raise ScenarioFailure(self.current_scenario, "timeout-submitted", "timed out status wait became submitted")
        if status.get("exit_code") != 1:
            raise ScenarioFailure(self.current_scenario, "timeout-exit-code", "timed out status wait did not record exit code 1")
        return {
            "job_id": job_id,
            "status": status.get("status"),
            "matched_required_rows": status.get("matched_required_rows"),
        }

    def scenario_pane_dies_mid_wait(self) -> dict[str, Any]:
        self.current_scenario = "pane-dies-mid-wait"
        job_id = "pane-dies-e2e"
        output = self.workspace / "pane-dies.out"
        target_pane = str(self.pane)
        self.tmux_send("sleep 20", step="busy-pane")
        time.sleep(0.2)
        try:
            self.start_queue_idle(job_id, "printf should-not-run > pane-dies.out")
            self.wait_status(job_id, "waiting_pane_idle", timeout=5.0)
            self.tmux_kill_pane(target_pane, step="kill-target-pane")

            def terminal_failure() -> dict[str, Any] | None:
                data = self.job_status(job_id)
                status = (data.get("status") or {}).get("status") or (data.get("record") or {}).get("status")
                return data if status in {"failed", "timeout"} else None

            final = self.poll_until("pane-dies-terminal", 8.0, terminal_failure)
            if output.exists():
                raise ScenarioFailure(self.current_scenario, "pane-dies-output", "dead pane queue still submitted command")
            record = final.get("record") or {}
            status_record = final.get("status") or {}
            if record.get("send_result") is not None or status_record.get("send_result") is not None:
                raise ScenarioFailure(self.current_scenario, "pane-dies-submit-contract", "dead pane queue recorded a send_result")
            status = (final.get("status") or {}).get("status") or (final.get("record") or {}).get("status")
            return {"job_id": job_id, "status": status, "output_exists": output.exists()}
        finally:
            self.reset_tmux_session()

    def scenario_task_followup_flow(self) -> dict[str, Any]:
        self.current_scenario = "task-followup-flow"
        job_id = "task-followup-e2e"
        instruction = "FOLLOWUP: inspect results"
        self.jobs.append(job_id)
        started = self.control(
            [
                "run",
                "--pane",
                str(self.pane),
                "--job-id",
                job_id,
                "--command",
                "printf task-ok > task-followup.out",
                "--workspace",
                str(self.workspace),
                "--next-instruction",
                instruction,
                "--next-on",
                "succeeded",
            ],
            step="run-with-followup",
        )
        next_task = (started.json_data or {}).get("next_task") or {}
        task_id = str(next_task.get("task_id") or "")
        if not task_id:
            raise ScenarioFailure(self.current_scenario, "next-task-created", "run did not return a follow-up task", started)
        self.wait_status(job_id, "succeeded", timeout=10.0)

        def ready_task() -> dict[str, Any] | None:
            command = self.control(
                ["task", "list", "--json", "--workspace", str(self.workspace)],
                step="task-list-ready",
            )
            tasks = command.json_data if isinstance(command.json_data, list) else []
            for task in tasks:
                if task.get("task_id") == task_id and task.get("effective_status") == "ready":
                    return task
            return None

        ready = self.poll_until("task-ready", 5.0, ready_task)
        loaded = self.control(
            ["task", "load", "--for-skill", "--workspace", str(self.workspace)],
            step="task-load-ready",
        )
        if task_id not in loaded.stdout or instruction not in loaded.stdout:
            raise ScenarioFailure(self.current_scenario, "task-load-ready-task", "task load did not expose the ready follow-up task", loaded)
        claimed = self.control(
            ["task", "claim", "--task-id", task_id, "--workspace", str(self.workspace)],
            step="task-claim",
        )
        claimed_data = claimed.json_data if isinstance(claimed.json_data, dict) else {}
        if claimed_data.get("status") != "in_progress":
            raise ScenarioFailure(self.current_scenario, "task-claim-status", "claimed task did not move to in_progress", claimed)
        next_after_claim = self.control(
            ["task", "next", "--json", "--workspace", str(self.workspace)],
            step="task-next-after-claim",
        )
        next_data = next_after_claim.json_data if isinstance(next_after_claim.json_data, dict) else {}
        if next_data.get("task_id") == task_id and next_data.get("effective_status") == "ready":
            raise ScenarioFailure(self.current_scenario, "task-next-claimed", "claimed task was still returned as ready", next_after_claim)
        return {"job_id": job_id, "task_id": task_id, "ready_status": ready.get("effective_status"), "claimed_status": claimed_data.get("status")}

    def scenario_manager_visible_success(self) -> dict[str, Any]:
        self.current_scenario = "manager-visible-success"
        manager_id = "manager-success"
        job_id = "manager-success-job"
        output = self.workspace / "manager-success.out"
        manager_proc, start, manager_args = self.start_manager_process(
            manager_id,
            job_id,
            "printf manager-ok > manager-success.out",
        )
        start_data = start.json_data if isinstance(start.json_data, dict) else {}
        self.wait_file(output, "manager-ok", timeout=10.0)
        status = self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="succeeded", timeout=10.0)
        record = status.get("record") if isinstance(status.get("record"), dict) else {}
        if record.get("last_notification", {}).get("mode") != "none":
            raise ScenarioFailure(self.current_scenario, "manager-notify-none", "manager did not record dashboard-only notification")
        layout = self.assert_manager_layout_geometry(str(start_data.get("manager_pane_id")), str(start_data.get("worker_pane_id")))
        manager_pane = str(start_data.get("manager_pane_id") or "")
        dashboard = self.control(["capture", "--pane", manager_pane, "--lines", "80", "--strip-ansi"], step="manager-success-dashboard")
        dashboard_output = dashboard.json_data.get("output") if isinstance(dashboard.json_data, dict) else dashboard.stdout
        if "manager" not in str(dashboard_output) or "LATEST EVENT" not in str(dashboard_output):
            raise ScenarioFailure(self.current_scenario, "manager-success-compact-dashboard", "compact dashboard was not visible", dashboard)
        if "clear; cat" in str(dashboard_output) or "printf '\\033[2J" in str(dashboard_output):
            raise ScenarioFailure(self.current_scenario, "manager-success-dashboard-history", "dashboard accumulated clear/cat prompt history", dashboard)
        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="manager-success-cancel")
        self.collect_manager_process(manager_proc, manager_args, step="manager-success-process-exit")
        return {
            "manager_id": manager_id,
            "job_id": job_id,
            "manager_pane_id": start_data.get("manager_pane_id"),
            "worker_pane_id": start_data.get("worker_pane_id"),
            "layout": layout,
            "output": str(output),
        }

    def scenario_manager_visible_failure(self) -> dict[str, Any]:
        self.current_scenario = "manager-visible-failure"
        manager_id = "manager-failure"
        job_id = "manager-failure-job"
        manager_proc, start, manager_args = self.start_manager_process(
            manager_id,
            job_id,
            "python3 -c 'print(\"manager-failed\"); raise SystemExit(7)'",
        )
        status = self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="failed", timeout=10.0)
        current_status = status.get("current_job_status") if isinstance(status.get("current_job_status"), dict) else {}
        log_path = Path(str(current_status.get("log_path") or ""))
        if not log_path.exists():
            raise ScenarioFailure(self.current_scenario, "manager-failure-log", "failed manager job did not write a log", start)
        start_data = start.json_data if isinstance(start.json_data, dict) else {}
        manager_pane = str(start_data.get("manager_pane_id") or "")
        dashboard = self.control(["capture", "--pane", manager_pane, "--lines", "80", "--strip-ansi"], step="manager-failure-dashboard")
        dashboard_output = dashboard.json_data.get("output") if isinstance(dashboard.json_data, dict) else dashboard.stdout
        record = status.get("record") if isinstance(status.get("record"), dict) else {}
        dashboard_path = Path(str(record.get("dashboard_path") or ""))
        if ("LATEST EVENT" not in str(dashboard_output) or "failed" not in str(dashboard_output)) and dashboard_path.exists():
            dashboard_output = dashboard_path.read_text(encoding="utf-8", errors="replace")
        if "LATEST EVENT" not in str(dashboard_output) or "failed" not in str(dashboard_output):
            raise ScenarioFailure(self.current_scenario, "manager-failure-compact-event", "compact dashboard did not show failed latest event", dashboard)
        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="manager-failure-cancel")
        self.collect_manager_process(manager_proc, manager_args, step="manager-failure-process-exit")
        return {"manager_id": manager_id, "job_id": job_id, "log_path": str(log_path)}

    def scenario_manager_run_next(self) -> dict[str, Any]:
        self.current_scenario = "manager-run-next"
        manager_id = "manager-next"
        first_job = "manager-next-first"
        second_job = "manager-next-second"
        first_output = self.workspace / "manager-next-first.out"
        second_output = self.workspace / "manager-next-second.out"
        manager_proc, _start, manager_args = self.start_manager_process(
            manager_id,
            first_job,
            "printf first-ok > manager-next-first.out",
        )
        self.wait_file(first_output, "first-ok", timeout=10.0)
        self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="succeeded", timeout=10.0)
        queued = self.control(
            [
                "manager",
                "run-next",
                "--manager-id",
                manager_id,
                "--job-id",
                second_job,
                "--command",
                "printf second-ok > manager-next-second.out",
                "--workspace",
                str(self.workspace),
            ],
            step="manager-run-next",
        )
        if not isinstance(queued.json_data, dict) or queued.json_data.get("queued") is not True:
            raise ScenarioFailure(self.current_scenario, "manager-run-next-queued", "manager run-next did not queue follow-up work", queued)
        self.wait_file(second_output, "second-ok", timeout=10.0)
        final_status = self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="succeeded", timeout=10.0)
        current_status = final_status.get("current_job_status") if isinstance(final_status.get("current_job_status"), dict) else {}
        if current_status.get("id") != second_job:
            raise ScenarioFailure(self.current_scenario, "manager-run-next-current-job", "manager did not switch to the follow-up job")
        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="manager-next-cancel")
        self.collect_manager_process(manager_proc, manager_args, step="manager-next-process-exit")
        return {"manager_id": manager_id, "first_job": first_job, "second_job": second_job}

    def scenario_manager_multi_pane(self) -> dict[str, Any]:
        self.current_scenario = "manager-multi-pane"
        manager_id = "manager-multi"
        slow_job = "manager-multi-slow"
        fast_job = "manager-multi-fast"
        slow_output = self.workspace / "manager-multi-slow.out"
        fast_output = self.workspace / "manager-multi-fast.out"
        manager_proc, start, manager_args = self.start_manager_process(
            manager_id,
            slow_job,
            "sleep 1.5; printf slow-ok > manager-multi-slow.out",
        )
        start_data = start.json_data if isinstance(start.json_data, dict) else {}
        self.wait_manager_status(manager_id, "running", timeout=10.0)
        submitted = self.control(
            [
                "manager",
                "submit",
                "--manager-id",
                manager_id,
                "--new-worker",
                "--job-id",
                fast_job,
                "--command",
                "printf fast-ok > manager-multi-fast.out",
                "--workspace",
                str(self.workspace),
            ],
            step="manager-multi-submit",
        )
        submitted_data = submitted.json_data if isinstance(submitted.json_data, dict) else {}
        if submitted_data.get("queued") is not True:
            raise ScenarioFailure(self.current_scenario, "manager-multi-submit-queued", "manager submit did not queue", submitted)
        self.wait_file(fast_output, "fast-ok", timeout=10.0)

        def fast_done_slow_active() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
            active = record.get("active_job_ids") if isinstance(record.get("active_job_ids"), list) else []
            fast = jobs.get(fast_job) if isinstance(jobs.get(fast_job), dict) else {}
            if fast.get("status") == "succeeded" and slow_job in active:
                return data
            return None

        self.poll_until("manager-multi-fast-terminal", 10.0, fast_done_slow_active)
        self.wait_file(slow_output, "slow-ok", timeout=10.0)

        def both_done() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
            slow = jobs.get(slow_job) if isinstance(jobs.get(slow_job), dict) else {}
            fast = jobs.get(fast_job) if isinstance(jobs.get(fast_job), dict) else {}
            active = record.get("active_job_ids") if isinstance(record.get("active_job_ids"), list) else []
            worker_panes = record.get("worker_pane_ids") if isinstance(record.get("worker_pane_ids"), list) else []
            if slow.get("status") == "succeeded" and fast.get("status") == "succeeded" and not active and len(worker_panes) >= 2:
                return data
            return None

        final_status = self.poll_until("manager-multi-both-terminal", 10.0, both_done)
        record = final_status.get("record") if isinstance(final_status.get("record"), dict) else {}
        jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
        fast_record = jobs.get(fast_job) if isinstance(jobs.get(fast_job), dict) else {}
        fast_pane = str(fast_record.get("pane_id") or "")
        manager_pane = str(record.get("manager_pane_id") or start_data.get("manager_pane_id") or "")
        dashboard = self.control(["capture", "--pane", manager_pane, "--lines", "80", "--strip-ansi"], step="manager-multi-dashboard")
        dashboard_output = dashboard.json_data.get("output") if isinstance(dashboard.json_data, dict) else dashboard.stdout
        if "manager" not in str(dashboard_output) or "LATEST EVENT" not in str(dashboard_output):
            raise ScenarioFailure(self.current_scenario, "manager-multi-dashboard-tui", "dashboard did not show compact TUI sections", dashboard)
        if ".codex/tmux-skills" in str(dashboard_output) or "/commands/" in str(dashboard_output) or "/logs/" in str(dashboard_output):
            raise ScenarioFailure(self.current_scenario, "manager-multi-dashboard-compact", "compact dashboard exposed detailed paths", dashboard)
        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="manager-multi-cancel")
        self.collect_manager_process(manager_proc, manager_args, step="manager-multi-process-exit")
        return {
            "manager_id": manager_id,
            "slow_job": slow_job,
            "fast_job": fast_job,
            "default_worker_pane_id": start_data.get("worker_pane_id"),
            "fast_worker_pane_id": fast_pane,
            "worker_pane_ids": record.get("worker_pane_ids"),
        }

    def scenario_manager_tui_delete_completed(self) -> dict[str, Any]:
        self.current_scenario = "manager-tui-delete-completed"
        manager_id = "manager-delete"
        slow_job = "manager-delete-active"
        fast_job = "manager-delete-done"
        fast_output = self.workspace / "manager-delete-done.out"
        manager_proc, start, manager_args = self.start_manager_process(
            manager_id,
            slow_job,
            "sleep 8; printf active-ok > manager-delete-active.out",
        )
        start_data = start.json_data if isinstance(start.json_data, dict) else {}
        self.wait_manager_status(manager_id, "running", timeout=10.0)
        submitted = self.control(
            [
                "manager",
                "submit",
                "--manager-id",
                manager_id,
                "--new-worker",
                "--job-id",
                fast_job,
                "--command",
                "printf done-ok > manager-delete-done.out",
                "--workspace",
                str(self.workspace),
            ],
            step="manager-delete-submit",
        )
        if not isinstance(submitted.json_data, dict) or submitted.json_data.get("queued") is not True:
            raise ScenarioFailure(self.current_scenario, "manager-delete-submit-queued", "manager submit did not queue terminal job", submitted)
        self.wait_file(fast_output, "done-ok", timeout=10.0)

        def fast_done_slow_active() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
            active = record.get("active_job_ids") if isinstance(record.get("active_job_ids"), list) else []
            fast = jobs.get(fast_job) if isinstance(jobs.get(fast_job), dict) else {}
            if fast.get("status") == "succeeded" and slow_job in active:
                return data
            return None

        before_delete = self.poll_until("manager-delete-fast-terminal", 10.0, fast_done_slow_active)
        before_record = before_delete.get("record") if isinstance(before_delete.get("record"), dict) else {}
        before_jobs = before_record.get("jobs") if isinstance(before_record.get("jobs"), dict) else {}
        fast_record = before_jobs.get(fast_job) if isinstance(before_jobs.get(fast_job), dict) else {}
        evidence_paths = [Path(str(value)) for value in (fast_record.get("command_request_path"), fast_record.get("status_path"), fast_record.get("log_path")) if value]
        panes_before = len(self.control(["list"], step="manager-delete-list-before").json_data.get("panes", []))
        manager_pane = str(before_record.get("manager_pane_id") or start_data.get("manager_pane_id") or "")
        if not manager_pane:
            raise ScenarioFailure(self.current_scenario, "manager-delete-pane", "manager pane id missing")

        self.require_success(self.run(["tmux", "send-keys", "-t", manager_pane, "d"]), step="manager-delete-key")

        def terminal_deleted_active_preserved() -> dict[str, Any] | None:
            data = self.manager_status_data(manager_id)
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            jobs = record.get("jobs") if isinstance(record.get("jobs"), dict) else {}
            active = record.get("active_job_ids") if isinstance(record.get("active_job_ids"), list) else []
            if fast_job not in jobs and slow_job in jobs and slow_job in active:
                return data
            return None

        after_delete = self.poll_until("manager-delete-row-removed", 10.0, terminal_deleted_active_preserved)
        panes_after = len(self.control(["list"], step="manager-delete-list-after").json_data.get("panes", []))
        if panes_after != panes_before:
            raise ScenarioFailure(self.current_scenario, "manager-delete-pane-count", "TUI deletion changed pane/window layout")
        remaining_paths = [str(path) for path in evidence_paths if path.exists()]
        if remaining_paths:
            raise ScenarioFailure(self.current_scenario, "manager-delete-evidence", f"terminal evidence still exists: {remaining_paths}")

        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace), "--all-workers"], step="manager-delete-cancel")
        self.terminate_one_manager_process(manager_proc, manager_args, step="manager-delete-process-exit")
        record = after_delete.get("record") if isinstance(after_delete.get("record"), dict) else {}
        return {
            "manager_id": manager_id,
            "deleted_job": fast_job,
            "active_job": slow_job,
            "active_job_ids": record.get("active_job_ids"),
            "panes_before": panes_before,
            "panes_after": panes_after,
        }

    def scenario_manager_bridge_random_notify(self) -> dict[str, Any]:
        self.current_scenario = "manager-bridge-random-notify"
        manager_id = "manager-bridge-random"
        manager_proc, _start, manager_args, thread_id = self.start_bridge_manager(manager_id)
        try:
            number, status = self.submit_bridge_random_job(manager_id, "manager-bridge-random-1")
            record = status.get("record") if isinstance(status.get("record"), dict) else {}
            return {
                "manager_id": manager_id,
                "thread_id": thread_id,
                "number": number,
                "last_terminal_event_id": record.get("last_terminal_event_id"),
                "last_ack": record.get("last_ack"),
            }
        finally:
            self.terminate_one_manager_process(manager_proc, manager_args, step="manager-bridge-random-process-exit")

    def scenario_manager_tmux_inject_wakes_current_codex(self) -> dict[str, Any]:
        self.current_scenario = "manager-tmux-inject-wakes-current-codex"
        manager_id = "manager-tmux-inject"
        job_id = "manager-tmux-inject-random"
        response_path = self.workspace / "tmux-inject-number-response.txt"
        codex_session = f"{self.session}-codex"
        codex_pane_id: str | None = None
        manager_proc: subprocess.Popen[str] | None = None
        manager_args: list[str] | None = None
        old_sdk_decision = self.env.get("TMUX_SKILLS_CODEX_SDK_DECISION")
        self.env["TMUX_SKILLS_CODEX_SDK_DECISION"] = "inject"
        ready_path = self.workspace / "tmux-inject-target-ready.txt"
        instructions = "\n".join(
            [
                "You are the tmux-skills tmux-inject E2E target.",
                "First, run `printf ready > tmux-inject-target-ready.txt`, reply READY, and then wait for a future user prompt.",
                "When a future prompt starts with 'ID:' and mentions 'tmux-manager event ready', inspect manager status once for this workspace.",
                f"Use this command shape for status if needed: python3 {CONTROL} manager status --manager-id {manager_id} --workspace {self.workspace}",
                f"The manager id will be {manager_id}. Read the event id and Event read token from the wake prompt.",
                f"Use this command shape to inspect the event: python3 {CONTROL} manager observe --manager-id {manager_id} --workspace {self.workspace} --event-id EVENT_ID --observe-token EVENT_READ_TOKEN",
                f"Use this command shape to acknowledge it: python3 {CONTROL} manager ack --manager-id {manager_id} --workspace {self.workspace} --event-id EVENT_ID",
                "Extract the single random digit from manager observe output, run manager ack before writing any file, then write exactly `숫자는 N이 나왔습니다.` to tmux-inject-number-response.txt in this workspace, replacing N with the digit.",
                "Use this command shape for that file write after ack: printf '숫자는 N이 나왔습니다.' > tmux-inject-number-response.txt",
                "Do not edit repository files, close panes, stop worker jobs, or run unrelated commands.",
            ]
        )
        codex_command = shlex.join(
            [
                "codex",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(self.workspace),
                instructions,
            ]
        )
        try:
            self.require_success(
                self.run(["tmux", "new-session", "-d", "-s", codex_session, "-c", str(self.workspace), codex_command]),
                step="tmux-inject-codex-session",
            )
            time.sleep(3.0)
            self.require_success(self.run(["tmux", "send-keys", "-t", codex_session, "Enter"]), step="tmux-inject-codex-trust")

            def codex_pane_ready() -> str | None:
                panes = self.control(["list"], step="tmux-inject-list-codex-pane").json_data.get("panes", [])
                for pane in panes:
                    if pane.get("session_name") == codex_session and "codex" in str(pane.get("current_command") or "").lower():
                        return str(pane.get("pane_id") or "")
                return None

            codex_pane_id = self.poll_until("tmux-inject-codex-pane-ready", 45.0, codex_pane_ready)
            if not codex_pane_id:
                raise ScenarioFailure(self.current_scenario, "tmux-inject-codex-pane-ready", "actual Codex TUI pane did not become ready")
            self.wait_file(ready_path, "ready", timeout=75.0)

            manager_args = [
                "manager",
                "start",
                "--manager-id",
                manager_id,
                "--notify",
                "tmux-inject",
                "--codex-pane",
                codex_pane_id,
                "--workspace",
                str(self.workspace),
                "--poll-seconds",
                "0.1",
                "--dashboard-renderer",
                "none",
            ]
            manager_proc = self.popen_control(manager_args)
            self.manager_processes.append((manager_proc, manager_args))
            start = self.read_process_json(manager_proc, manager_args)
            start_data = start.json_data if isinstance(start.json_data, dict) else {}
            if start_data.get("started") is not True:
                raise ScenarioFailure(self.current_scenario, "tmux-inject-manager-started", "tmux-inject manager did not start", start)

            command_text = 'python3 -c "import random,time; time.sleep(15); print(random.randint(0, 9))"'
            submitted = self.control(
                [
                    "manager",
                    "submit",
                    "--manager-id",
                    manager_id,
                    "--job-id",
                    job_id,
                    "--command",
                    command_text,
                    "--workspace",
                    str(self.workspace),
                ],
                step="tmux-inject-submit-random",
            )
            if not isinstance(submitted.json_data, dict) or submitted.json_data.get("queued") is not True:
                raise ScenarioFailure(self.current_scenario, "tmux-inject-submit-random", "manager submit did not queue random job", submitted)

            def response_ready() -> dict[str, Any] | None:
                response_text = None
                response_number = None
                if response_path.exists():
                    response_text = response_path.read_text(encoding="utf-8").strip()
                    response_match = re.search(r"숫자는 ([0-9])이 나왔습니다\.", response_text)
                    if not response_match:
                        raise ScenarioFailure(self.current_scenario, "tmux-inject-response", f"unexpected Codex response: {response_text!r}")
                    response_number = int(response_match.group(1))
                data = self.manager_status_data(manager_id)
                record = data.get("record") if isinstance(data.get("record"), dict) else {}
                last_event_id = str(record.get("last_terminal_event_id") or "")
                notification = record.get("last_notification") if isinstance(record.get("last_notification"), dict) else {}
                last_ack = record.get("last_ack") if isinstance(record.get("last_ack"), dict) else {}
                events = record.get("events") if isinstance(record.get("events"), dict) else {}
                event = events.get(last_event_id) if isinstance(events.get(last_event_id), dict) else {}
                if (
                    last_event_id
                    and notification.get("mode") == "tmux-inject"
                    and notification.get("submitted_to_tmux") is True
                    and notification.get("injected_to_tmux") is True
                    and last_ack.get("event_id") == last_event_id
                    and event.get("acknowledged_by_codex") is True
                    and event.get("event_read_consumed_at")
                ):
                    if response_number is not None:
                        number = response_number
                    else:
                        current_status = data.get("current_job_status") if isinstance(data.get("current_job_status"), dict) else {}
                        observed_text = str(event.get("last_output") or current_status.get("last_output") or "")
                        match = re.search(r"([0-9])", observed_text)
                        if not match:
                            raise ScenarioFailure(self.current_scenario, "tmux-inject-observed-number", f"manager observe evidence did not contain a random digit: {observed_text!r}")
                        number = int(match.group(1))
                    return {"number": number, "manager_status": data, "response": response_text}
                return None

            result = self.poll_until("tmux-inject-response-ready", 180.0, response_ready)
            status_record = result["manager_status"].get("record") if isinstance(result.get("manager_status"), dict) else {}
            event_id = str(status_record.get("last_terminal_event_id") or "")
            events = status_record.get("events") if isinstance(status_record.get("events"), dict) else {}
            event = events.get(event_id) if isinstance(events.get(event_id), dict) else {}
            expected_prompt = tmux_manager.build_tmux_inject_wake_prompt(
                status_record,
                {
                    "event_id": event_id,
                    "wake_id": tmux_manager.tmux_inject_wake_id(event_id),
                    "job_id": event.get("job_id"),
                    "event_read_token": event.get("event_read_token"),
                },
            )
            notification = status_record.get("last_notification") if isinstance(status_record.get("last_notification"), dict) else {}
            if notification.get("prompt_sha256") != hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest():
                raise ScenarioFailure(self.current_scenario, "tmux-inject-prompt-hash", "wake prompt hash did not match the short prompt")
            return {
                "manager_id": manager_id,
                "job_id": job_id,
                "codex_pane_id": codex_pane_id,
                "number": result["number"],
                "response": result["response"],
                "last_terminal_event_id": event_id,
                "last_ack": status_record.get("last_ack"),
            }
        finally:
            if codex_pane_id:
                capture = self.tmux_pane_capture_text(codex_pane_id, lines=180)
                (self.workspace / "tmux-inject-target-capture.txt").write_text(capture, encoding="utf-8", errors="replace")
            self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="tmux-inject-manager-cancel", check=False)
            if manager_proc is not None and manager_args is not None and manager_proc.poll() is None:
                self.terminate_one_manager_process(manager_proc, manager_args, step="tmux-inject-manager-process-exit")
            self.run(["tmux", "kill-session", "-t", codex_session])
            if old_sdk_decision is None:
                self.env.pop("TMUX_SKILLS_CODEX_SDK_DECISION", None)
            else:
                self.env["TMUX_SKILLS_CODEX_SDK_DECISION"] = old_sdk_decision

    def scenario_manager_random_repeat_until_zero_one(self) -> dict[str, Any]:
        self.current_scenario = "manager-random-repeat-until-zero-one"
        manager_id = "manager-random-repeat"
        manager_proc, _start, manager_args, thread_id = self.start_bridge_manager(manager_id)
        attempts: list[dict[str, Any]] = []
        final_number: int | None = None
        final_status: dict[str, Any] | None = None
        try:
            for index in range(1, 11):
                seed = 0 if index == 1 else 2
                command = f'python3 -c "import random,time; time.sleep(1); random.seed({seed}); print(random.randint(0, 9))"'
                action = "submit" if index == 1 else "run-next"
                number, status = self.submit_bridge_random_job(manager_id, f"manager-random-repeat-{index}", command, action=action)
                attempts.append({"attempt": index, "number": number})
                final_number = number
                final_status = status
                if index >= 2 and number in {0, 1}:
                    break
            if final_number not in {0, 1} or len(attempts) < 2:
                raise ScenarioFailure(self.current_scenario, "manager-random-repeat-result", f"did not reach 0 or 1 after {len(attempts)} attempts: {attempts}")
            record = final_status.get("record") if isinstance(final_status, dict) and isinstance(final_status.get("record"), dict) else {}
            return {
                "manager_id": manager_id,
                "thread_id": thread_id,
                "attempts": attempts,
                "final_number": final_number,
                "manager_status": record.get("status"),
                "last_terminal_event_id": record.get("last_terminal_event_id"),
                "last_ack": record.get("last_ack"),
            }
        finally:
            self.terminate_one_manager_process(manager_proc, manager_args, step="manager-random-repeat-process-exit")

    def scenario_manager_start_reuses_live_process(self) -> dict[str, Any]:
        self.current_scenario = "manager-start-reuses-live-process"
        manager_id = "manager-reuse"
        first_job = "manager-reuse-first"
        second_job = "manager-reuse-second"
        first_output = self.workspace / "manager-reuse-first.out"
        second_output = self.workspace / "manager-reuse-second.out"
        manager_proc, _start, manager_args = self.start_manager_process(
            manager_id,
            first_job,
            "printf first-ok > manager-reuse-first.out",
        )
        self.wait_file(first_output, "first-ok", timeout=10.0)
        first_status = self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="succeeded", timeout=10.0)
        first_record = first_status.get("record") if isinstance(first_status.get("record"), dict) else {}
        first_manager_pid = first_record.get("manager_pid")
        first_manager_pane = first_record.get("manager_pane_id")
        first_worker_pane = first_record.get("worker_pane_id")
        panes_before = self.control(["list"], step="manager-reuse-list-before").json_data.get("panes", [])
        session_panes_before = [pane for pane in panes_before if pane.get("session_name") == self.session]
        second_start = self.control(
            [
                "manager",
                "start",
                "--manager-id",
                manager_id,
                "--job-id",
                second_job,
                "--command",
                "printf second-ok > manager-reuse-second.out",
                "--notify",
                "none",
                "--workspace",
                str(self.workspace),
                "--poll-seconds",
                "0.1",
            ],
            step="manager-reuse-second-start",
        )
        data = second_start.json_data if isinstance(second_start.json_data, dict) else {}
        if data.get("queued_on_existing_manager") is not True or data.get("start_process_mode") != "existing":
            raise ScenarioFailure(self.current_scenario, "manager-reuse-existing-process", "second manager start did not queue to the live manager", second_start)
        if manager_proc.poll() is not None:
            raise ScenarioFailure(self.current_scenario, "manager-reuse-original-process", "original manager process exited before second job")
        self.wait_file(second_output, "second-ok", timeout=10.0)
        final_status = self.wait_manager_status(manager_id, "waiting_for_codex", expected_job_status="succeeded", timeout=10.0)
        final_record = final_status.get("record") if isinstance(final_status.get("record"), dict) else {}
        if final_record.get("manager_pid") != first_manager_pid:
            raise ScenarioFailure(self.current_scenario, "manager-reuse-pid", "manager pid changed after second start")
        if final_record.get("manager_pane_id") != first_manager_pane or final_record.get("worker_pane_id") != first_worker_pane:
            raise ScenarioFailure(self.current_scenario, "manager-reuse-pane-ids", "manager or worker pane id changed after second start")
        panes_after = self.control(["list"], step="manager-reuse-list-after").json_data.get("panes", [])
        session_panes_after = [pane for pane in panes_after if pane.get("session_name") == self.session]
        if len(session_panes_after) != len(session_panes_before):
            raise ScenarioFailure(self.current_scenario, "manager-reuse-pane-count", "second manager start created or removed a pane")
        self.control(["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)], step="manager-reuse-cancel")
        self.collect_manager_process(manager_proc, manager_args, step="manager-reuse-process-exit")
        return {
            "manager_id": manager_id,
            "first_job": first_job,
            "second_job": second_job,
            "manager_pid": first_manager_pid,
            "manager_pane_id": first_manager_pane,
            "worker_pane_id": first_worker_pane,
        }

    def scenario_manager_cancel(self) -> dict[str, Any]:
        self.current_scenario = "manager-cancel"
        manager_id = "manager-cancel"
        job_id = "manager-cancel-job"
        manager_proc, start, manager_args = self.start_manager_process(
            manager_id,
            job_id,
            "sleep 10; printf should-not-finish > manager-cancel.out",
        )
        start_data = start.json_data if isinstance(start.json_data, dict) else {}
        worker_pane = str(start_data.get("worker_pane_id") or "")
        manager_pane = str(start_data.get("manager_pane_id") or "")
        self.wait_manager_status(manager_id, "running", timeout=10.0)
        first_cancel = self.control(
            ["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace)],
            step="manager-cancel-dashboard-only",
        )
        first_data = first_cancel.json_data if isinstance(first_cancel.json_data, dict) else {}
        if first_data.get("worker_stop_result") is not None:
            raise ScenarioFailure(self.current_scenario, "manager-cancel-no-stop", "default cancel attempted to stop the worker", first_cancel)
        self.collect_manager_process(manager_proc, manager_args, step="manager-cancel-process-exit", reset_tmux=False)
        time.sleep(0.3)
        running_status = self.job_status(job_id)
        status = (running_status.get("status") or {}).get("status")
        if status not in {"pending", "running"}:
            raise ScenarioFailure(self.current_scenario, "manager-cancel-worker-running", f"worker was not left running: {status}")
        if not self.tmux_pane_exists(worker_pane) or not self.tmux_pane_exists(manager_pane):
            raise ScenarioFailure(self.current_scenario, "manager-cancel-pane-preserved", "manager cancel closed a pane")
        stop_cancel = self.control(
            ["manager", "cancel", "--manager-id", manager_id, "--workspace", str(self.workspace), "--stop-worker"],
            step="manager-cancel-stop-worker",
        )
        stop_data = stop_cancel.json_data if isinstance(stop_cancel.json_data, dict) else {}
        worker_stop = stop_data.get("worker_stop_result") if isinstance(stop_data.get("worker_stop_result"), dict) else {}
        if worker_stop.get("sent") is not True:
            raise ScenarioFailure(self.current_scenario, "manager-cancel-stop-sent", "stop-worker did not send an interrupt", stop_cancel)
        stopped = self.wait_status(job_id, "stopped", timeout=10.0)
        self.reset_after_manager_process()
        return {
            "manager_id": manager_id,
            "job_id": job_id,
            "worker_pane_id": worker_pane,
            "manager_pane_id": manager_pane,
            "stopped_status": (stopped.get("status") or {}).get("status"),
        }

    def scenario_manager_process_exit_keeps_worker(self) -> dict[str, Any]:
        self.current_scenario = "manager-process-exit-keeps-worker"
        manager_id = "manager-exit"
        job_id = "manager-exit-job"
        output = self.workspace / "manager-exit.out"
        manager_proc, _start, manager_args = self.start_manager_process(
            manager_id,
            job_id,
            "sleep 1.5; printf worker-survived > manager-exit.out",
        )
        self.wait_manager_status(manager_id, "running", timeout=10.0)
        terminated = self.terminate_one_manager_process(manager_proc, manager_args, step="manager-process-terminate", reset_tmux=False)
        time.sleep(0.3)
        running_status = self.job_status(job_id)
        status = (running_status.get("status") or {}).get("status")
        if status not in {"pending", "running", "succeeded"}:
            raise ScenarioFailure(self.current_scenario, "worker-after-manager-exit", f"worker did not survive manager exit: {status}")
        self.wait_status(job_id, "succeeded", timeout=10.0)
        self.wait_file(output, "worker-survived", timeout=5.0)
        self.reset_tmux_session()
        return {
            "manager_id": manager_id,
            "job_id": job_id,
            "manager_returncode": terminated.returncode,
            "output": str(output),
        }

    def scenario_autopilot_repair_rerun(self) -> dict[str, Any]:
        self.current_scenario = "autopilot-repair-rerun"
        objective_id = "autopilot-e2e"
        first_job = f"{objective_id}-attempt-1"
        second_job = f"{objective_id}-attempt-2"
        output = self.workspace / "autopilot.out"
        self.jobs.extend([first_job, second_job])

        started = self.control(
            [
                "autopilot",
                "start",
                "--objective-id",
                objective_id,
                "--pane",
                str(self.pane),
                "--command",
                "python3 -c 'print(\"first-fail-\" + \"x\" * 5000); raise SystemExit(7)'",
                "--goal",
                "Finish autopilot E2E after a bounded repair",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-start",
        )
        start_data = started.json_data if isinstance(started.json_data, dict) else {}
        if start_data.get("started") is not True:
            raise ScenarioFailure(self.current_scenario, "autopilot-started", "autopilot start did not start first attempt", started)
        self.wait_status(first_job, "failed", timeout=10.0)

        tick = self.control(
            [
                "autopilot",
                "tick",
                "--objective-id",
                objective_id,
                "--for-agent",
                "--json",
                "--max-chars",
                "1200",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-tick-repair",
        )
        tick_data = tick.json_data if isinstance(tick.json_data, dict) else {}
        if tick_data.get("action") != "repair" or tick_data.get("status") != "repairing":
            raise ScenarioFailure(self.current_scenario, "autopilot-repair-action", "tick did not claim repair", tick)
        if first_job not in str(tick_data.get("evidence_paths") or ""):
            raise ScenarioFailure(self.current_scenario, "autopilot-evidence", "tick did not expose failed attempt evidence", tick)
        summary = tick_data.get("attempt_summary") if isinstance(tick_data.get("attempt_summary"), dict) else {}
        if len(str(summary.get("last_output_tail") or "")) > 1200 or not summary.get("truncated"):
            raise ScenarioFailure(self.current_scenario, "autopilot-summary-bound", "tick summary was not bounded and marked truncated", tick)
        evidence_commands = tick_data.get("evidence_commands") if isinstance(tick_data.get("evidence_commands"), list) else []
        if not any("--kind log" in str(item.get("command") if isinstance(item, dict) else item) for item in evidence_commands):
            raise ScenarioFailure(self.current_scenario, "autopilot-evidence-command", "tick did not expose bounded log evidence command", tick)

        log_evidence = self.control(
            [
                "autopilot",
                "evidence",
                "--objective-id",
                objective_id,
                "--kind",
                "log",
                "--max-chars",
                "8000",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-log-evidence",
        )
        log_evidence_data = log_evidence.json_data if isinstance(log_evidence.json_data, dict) else {}
        if "first-fail-" not in str(log_evidence_data.get("content") or ""):
            raise ScenarioFailure(self.current_scenario, "autopilot-log-evidence-content", "bounded log evidence did not include failure output", log_evidence)

        duplicate_tick = self.control(
            [
                "autopilot",
                "tick",
                "--objective-id",
                objective_id,
                "--for-agent",
                "--json",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-tick-duplicate",
        )
        duplicate_data = duplicate_tick.json_data if isinstance(duplicate_tick.json_data, dict) else {}
        if duplicate_data.get("action") != "no_action" or duplicate_data.get("reason") != "repair already claimed":
            raise ScenarioFailure(self.current_scenario, "autopilot-duplicate-claim", "duplicate tick did not respect repair lease", duplicate_tick)

        rerun = self.control(
            [
                "autopilot",
                "rerun",
                "--objective-id",
                objective_id,
                "--command",
                "printf autopilot-ok > autopilot.out",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-rerun",
        )
        rerun_data = rerun.json_data if isinstance(rerun.json_data, dict) else {}
        if rerun_data.get("action") != "rerun_started":
            raise ScenarioFailure(self.current_scenario, "autopilot-rerun-started", "rerun did not start second attempt", rerun)
        self.wait_status(second_job, "succeeded", timeout=10.0)
        self.wait_file(output, "autopilot-ok", timeout=5.0)

        final_tick = self.control(
            [
                "autopilot",
                "tick",
                "--objective-id",
                objective_id,
                "--for-agent",
                "--json",
                "--workspace",
                str(self.workspace),
            ],
            step="autopilot-final-tick",
        )
        final_data = final_tick.json_data if isinstance(final_tick.json_data, dict) else {}
        if final_data.get("action") != "completed" or final_data.get("status") != "succeeded":
            raise ScenarioFailure(self.current_scenario, "autopilot-completed", "final tick did not complete objective", final_tick)
        if "evidence_commands" in final_data:
            raise ScenarioFailure(self.current_scenario, "autopilot-terminal-evidence", "completed tick should not request extra evidence", final_tick)

        prompt = self.control(
            ["autopilot", "heartbeat-prompt", "--objective-id", objective_id, "--workspace", str(self.workspace)],
            step="autopilot-heartbeat-prompt",
        )
        if "autopilot tick" not in prompt.stdout or "--max-chars 1200" not in prompt.stdout or "heartbeat can be paused or removed" not in prompt.stdout:
            raise ScenarioFailure(self.current_scenario, "autopilot-prompt", "heartbeat prompt did not include wake contract", prompt)
        return {"objective_id": objective_id, "first_job": first_job, "second_job": second_job, "final_action": final_data.get("action")}


SCENARIO_METHODS = {
    "idle-continuation": Harness.scenario_idle_continuation,
    "status-chain": Harness.scenario_status_chain,
    "status-chain-waits-for-busy-pane": Harness.scenario_status_chain_waits_for_busy_pane,
    "concurrent-duplicate-race": Harness.scenario_concurrent_duplicate_race,
    "duplicate-block": Harness.scenario_duplicate_block,
    "preflight-strict": Harness.scenario_preflight_strict,
    "watch-visibility": Harness.scenario_watch_visibility,
    "capture-strips-ansi": Harness.scenario_capture_strips_ansi,
    "busy-pane-wait": Harness.scenario_busy_pane_wait,
    "queue-command-file": Harness.scenario_queue_command_file,
    "status-fail-blocks": Harness.scenario_status_fail_blocks,
    "allow-duplicate": Harness.scenario_allow_duplicate,
    "watch-duplicate-block": Harness.scenario_watch_duplicate_block,
    "watch-concurrent-race": Harness.scenario_watch_concurrent_race,
    "replace-same-job-only": Harness.scenario_replace_same_job_only,
    "cancel-active-queue": Harness.scenario_cancel_active_queue,
    "stale-gc-recovery": Harness.scenario_stale_gc_recovery,
    "corrupted-state-degrades": Harness.scenario_corrupted_state_degrades,
    "replace-rejects-foreign-pid": Harness.scenario_replace_rejects_foreign_pid,
    "pane-missing-failure": Harness.scenario_pane_missing_failure,
    "status-timeout-blocks": Harness.scenario_status_timeout_blocks,
    "pane-dies-mid-wait": Harness.scenario_pane_dies_mid_wait,
    "task-followup-flow": Harness.scenario_task_followup_flow,
    "autopilot-repair-rerun": Harness.scenario_autopilot_repair_rerun,
    "manager-visible-success": Harness.scenario_manager_visible_success,
    "manager-visible-failure": Harness.scenario_manager_visible_failure,
    "manager-run-next": Harness.scenario_manager_run_next,
    "manager-multi-pane": Harness.scenario_manager_multi_pane,
    "manager-tui-delete-completed": Harness.scenario_manager_tui_delete_completed,
    "manager-bridge-random-notify": Harness.scenario_manager_bridge_random_notify,
    "manager-tmux-inject-wakes-current-codex": Harness.scenario_manager_tmux_inject_wakes_current_codex,
    "manager-random-repeat-until-zero-one": Harness.scenario_manager_random_repeat_until_zero_one,
    "manager-start-reuses-live-process": Harness.scenario_manager_start_reuses_live_process,
    "manager-cancel": Harness.scenario_manager_cancel,
    "manager-process-exit-keeps-worker": Harness.scenario_manager_process_exit_keeps_worker,
}


def selected_scenarios(value: str) -> list[str]:
    if value == "smoke":
        return list(SMOKE_SCENARIOS)
    if value == "all":
        return list(ALL_SCENARIOS)
    return [value]


def skip_result(json_output: bool) -> int:
    payload = {"status": "skipped", "reason": "tmux is not installed or not on PATH"}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"SKIP: {payload['reason']}")
    return SKIP_EXIT_CODE


def unknown_scenario_summary(names: list[str], unknown: list[str]) -> dict[str, Any]:
    return {
        "status": "failed",
        "scenario_count": len(names),
        "selected_scenarios": names,
        "results": [
            {
                "scenario": name,
                "status": "failed",
                "elapsed_seconds": 0.0,
                "failure": {
                    "scenario": name,
                    "step": "unknown-scenario",
                    "message": f"unknown scenario: {name}",
                },
            }
            for name in unknown
        ],
        "artifacts_kept": False,
        "artifact_dir": None,
        "cleanup": None,
    }


def run_scenarios(names: list[str], *, keep_artifacts: bool, keep_going: bool = False) -> tuple[int, dict[str, Any]]:
    unknown = [name for name in names if name not in SCENARIO_METHODS]
    if unknown:
        return 1, unknown_scenario_summary(names, unknown)

    harness = Harness(keep_artifacts=keep_artifacts)
    results: list[dict[str, Any]] = []
    status = "passed"
    exit_code = 0
    cleanup_info: dict[str, Any] | None = None
    try:
        try:
            harness.setup_tmux()
        except ScenarioFailure as exc:
            status = "failed"
            exit_code = 1
            results.append(scenario_failure_result(harness, scenario=exc.scenario, failure=exc, elapsed_seconds=0.0))
        except Exception as exc:
            failure = unexpected_failure("setup", exc)
            status = "failed"
            exit_code = 1
            results.append(scenario_failure_result(harness, scenario=failure.scenario, failure=failure, elapsed_seconds=0.0))
        else:
            for name in names:
                method = SCENARIO_METHODS[name]
                started = time.monotonic()
                should_stop = False
                try:
                    harness.before_scenario(name)
                    detail = method(harness)
                    results.append({"scenario": name, "status": "passed", "elapsed_seconds": round(time.monotonic() - started, 3), "detail": detail})
                except ScenarioFailure as exc:
                    status = "failed"
                    exit_code = 1
                    results.append(scenario_failure_result(harness, scenario=name, failure=exc, elapsed_seconds=round(time.monotonic() - started, 3)))
                    if not keep_going:
                        should_stop = True
                except Exception as exc:
                    status = "failed"
                    exit_code = 1
                    failure = unexpected_failure(name, exc)
                    results.append(scenario_failure_result(harness, scenario=name, failure=failure, elapsed_seconds=round(time.monotonic() - started, 3)))
                    if not keep_going:
                        should_stop = True
                try:
                    harness.after_scenario()
                except ScenarioFailure as exc:
                    status = "failed"
                    exit_code = 1
                    results.append(scenario_failure_result(harness, scenario=exc.scenario, failure=exc, elapsed_seconds=0.0))
                    should_stop = True
                except Exception as exc:
                    status = "failed"
                    exit_code = 1
                    failure = unexpected_failure(name, exc, step="after-scenario")
                    results.append(scenario_failure_result(harness, scenario=name, failure=failure, elapsed_seconds=0.0))
                    should_stop = True
                try:
                    harness.before_scenario("teardown-check")
                except ScenarioFailure as exc:
                    status = "failed"
                    exit_code = 1
                    results.append(scenario_failure_result(harness, scenario=exc.scenario, failure=exc, elapsed_seconds=0.0))
                    should_stop = True
                except Exception as exc:
                    status = "failed"
                    exit_code = 1
                    failure = unexpected_failure(name, exc, step="teardown-check")
                    results.append(scenario_failure_result(harness, scenario=name, failure=failure, elapsed_seconds=0.0))
                    should_stop = True
                if should_stop:
                    break
    finally:
        try:
            if status == "passed" or not keep_artifacts:
                cleanup_info = harness.cleanup(remove_artifacts=True)
            else:
                harness.cancel_active_jobs()
                harness.run(["tmux", "kill-session", "-t", harness.session])
                harness.run(["tmux", "kill-server"])
                harness.remove_repo_runtime_artifacts()
                cleanup_info = {
                    "session_absent": not harness.session_exists(),
                    "server_absent": not harness.server_exists(),
                    "temp_dir_removed": False,
                    "repo_runtime_artifacts": harness.repo_runtime_artifacts(),
                    "removed_repo_runtime_artifacts": harness.removed_repo_artifacts,
                    "artifact_dir": str(harness.base_dir),
                }
        except Exception as exc:
            status = "failed"
            exit_code = 1
            cleanup_info = cleanup_failure_info(harness, exc)
            results.append(
                {
                    "scenario": "e2e-cleanup-verification",
                    "status": "failed",
                    "elapsed_seconds": 0.0,
                    "failure": {
                        "scenario": "e2e-cleanup-verification",
                        "step": "cleanup-exception",
                        "message": f"{type(exc).__name__}: {exc}",
                        "cleanup": cleanup_info,
                    },
                }
            )

    if status == "passed" and cleanup_info:
        cleanup_errors: list[str] = []
        if not cleanup_info.get("session_absent"):
            cleanup_errors.append("test tmux session still exists")
        if not cleanup_info.get("server_absent"):
            cleanup_errors.append("test tmux server still exists")
        if not cleanup_info.get("temp_dir_removed"):
            cleanup_errors.append("temp directory still exists")
        if cleanup_info.get("repo_runtime_artifacts"):
            cleanup_errors.append(f"repo runtime artifacts remain: {cleanup_info['repo_runtime_artifacts']}")
        if cleanup_errors:
            status = "failed"
            exit_code = 1
            results.append(
                {
                    "scenario": "e2e-cleanup-verification",
                    "status": "failed",
                    "elapsed_seconds": 0.0,
                    "failure": {
                        "scenario": "e2e-cleanup-verification",
                        "step": "post-run-cleanup",
                        "message": "; ".join(cleanup_errors),
                        "cleanup": cleanup_info,
                    },
                }
            )

    return exit_code, {
        "status": status,
        "scenario_count": len(names),
        "selected_scenarios": names,
        "results": results,
        "artifacts_kept": bool(keep_artifacts and cleanup_info and not cleanup_info.get("temp_dir_removed")),
        "artifact_dir": str(harness.base_dir) if keep_artifacts and cleanup_info and not cleanup_info.get("temp_dir_removed") else None,
        "cleanup": cleanup_info,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real-use E2E scenarios for tmux-skills")
    choices = ["smoke", "all", *SCENARIO_METHODS.keys()]
    parser.add_argument(
        "--scenario",
        choices=choices,
        default="smoke",
        help="Scenario group or named scenario to run (default: smoke)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep temp artifacts after a failure")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue after scenario-body failures; stop on teardown or cleanup failures",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if shutil.which("tmux") is None:
        return skip_result(args.json)

    names = selected_scenarios(args.scenario)
    exit_code, summary = run_scenarios(names, keep_artifacts=args.keep_artifacts, keep_going=args.keep_going)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif exit_code == 0:
        print(f"PASS: {', '.join(names)}")
    else:
        failed = next((result for result in summary["results"] if result["status"] == "failed"), None)
        print(json.dumps(failed or summary, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
