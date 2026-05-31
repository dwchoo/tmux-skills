from __future__ import annotations

import io
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import e2e_real_use
import tmux_control


DOC = Path(__file__).resolve().parents[1] / "docs" / "real-use-e2e.md"
README = Path(__file__).resolve().parents[1] / "README.md"
SKILL_DOC = Path(__file__).resolve().parents[1] / "SKILL.md"


def readme_verify_commands() -> list[str]:
    text = README.read_text(encoding="utf-8")
    verify_section = text.split("## Verify", 1)[1]
    block = verify_section.split("```bash", 1)[1].split("```", 1)[0]
    return [line for line in block.splitlines() if line]


def without_leading_env(command: str) -> list[str]:
    argv = shlex.split(command)
    if argv and "=" in argv[0] and not argv[0].startswith("-"):
        return argv[1:]
    return argv


def scenario_matrix_rows() -> list[list[str]]:
    text = DOC.read_text(encoding="utf-8")
    matrix = text.split("## Scenario Matrix", 1)[1].split("## Scenario Design Rules", 1)[0]
    rows: list[list[str]] = []
    for line in matrix.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == "---":
            continue
        rows.append(cells)
    return rows


class E2ERealUseTests(unittest.TestCase):
    def test_all_scenarios_include_every_named_scenario(self) -> None:
        self.assertEqual(set(e2e_real_use.ALL_SCENARIOS), set(e2e_real_use.SCENARIO_METHODS))

    def test_smoke_and_full_only_do_not_overlap(self) -> None:
        self.assertFalse(set(e2e_real_use.SMOKE_SCENARIOS) & set(e2e_real_use.FULL_ONLY_SCENARIOS))

    def test_real_use_docs_scenario_groups_match_code(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        smoke_section = text.split("Smoke scenarios:", 1)[1].split("Full-only scenarios:", 1)[0]
        full_section = text.split("Full-only scenarios:", 1)[1].split("## Scenario Matrix", 1)[0]
        matrix_names = re.findall(r"^\| `([^`]+)` \|", text, flags=re.MULTILINE)

        self.assertIn(f"`smoke` has {len(e2e_real_use.SMOKE_SCENARIOS)} scenarios.", text)
        self.assertIn(f"`all` has {len(e2e_real_use.ALL_SCENARIOS)} scenarios.", text)
        self.assertEqual(re.findall(r"^- `([^`]+)`", smoke_section, flags=re.MULTILINE), e2e_real_use.SMOKE_SCENARIOS)
        self.assertEqual(re.findall(r"^- `([^`]+)`", full_section, flags=re.MULTILINE), e2e_real_use.FULL_ONLY_SCENARIOS)
        self.assertEqual(len(matrix_names), len(e2e_real_use.ALL_SCENARIOS))
        self.assertEqual(set(matrix_names), set(e2e_real_use.ALL_SCENARIOS))

    def test_real_use_scenario_matrix_has_required_columns(self) -> None:
        rows = scenario_matrix_rows()
        header, scenario_rows = rows[0], rows[1:]

        self.assertEqual(header, ["Scenario", "Workflow covered", "Setup", "Action", "Required assertions"])
        self.assertEqual(len(scenario_rows), len(e2e_real_use.ALL_SCENARIOS))
        for row in scenario_rows:
            with self.subTest(row=row):
                self.assertEqual(len(row), 5)
                scenario, workflow, setup, action, assertions = row
                self.assertRegex(scenario, r"^`[^`]+`$")
                self.assertIn(scenario.strip("`"), e2e_real_use.ALL_SCENARIOS)
                for cell in (workflow, setup, action, assertions):
                    self.assertTrue(cell)
                    self.assertNotEqual(cell.lower(), "tbd")

    def test_real_use_doc_commands_match_public_parser(self) -> None:
        parser = e2e_real_use.build_parser()
        command_lines = [
            line
            for line in DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("python3 scripts/e2e_real_use.py ")
        ]

        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(line=line):
                argv = shlex.split(line)
                self.assertEqual(argv[:2], ["python3", "scripts/e2e_real_use.py"])
                parser.parse_args(argv[2:])

    def test_skill_quick_reference_e2e_commands_match_public_parser(self) -> None:
        parser = e2e_real_use.build_parser()
        command_lines = [
            line
            for line in SKILL_DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("python scripts/e2e_real_use.py ")
        ]

        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(line=line):
                argv = shlex.split(line)
                self.assertEqual(argv[:2], ["python", "scripts/e2e_real_use.py"])
                parser.parse_args(argv[2:])

    def test_readme_verify_commands_match_public_interfaces(self) -> None:
        e2e_parser = e2e_real_use.build_parser()
        control_parser = tmux_control.build_parser()
        commands = readme_verify_commands()

        self.assertEqual(
            commands,
            [
                "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover",
                "PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/*.py",
                "PYTHONDONTWRITEBYTECODE=1 python3 scripts/e2e_real_use.py --scenario smoke --json",
                "PYTHONDONTWRITEBYTECODE=1 python3 scripts/e2e_real_use.py --scenario all --json",
                "git diff --check",
                "python3 scripts/tmux_control.py --help",
                "python3 scripts/tmux_control.py list",
            ],
        )
        for command in commands:
            with self.subTest(command=command):
                argv = without_leading_env(command)
                if argv[:2] == ["python3", "scripts/e2e_real_use.py"]:
                    e2e_parser.parse_args(argv[2:])
                elif argv[:2] == ["python3", "scripts/tmux_control.py"]:
                    if argv[2:] == ["--help"]:
                        with self.assertRaises(SystemExit) as raised:
                            with mock.patch.object(tmux_control.sys, "stdout", io.StringIO()):
                                control_parser.parse_args(argv[2:])
                        self.assertEqual(raised.exception.code, 0)
                    else:
                        control_parser.parse_args(argv[2:])
                elif argv[:3] == ["python3", "-m", "unittest"]:
                    self.assertEqual(argv[3:], ["discover"])
                elif argv[:3] == ["python3", "-m", "py_compile"]:
                    self.assertEqual(argv[3:], ["scripts/*.py"])
                else:
                    self.assertEqual(argv, ["git", "diff", "--check"])

    def test_real_use_docs_describe_failure_safety_fields(self) -> None:
        text = DOC.read_text(encoding="utf-8")

        self.assertIn("`diagnostics_error`", text)
        self.assertIn("`cleanup_error`", text)
        self.assertIn("`e2e-cleanup-verification`", text)
        self.assertIn("`server_absent`", text)
        self.assertIn("`worker_pids_signalled`", text)
        self.assertIn("isolated tmux server has been stopped", text)
        self.assertIn("Teardown, isolation, or cleanup failures still stop the run", text)

    def test_real_use_docs_and_help_describe_default_scenario(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        help_text = e2e_real_use.build_parser().format_help()

        self.assertIn("`--scenario smoke` runs the fast real-use set by default.", text)
        self.assertIn("Scenario group or named scenario", help_text)
        self.assertRegex(help_text, r"default:\s+smoke")

    def test_main_json_prints_full_summary(self) -> None:
        summary = {
            "status": "failed",
            "scenario_count": 1,
            "selected_scenarios": ["idle-continuation"],
            "results": [{"scenario": "idle-continuation", "status": "failed"}],
            "cleanup": {"session_absent": True, "server_absent": True},
        }
        argv = ["e2e_real_use.py", "--scenario", "idle-continuation", "--json"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value="/usr/bin/tmux"):
                with mock.patch.object(e2e_real_use, "run_scenarios", return_value=(1, summary)) as run_scenarios:
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()) as stdout:
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), summary)
        run_scenarios.assert_called_once_with(["idle-continuation"], keep_artifacts=False, keep_going=False)

    def test_main_forwards_all_scenarios_and_lifecycle_flags(self) -> None:
        summary = {
            "status": "passed",
            "scenario_count": len(e2e_real_use.ALL_SCENARIOS),
            "selected_scenarios": e2e_real_use.ALL_SCENARIOS,
            "results": [],
            "cleanup": {"session_absent": True, "server_absent": True},
        }
        argv = ["e2e_real_use.py", "--scenario", "all", "--keep-artifacts", "--keep-going", "--json"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value="/usr/bin/tmux"):
                with mock.patch.object(e2e_real_use, "run_scenarios", return_value=(0, summary)) as run_scenarios:
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()):
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, 0)
        run_scenarios.assert_called_once_with(e2e_real_use.ALL_SCENARIOS, keep_artifacts=True, keep_going=True)

    def test_main_text_success_prints_pass_line(self) -> None:
        summary = {
            "status": "passed",
            "scenario_count": 1,
            "selected_scenarios": ["idle-continuation"],
            "results": [{"scenario": "idle-continuation", "status": "passed"}],
            "cleanup": {"session_absent": True, "server_absent": True},
        }
        argv = ["e2e_real_use.py", "--scenario", "idle-continuation"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value="/usr/bin/tmux"):
                with mock.patch.object(e2e_real_use, "run_scenarios", return_value=(0, summary)):
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()) as stdout:
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "PASS: idle-continuation\n")

    def test_main_text_failure_prints_first_failed_result(self) -> None:
        failed = {"scenario": "idle-continuation", "status": "failed", "failure": {"step": "check", "message": "nope"}}
        summary = {
            "status": "failed",
            "scenario_count": 2,
            "selected_scenarios": ["idle-continuation", "watch-visibility"],
            "results": [{"scenario": "watch-visibility", "status": "passed"}, failed],
            "cleanup": {"session_absent": True, "server_absent": True},
        }
        argv = ["e2e_real_use.py", "--scenario", "idle-continuation"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value="/usr/bin/tmux"):
                with mock.patch.object(e2e_real_use, "run_scenarios", return_value=(1, summary)):
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()) as stdout:
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), failed)

    def test_main_text_skip_when_tmux_missing(self) -> None:
        argv = ["e2e_real_use.py", "--scenario", "idle-continuation"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value=None):
                with mock.patch.object(e2e_real_use, "run_scenarios") as run_scenarios:
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()) as stdout:
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, e2e_real_use.SKIP_EXIT_CODE)
        self.assertEqual(stdout.getvalue(), "SKIP: tmux is not installed or not on PATH\n")
        run_scenarios.assert_not_called()

    def test_main_json_skip_when_tmux_missing(self) -> None:
        argv = ["e2e_real_use.py", "--scenario", "idle-continuation", "--json"]
        with mock.patch.object(e2e_real_use.sys, "argv", argv):
            with mock.patch.object(e2e_real_use.shutil, "which", return_value=None):
                with mock.patch.object(e2e_real_use, "run_scenarios") as run_scenarios:
                    with mock.patch.object(e2e_real_use.sys, "stdout", io.StringIO()) as stdout:
                        exit_code = e2e_real_use.main()

        self.assertEqual(exit_code, e2e_real_use.SKIP_EXIT_CODE)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "skipped", "reason": "tmux is not installed or not on PATH"})
        run_scenarios.assert_not_called()

    def test_active_managed_job_normalizes_status_token(self) -> None:
        self.assertTrue(e2e_real_use.is_active_managed_job({"status": " Running "}))
        self.assertTrue(e2e_real_use.is_active_managed_job({"status": " Waiting_Status\n"}))
        self.assertFalse(e2e_real_use.is_active_managed_job({"status": "submitted"}))
        self.assertFalse(e2e_real_use.is_active_managed_job({"status": "running", "stale": True}))

    def test_harness_run_returns_timeout_result(self) -> None:
        harness = e2e_real_use.Harness.__new__(e2e_real_use.Harness)
        harness.env = {}
        harness.last_command = None
        timeout = e2e_real_use.subprocess.TimeoutExpired(
            ["slow"],
            e2e_real_use.COMMAND_TIMEOUT_SECONDS,
            output="partial",
            stderr="still waiting",
        )

        with mock.patch.object(e2e_real_use.subprocess, "run", side_effect=timeout):
            result = harness.run(["slow"])

        self.assertEqual(result.returncode, e2e_real_use.COMMAND_TIMEOUT_EXIT_CODE)
        self.assertEqual(result.stdout, "partial")
        self.assertIn("still waiting", result.stderr)
        self.assertIn("timed out after", result.stderr)
        self.assertIs(harness.last_command, result)

    def test_collect_process_kills_timed_out_child(self) -> None:
        class TimeoutProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.killed = False

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                if timeout is not None:
                    raise e2e_real_use.subprocess.TimeoutExpired(["slow"], timeout)
                self.returncode = -9
                return ('{"ok": true}', "partial stderr")

            def kill(self) -> None:
                self.killed = True

        harness = e2e_real_use.Harness.__new__(e2e_real_use.Harness)
        harness.last_command = None
        proc = TimeoutProcess()

        result = harness.collect_process(proc, ["watch"])

        self.assertTrue(proc.killed)
        self.assertEqual(result.returncode, e2e_real_use.COMMAND_TIMEOUT_EXIT_CODE)
        self.assertEqual(result.json_data, {"ok": True})
        self.assertIn("partial stderr", result.stderr)
        self.assertIn("timed out after", result.stderr)
        self.assertIs(harness.last_command, result)

    def test_cancel_active_jobs_prunes_terminal_tracked_ids(self) -> None:
        harness = e2e_real_use.Harness.__new__(e2e_real_use.Harness)
        harness.workspace = Path("/tmp/tmux-skills-workspace")
        harness.jobs = ["terminal-job", "still-active"]
        cancelled: list[str] = []

        def fake_control(args: list[str], *, step: str, check: bool = True) -> e2e_real_use.CommandResult:
            cancelled.append(args[3])
            return e2e_real_use.CommandResult(args=args, returncode=0, stdout="{}", stderr="", json_data={})

        active_passes = [
            [{"job_id": "still-active"}],
            [{"job_id": "still-active"}],
        ]
        with mock.patch.object(harness, "active_jobs", side_effect=active_passes):
            with mock.patch.object(harness, "control", side_effect=fake_control):
                harness.cancel_active_jobs()

        self.assertEqual(cancelled, ["terminal-job", "still-active"])
        self.assertEqual(harness.jobs, ["still-active"])

    def test_tmux_queue_pids_for_workspace_filters_ps_output(self) -> None:
        stdout = "\n".join(
            [
                "101 python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id a --workspace /tmp/ws",
                "102 python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id b --workspace /tmp/other",
                "103 python3 /repo/scripts/tmux_job.py exec --workspace /tmp/ws",
                "104 python3 /repo/scripts/tmux_queue.py queue-after-status --workspace=/tmp/ws",
                "105 python3 /repo/scripts/tmux_queue.py queue-after-idle --workspace /tmp/ws2",
                "not-a-pid python3 /repo/scripts/tmux_queue.py queue-after-idle --workspace /tmp/ws",
            ]
        )
        completed = e2e_real_use.subprocess.CompletedProcess(["ps"], 0, stdout=stdout, stderr="")

        with mock.patch.object(e2e_real_use.subprocess, "run", return_value=completed):
            with mock.patch.object(e2e_real_use.os, "getpid", return_value=999):
                pids = e2e_real_use.tmux_queue_pids_for_workspace(Path("/tmp/ws"))

        self.assertEqual(pids, [101, 104])

    def test_tmux_queue_pids_for_workspace_matches_resolved_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real"
            real.mkdir()
            alias = base / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            workspace = alias / "workspace"
            workspace.mkdir()
            resolved_workspace = workspace.resolve()
            stdout = (
                "201 python3 /repo/scripts/tmux_queue.py queue-after-idle "
                f"--job-id a --workspace {resolved_workspace}"
            )
            completed = e2e_real_use.subprocess.CompletedProcess(["ps"], 0, stdout=stdout, stderr="")

            with mock.patch.object(e2e_real_use.subprocess, "run", return_value=completed):
                with mock.patch.object(e2e_real_use.os, "getpid", return_value=999):
                    pids = e2e_real_use.tmux_queue_pids_for_workspace(workspace)

        self.assertEqual(pids, [201])

    def test_terminate_workspace_workers_signals_matching_pids(self) -> None:
        harness = e2e_real_use.Harness.__new__(e2e_real_use.Harness)
        harness.workspace = Path("/tmp/ws")

        with mock.patch.object(e2e_real_use, "tmux_queue_pids_for_workspace", return_value=[101, 102]):
            with mock.patch.object(e2e_real_use.os, "kill") as kill:
                pids = harness.terminate_workspace_workers()

        self.assertEqual(pids, [101, 102])
        kill.assert_has_calls(
            [
                mock.call(101, e2e_real_use.signal.SIGTERM),
                mock.call(102, e2e_real_use.signal.SIGTERM),
            ]
        )

    def test_run_scenarios_rejects_unknown_names_before_setup(self) -> None:
        with mock.patch.object(e2e_real_use, "Harness") as harness:
            exit_code, summary = e2e_real_use.run_scenarios(["missing-scenario"], keep_artifacts=False)

        harness.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["cleanup"], None)
        self.assertEqual(summary["results"][0]["failure"]["step"], "unknown-scenario")
        self.assertIn("missing-scenario", summary["results"][0]["failure"]["message"])

    def test_start_queue_idle_builds_parser_valid_control_args(self) -> None:
        harness = e2e_real_use.Harness.__new__(e2e_real_use.Harness)
        harness.jobs = []
        harness.pane = "%42"
        harness.workspace = Path("/tmp/tmux-skills-workspace")
        captured: dict[str, object] = {}

        def fake_control(args: list[str], *, step: str, check: bool = True) -> e2e_real_use.CommandResult:
            captured["args"] = args
            captured["step"] = step
            captured["check"] = check
            return e2e_real_use.CommandResult(args=[], returncode=0, stdout="{}", stderr="", json_data={})

        with mock.patch.object(harness, "control", side_effect=fake_control):
            harness.start_queue_idle("job", "echo ok", "--owner", "codex", check=False)

        args = captured["args"]
        assert isinstance(args, list)
        parsed = tmux_control.build_parser().parse_args(args)
        self.assertEqual(args.count("--pane"), 1)
        self.assertEqual(parsed.pane, "%42")
        self.assertEqual(parsed.command_text, "echo ok")
        self.assertEqual(captured["step"], "start-job")
        self.assertFalse(captured["check"])
        self.assertEqual(harness.jobs, ["job"])

    def test_run_scenarios_reports_setup_failure_without_raising(self) -> None:
        class FakeHarness:
            commands: list[list[str]] = []

            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.keep_artifacts = keep_artifacts
                self.session = "fake-session"
                self.removed_repo_artifacts: list[str] = []

            def setup_tmux(self) -> None:
                raise e2e_real_use.ScenarioFailure("setup", "tmux-new-session", "tmux failed")

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cancel_active_jobs(self) -> None:
                pass

            def run(self, args: list[str]) -> e2e_real_use.CommandResult:
                self.commands.append(args)
                return e2e_real_use.CommandResult(args=args, returncode=1, stdout="", stderr="", json_data=None)

            def remove_repo_runtime_artifacts(self) -> list[str]:
                return []

            def repo_runtime_artifacts(self) -> list[str]:
                return []

            def session_exists(self) -> bool:
                return False

            def server_exists(self) -> bool:
                return False

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                    "remove_artifacts": remove_artifacts,
                }

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            exit_code, summary = e2e_real_use.run_scenarios(["idle-continuation"], keep_artifacts=False)
            kept_exit_code, kept_summary = e2e_real_use.run_scenarios(["idle-continuation"], keep_artifacts=True)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["results"][0]["scenario"], "setup")
        self.assertEqual(summary["results"][0]["failure"]["step"], "tmux-new-session")
        self.assertEqual(summary["cleanup"]["remove_artifacts"], True)
        self.assertEqual(kept_exit_code, 1)
        self.assertEqual(kept_summary["status"], "failed")
        self.assertEqual(kept_summary["results"][0]["scenario"], "setup")
        self.assertTrue(kept_summary["artifacts_kept"])
        self.assertEqual(kept_summary["artifact_dir"], "/tmp/tmux-skills-fake-e2e")
        self.assertFalse(kept_summary["cleanup"]["temp_dir_removed"])
        self.assertIn(["tmux", "kill-server"], FakeHarness.commands)

    def test_run_scenarios_keep_artifacts_still_cleans_successful_run(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.keep_artifacts = keep_artifacts
                self.cleanup_remove_artifacts: bool | None = None

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                self.current_scenario = name

            def after_scenario(self) -> None:
                pass

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                self.cleanup_remove_artifacts = remove_artifacts
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                    "remove_artifacts": remove_artifacts,
                }

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"ok": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["ok"], keep_artifacts=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "passed")
        self.assertFalse(summary["artifacts_kept"])
        self.assertIsNone(summary["artifact_dir"])
        self.assertTrue(summary["cleanup"]["remove_artifacts"])

    def test_run_scenarios_reports_unexpected_setup_exception(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")

            def setup_tmux(self) -> None:
                raise RuntimeError("tmux setup exploded")

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                }

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            exit_code, summary = e2e_real_use.run_scenarios(["idle-continuation"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        failure = summary["results"][0]["failure"]
        self.assertEqual(failure["scenario"], "setup")
        self.assertEqual(failure["step"], "unexpected-exception")
        self.assertIn("RuntimeError: tmux setup exploded", failure["message"])

    def test_run_scenarios_reports_unexpected_scenario_exception(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.after_called = False

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                self.current_scenario = name

            def after_scenario(self) -> None:
                self.after_called = True

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                }

        def boom(_harness: object) -> dict[str, object]:
            raise RuntimeError("boom")

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"boom": boom}):
                exit_code, summary = e2e_real_use.run_scenarios(["boom"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        failure = summary["results"][0]["failure"]
        self.assertEqual(failure["scenario"], "boom")
        self.assertEqual(failure["step"], "unexpected-exception")
        self.assertIn("RuntimeError: boom", failure["message"])

    def test_run_scenarios_keep_going_runs_after_scenario_failure(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.visited: list[str] = []

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                self.current_scenario = name
                self.visited.append(name)

            def after_scenario(self) -> None:
                pass

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                    "visited": self.visited,
                }

        def fail(_harness: object) -> dict[str, object]:
            raise e2e_real_use.ScenarioFailure("first", "expected-failure", "first failed")

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"first": fail, "second": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["first", "second"], keep_artifacts=False, keep_going=True)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual([result["scenario"] for result in summary["results"][:2]], ["first", "second"])
        self.assertEqual(summary["results"][0]["status"], "failed")
        self.assertEqual(summary["results"][1]["status"], "passed")
        self.assertEqual(summary["cleanup"]["visited"], ["first", "teardown-check", "second", "teardown-check"])

    def test_run_scenarios_stops_after_failure_by_default(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.visited: list[str] = []

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                self.current_scenario = name
                self.visited.append(name)

            def after_scenario(self) -> None:
                pass

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                    "visited": self.visited,
                }

        def fail(_harness: object) -> dict[str, object]:
            raise e2e_real_use.ScenarioFailure("first", "expected-failure", "first failed")

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"first": fail, "second": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["first", "second"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual([result["scenario"] for result in summary["results"]], ["first"])
        self.assertEqual(summary["results"][0]["status"], "failed")
        self.assertEqual(summary["cleanup"]["visited"], ["first", "teardown-check"])

    def test_run_scenarios_reports_teardown_exceptions(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                if name == "teardown-check":
                    raise RuntimeError("teardown check failed")

            def after_scenario(self) -> None:
                raise RuntimeError("after failed")

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                return {"scenario": failure.scenario, "step": failure.step, "message": failure.message}

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                }

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"ok": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["ok"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        failures = [result["failure"] for result in summary["results"] if result["status"] == "failed"]
        self.assertEqual([failure["step"] for failure in failures], ["after-scenario", "teardown-check"])
        self.assertIn("RuntimeError: after failed", failures[0]["message"])
        self.assertIn("RuntimeError: teardown check failed", failures[1]["message"])

    def test_run_scenarios_preserves_failure_when_diagnostics_fails(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                pass

            def after_scenario(self) -> None:
                pass

            def diagnostics(self, failure: e2e_real_use.ScenarioFailure) -> dict[str, object]:
                raise RuntimeError("diagnostics failed")

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": True,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                }

        def fail(_harness: object) -> dict[str, object]:
            raise e2e_real_use.ScenarioFailure("scenario", "step", "original failure")

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"scenario": fail}):
                exit_code, summary = e2e_real_use.run_scenarios(["scenario"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        failure = summary["results"][0]["failure"]
        self.assertEqual(failure["scenario"], "scenario")
        self.assertEqual(failure["step"], "step")
        self.assertEqual(failure["message"], "original failure")
        self.assertEqual(failure["diagnostics_error"], "RuntimeError: diagnostics failed")

    def test_run_scenarios_reports_tmux_server_cleanup_leak(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                pass

            def after_scenario(self) -> None:
                pass

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                return {
                    "session_absent": True,
                    "server_absent": False,
                    "temp_dir_removed": True,
                    "repo_runtime_artifacts": [],
                    "removed_repo_runtime_artifacts": [],
                    "artifact_dir": str(self.base_dir),
                }

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"ok": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["ok"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        cleanup_result = summary["results"][-1]
        self.assertEqual(cleanup_result["scenario"], "e2e-cleanup-verification")
        self.assertEqual(cleanup_result["failure"]["step"], "post-run-cleanup")
        self.assertIn("test tmux server still exists", cleanup_result["failure"]["message"])

    def test_run_scenarios_reports_cleanup_exception(self) -> None:
        class FakeHarness:
            def __init__(self, *, keep_artifacts: bool = False) -> None:
                self.base_dir = Path("/tmp/tmux-skills-fake-e2e")
                self.removed_repo_artifacts = ["scripts/__pycache__"]

            def setup_tmux(self) -> None:
                pass

            def before_scenario(self, name: str) -> None:
                pass

            def after_scenario(self) -> None:
                pass

            def cleanup(self, *, remove_artifacts: bool = True) -> dict[str, object]:
                raise RuntimeError("cleanup failed")

        def ok(_harness: object) -> dict[str, object]:
            return {"ok": True}

        with mock.patch.object(e2e_real_use, "Harness", FakeHarness):
            with mock.patch.dict(e2e_real_use.SCENARIO_METHODS, {"ok": ok}):
                exit_code, summary = e2e_real_use.run_scenarios(["ok"], keep_artifacts=False)

        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "failed")
        cleanup_result = summary["results"][-1]
        self.assertEqual(cleanup_result["scenario"], "e2e-cleanup-verification")
        self.assertEqual(cleanup_result["failure"]["step"], "cleanup-exception")
        self.assertEqual(cleanup_result["failure"]["cleanup"]["cleanup_error"], "RuntimeError: cleanup failed")
        self.assertEqual(cleanup_result["failure"]["cleanup"]["removed_repo_runtime_artifacts"], ["scripts/__pycache__"])


if __name__ == "__main__":
    unittest.main()
