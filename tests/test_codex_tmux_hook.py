from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import codex_tmux_hook
import tmux_state


HOOK = Path(__file__).resolve().parents[1] / "scripts" / "codex_tmux_hook.py"
HOOK_DOC = Path(__file__).resolve().parents[1] / "references" / "HOOKS.md"


class CodexTmuxHookTests(unittest.TestCase):
    def run_hook(self, args: list[str], stdin: dict[str, object] | None = None) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(HOOK), *args],
            input=json.dumps(stdin or {}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def run_hook_raw_stdin(self, args: list[str], stdin: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(HOOK), *args],
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def test_help_describes_subcommands(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        self.assertIn("context", result.stdout)
        self.assertIn("Emit additional Codex context", result.stdout)
        self.assertIn("stop", result.stdout)
        self.assertIn("Block once on ready tasks", result.stdout)

    def test_hook_docs_describe_stop_stdin_safety(self) -> None:
        text = HOOK_DOC.read_text(encoding="utf-8")

        self.assertIn("empty, malformed, or non-object stdin", text)
        self.assertIn("stop_hook_active: true", text)
        self.assertIn("bounds long text", text)

    def test_hook_docs_and_parser_support_custom_state_dir(self) -> None:
        text = HOOK_DOC.read_text(encoding="utf-8")
        parser = codex_tmux_hook.build_parser()

        self.assertIn("--state-dir PATH", text)
        context = parser.parse_args(["context", "--event", "SessionStart", "--workspace", "/repo", "--state-dir", "/tmp/state"])
        stop = parser.parse_args(["stop", "--workspace", "/repo", "--state-dir", "/tmp/state"])
        self.assertEqual(context.state_dir, "/tmp/state")
        self.assertEqual(stop.state_dir, "/tmp/state")

    def test_hook_doc_commands_match_public_parser(self) -> None:
        text = HOOK_DOC.read_text(encoding="utf-8")
        commands = [
            ast.literal_eval(f'"{match}"')
            for match in re.findall(r'^command = "(.*)"$', text, flags=re.MULTILINE)
        ]
        parser = codex_tmux_hook.build_parser()

        self.assertEqual(len(commands), 3)
        for command in commands:
            with self.subTest(command=command):
                argv = shlex.split(command)
                self.assertEqual(argv[:2], ["python", "scripts/codex_tmux_hook.py"])
                parser.parse_args(argv[2:])

    def test_stop_treats_empty_stdin_as_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook_raw_stdin(["stop", "--workspace", tmp], "")

        self.assertEqual(output["decision"], "block")
        self.assertIn("training: failed", output["reason"])

    def test_stop_treats_invalid_stdin_json_as_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook_raw_stdin(["stop", "--workspace", tmp], "{not-json")

        self.assertEqual(output["decision"], "block")
        self.assertIn("training: failed", output["reason"])

    def test_stop_treats_non_object_stdin_json_as_empty_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook_raw_stdin(["stop", "--workspace", tmp], '["not", "object"]')

        self.assertEqual(output["decision"], "block")
        self.assertIn("training: failed", output["reason"])

    def write_terminal_status(self, workspace: str) -> dict[str, object]:
        paths = tmux_state.state_paths(workspace)
        tmux_state.ensure_state_dirs(paths)
        status_file = tmux_state.status_path(paths, "job")
        status = tmux_state.build_status(
            kind="job",
            item_id="job",
            attempt=1,
            name="training",
            status="failed",
            pane_id="%1",
            command_preview_text="python train.py",
            cwd=workspace,
            status_file=status_file,
            log_file=tmux_state.log_path(paths, "job"),
            exit_code=2,
            last_output="Traceback",
        )
        return tmux_state.write_status(status_file, status)

    def test_context_outputs_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp])
        self.assertIn("hookSpecificOutput", output)
        self.assertIn("training: failed", output["hookSpecificOutput"]["additionalContext"])

    def test_context_compacts_multiline_status_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")
            status = tmux_state.build_status(
                kind="job",
                item_id="job",
                attempt=1,
                name="training",
                status="failed",
                pane_id="%1",
                command_preview_text="python train.py",
                cwd=tmp,
                status_file=status_file,
                log_file=tmux_state.log_path(paths, "job"),
                exit_code=2,
                last_output="line one\nline two",
            )
            tmux_state.write_status(status_file, status)

            output = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp])

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tail=line one line two", context)
        self.assertNotIn("\nline two", context)

    def test_context_compacts_multiline_status_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "legacy-field"),
                {
                    "id": "legacy-field",
                    "name": "train\nmodel",
                    "status": "failed",
                    "exit_code": "2\n3",
                    "pane_id": "%1\nextra",
                    "last_output": "done",
                    "updated_at": tmux_state.utc_now(),
                },
            )

            output = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp])

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("train model: failed", context)
        self.assertIn("exit=2 3", context)
        self.assertIn("pane=%1 extra", context)
        self.assertNotIn("\n3", context)
        self.assertNotIn("\nmodel", context)
        self.assertNotIn("\nextra", context)

    def test_context_normalizes_legacy_running_status_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "legacy-running"),
                {
                    "id": "legacy-running",
                    "name": "legacy",
                    "status": " Running ",
                    "updated_at": tmux_state.utc_now(),
                },
            )

            output = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp], {})

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("legacy: running", context)

    def test_stop_blocks_once_and_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            first = self.run_hook(["stop", "--workspace", tmp], {})
            second = self.run_hook(["stop", "--workspace", tmp], {})
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second, {})

    def test_stop_acks_legacy_terminal_status_without_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "legacy.json").write_text(
                json.dumps({"id": "legacy", "status": "succeeded", "updated_at": "2026-05-30T00:00:00Z"}),
                encoding="utf-8",
            )

            first = self.run_hook(["stop", "--workspace", tmp], {})
            second = self.run_hook(["stop", "--workspace", tmp], {})

        self.assertEqual(first["decision"], "block")
        self.assertIn("legacy: succeeded", first["reason"])
        self.assertEqual(second, {})

    def test_stop_hook_active_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook(["stop", "--workspace", tmp], {"stop_hook_active": True})
        self.assertEqual(output, {})

    def test_context_and_stop_include_ready_task_without_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect the failed training log",
                summary="inspect failure",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)

            context = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp], {})
            stop = self.run_hook(["stop", "--workspace", tmp], {})
            stored = tmux_state.read_json(tmux_state.task_path(paths, "follow-up"))[0]

        self.assertIn("ready task follow-up", context["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(stop["decision"], "block")
        self.assertIn("Inspect the failed training log", stop["reason"])
        self.assertEqual(stored["status"], "waiting")

    def test_stop_ready_task_acks_matched_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect the failed training log",
                summary="inspect failure",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)

            first = self.run_hook(["stop", "--workspace", tmp], {})
            task["status"] = "done"
            tmux_state.write_task(paths, task)
            second = self.run_hook(["stop", "--workspace", tmp], {})

        self.assertEqual(first["decision"], "block")
        self.assertIn("ready task follow-up", first["reason"])
        self.assertEqual(second, {})

    def test_stop_terminal_event_survives_ack_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            args = argparse.Namespace(workspace=tmp, state_dir=None)

            with mock.patch.object(codex_tmux_hook.tmux_state, "ack_status", side_effect=OSError("disk full")):
                output = codex_tmux_hook.stop(args, {})

        self.assertEqual(output["decision"], "block")
        self.assertIn("tmux-skills observed a terminal event", output["reason"])
        self.assertIn("Could not acknowledge terminal event", output["reason"])

    def test_stop_ready_task_survives_matched_event_ack_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect the failed training log",
                summary="inspect failure",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)
            args = argparse.Namespace(workspace=tmp, state_dir=None)

            with mock.patch.object(codex_tmux_hook.tmux_state, "ack_status", side_effect=OSError("disk full")):
                output = codex_tmux_hook.stop(args, {})

        self.assertEqual(output["decision"], "block")
        self.assertIn("ready task follow-up", output["reason"])
        self.assertIn("Could not acknowledge terminal event", output["reason"])

    def test_stop_ready_task_reports_unreadable_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect the failed training log",
                summary="inspect failure",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)
            (paths["jobs"] / "bad.json").write_text("{", encoding="utf-8")

            output = self.run_hook(["stop", "--workspace", tmp], {})

        self.assertEqual(output["decision"], "block")
        self.assertIn("ready task follow-up", output["reason"])
        self.assertIn("Skipped 1 unreadable tmux-skills state file(s).", output["reason"])

    def test_context_includes_active_managed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": "watch",
                    "status": "starting",
                    "pane_id": "%1\nextra",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )

            output = self.run_hook(["context", "--event", "UserPromptSubmit", "--workspace", tmp], {})

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("managed job watch: starting kind=watch pane=%1 extra heartbeat=", context)
        self.assertNotIn("\nextra", context)

    def test_context_excludes_stale_managed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "stale-watch"),
                {
                    "job_id": "stale-watch",
                    "kind": "watch",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": old,
                    "updated_at": old,
                    "check_interval_seconds": 1,
                },
            )

            output = self.run_hook(["context", "--event", "UserPromptSubmit", "--workspace", tmp], {})

        self.assertEqual(output, {})

    def test_context_excludes_stale_foreign_live_pid_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            foreign = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                tmux_state.atomic_write_json(
                    tmux_state.job_path(paths, "foreign-watch"),
                    {
                        "job_id": "foreign-watch",
                        "kind": "watch",
                        "status": "running",
                        "pid": foreign.pid,
                        "pane_id": "%1",
                        "heartbeat_at": old,
                        "updated_at": old,
                        "check_interval_seconds": 1,
                    },
                )

                output = self.run_hook(["context", "--event", "UserPromptSubmit", "--workspace", tmp], {})
            finally:
                foreign.terminate()
                try:
                    foreign.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    foreign.kill()
                    foreign.wait(timeout=5)

        self.assertEqual(output, {})

    def test_context_reports_unreadable_state_files_generically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["jobs"] / "bad.json").write_text("{", encoding="utf-8")

            output = self.run_hook(["context", "--event", "UserPromptSubmit", "--workspace", tmp], {})

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Skipped 1 unreadable tmux-skills state file(s).", context)
        self.assertNotIn("status file(s)", context)

    def test_ready_task_hook_output_compacts_multiline_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect first line\nthen inspect second line",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)

            context = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp], {})
            stop = self.run_hook(["stop", "--workspace", tmp], {})

        additional = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Inspect first line then inspect second line", additional)
        self.assertNotIn("\nthen inspect second line", additional)
        self.assertIn("Inspect first line then inspect second line", stop["reason"])

    def test_ready_task_hook_output_bounds_long_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            long_instruction = "Inspect prefix " + ("x" * 1000) + " suffix should be omitted"
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction=long_instruction,
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)

            context = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp], {})
            stop = self.run_hook(["stop", "--workspace", tmp], {})

        additional = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Inspect prefix", additional)
        self.assertIn("...", additional)
        self.assertNotIn("suffix should be omitted", additional)
        self.assertIn("Inspect prefix", stop["reason"])
        self.assertIn("...", stop["reason"])
        self.assertNotIn("suffix should be omitted", stop["reason"])


if __name__ == "__main__":
    unittest.main()
