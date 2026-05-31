#!/usr/bin/env python3
"""Real-use E2E scenarios for tmux-skills managed workers."""

from __future__ import annotations

import argparse
import json
import os
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


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "tmux_control.py"
HOOK = ROOT / "scripts" / "codex_tmux_hook.py"
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
    "stop-hook-blocks-terminal",
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
        self.removed_repo_artifacts: list[str] = []
        self.remove_repo_runtime_artifacts()

    def run(self, args: list[str], *, cwd: Path = ROOT, input_text: str | None = None) -> CommandResult:
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
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = timeout_output_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = append_timeout_message(timeout_output_text(getattr(exc, "stderr", None)), COMMAND_TIMEOUT_SECONDS)
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

    def require_success(self, command: CommandResult, *, step: str) -> CommandResult:
        if command.returncode != 0:
            raise ScenarioFailure(self.current_scenario, step, f"command failed with exit {command.returncode}", command)
        return command

    def control(self, args: list[str], *, step: str, check: bool = True) -> CommandResult:
        command = self.run(self.control_args(args))
        if check:
            self.require_success(command, step=step)
        return command

    def hook_context(self, *, step: str) -> CommandResult:
        command = self.run(
            [
                sys.executable,
                str(HOOK),
                "context",
                "--event",
                "UserPromptSubmit",
                "--workspace",
                str(self.workspace),
            ],
            input_text="{}",
        )
        self.require_success(command, step=step)
        return command

    def hook_stop(self, *, step: str) -> CommandResult:
        command = self.run(
            [
                sys.executable,
                str(HOOK),
                "stop",
                "--workspace",
                str(self.workspace),
            ],
            input_text="{}",
        )
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

    def before_scenario(self, name: str) -> None:
        self.current_scenario = name
        self.cancel_active_jobs()
        self.interrupt_pane()

    def after_scenario(self) -> None:
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

        def hook_has_job() -> bool:
            data = self.hook_context(step="hook-context").json_data or {}
            context = ((data.get("hookSpecificOutput") or {}).get("additionalContext") or "")
            return f"managed job {job_id}: running" in context

        self.poll_until("hook-active-watch", 3.0, hook_has_job)
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

        context = self.hook_context(step="hook-context-corrupt")
        if "Traceback (most recent call last)" in context.stderr:
            raise ScenarioFailure(self.current_scenario, "hook-context-traceback", "hook context emitted a traceback", context)
        context_data = context.json_data if isinstance(context.json_data, dict) else {}
        additional_context = str(((context_data.get("hookSpecificOutput") or {}).get("additionalContext") or ""))
        if "unreadable" not in additional_context:
            raise ScenarioFailure(self.current_scenario, "hook-context-unreadable", "hook context did not report unreadable state files", context)
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
        context = self.hook_context(step="hook-context-ready").json_data or {}
        additional = ((context.get("hookSpecificOutput") or {}).get("additionalContext") or "")
        if "ready task" not in additional or instruction not in additional:
            raise ScenarioFailure(self.current_scenario, "hook-ready-task", "hook context did not expose the ready follow-up task")
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

    def scenario_stop_hook_blocks_terminal(self) -> dict[str, Any]:
        self.current_scenario = "stop-hook-blocks-terminal"
        job_id = "stop-hook-e2e"
        output = self.workspace / "stop-hook.out"

        for index in range(30):
            drained = self.hook_stop(step=f"drain-stop-{index}")
            data = drained.json_data if isinstance(drained.json_data, dict) else {}
            if data.get("decision") != "block":
                break
            if "ready task" in str(data.get("reason") or ""):
                raise ScenarioFailure(self.current_scenario, "drain-ready-task", "ready task interfered with stop hook terminal drain", drained)
        else:
            raise ScenarioFailure(self.current_scenario, "drain-stop-hooks", "could not drain existing terminal stop notifications")

        self.jobs.append(job_id)
        self.control(
            [
                "run",
                "--pane",
                str(self.pane),
                "--job-id",
                job_id,
                "--command",
                "printf stop-ok > stop-hook.out",
                "--workspace",
                str(self.workspace),
            ],
            step="run-terminal-job",
        )
        self.wait_status(job_id, "succeeded", timeout=10.0)
        self.wait_file(output, "stop-ok", timeout=5.0)
        first = self.hook_stop(step="stop-first")
        first_data = first.json_data if isinstance(first.json_data, dict) else {}
        first_reason = str(first_data.get("reason") or "")
        if first_data.get("decision") != "block" or "terminal event" not in first_reason:
            raise ScenarioFailure(self.current_scenario, "stop-first-block", "first stop hook call did not block on terminal event", first)
        second = self.hook_stop(step="stop-second")
        second_data = second.json_data if isinstance(second.json_data, dict) else {}
        if second_data.get("decision") == "block":
            raise ScenarioFailure(self.current_scenario, "stop-second-ack", "second stop hook call blocked after ack", second)
        return {"job_id": job_id, "first_decision": first_data.get("decision"), "second": second_data}


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
    "stop-hook-blocks-terminal": Harness.scenario_stop_hook_blocks_terminal,
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
