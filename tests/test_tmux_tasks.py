from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_control
import tmux_state


CONTROL = Path(__file__).resolve().parents[1] / "scripts" / "tmux_control.py"


class TmuxTaskTests(unittest.TestCase):
    def write_status(self, paths: dict[str, Path], job_id: str, status: str = "succeeded") -> dict[str, object]:
        status_file = tmux_state.status_path(paths, job_id)
        data = tmux_state.build_status(
            kind="job",
            item_id=job_id,
            attempt=1,
            name="job",
            status=status,
            pane_id="%1",
            command_preview_text="echo ok",
            cwd=str(paths["workspace"]),
            status_file=status_file,
            log_file=tmux_state.log_path(paths, job_id),
            exit_code=0 if status == "succeeded" else 1,
            last_output="ok",
        )
        return tmux_state.write_status(status_file, data)

    def cli(self, args: list[str], workspace: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTROL), *args, "--workspace", workspace],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_task_add_next_claim_done_blocked_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")

            add = self.cli(
                [
                    "task",
                    "add",
                    "--after-job",
                    "job",
                    "--trigger-on",
                    "succeeded",
                    "--instruction",
                    "Inspect the result",
                    "--summary",
                    "inspect",
                ],
                tmp,
            )
            task = json.loads(add.stdout)
            task_id = task["task_id"]

            next_result = self.cli(["task", "next", "--json"], tmp)
            self.assertEqual(json.loads(next_result.stdout)["task_id"], task_id)

            claimed = json.loads(self.cli(["task", "claim", "--task-id", task_id], tmp).stdout)
            self.assertEqual(claimed["status"], "in_progress")

            done = json.loads(self.cli(["task", "done", "--task-id", task_id, "--note", "finished"], tmp).stdout)
            self.assertEqual(done["status"], "done")

            blocked_add = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Blocked task",
                    ],
                    tmp,
                ).stdout
            )
            blocked = json.loads(
                self.cli(["task", "blocked", "--task-id", blocked_add["task_id"], "--note", "needs input"], tmp).stdout
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["blocked_reason"], "needs input")

            cancelled_add = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Cancel task",
                    ],
                    tmp,
                ).stdout
            )
            cancelled = json.loads(self.cli(["task", "cancel", "--task-id", cancelled_add["task_id"]], tmp).stdout)
            self.assertEqual(cancelled["status"], "cancelled")

    def test_task_finish_clears_stale_state_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            added = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Inspect metadata",
                    ],
                    tmp,
                ).stdout
            )

            blocked = json.loads(self.cli(["task", "blocked", "--task-id", added["task_id"], "--note", "needs input"], tmp).stdout)
            done = json.loads(self.cli(["task", "done", "--task-id", added["task_id"], "--note", "finished"], tmp).stdout)
            blocked_again = json.loads(self.cli(["task", "blocked", "--task-id", added["task_id"], "--note", "reopened"], tmp).stdout)

        self.assertEqual(blocked["blocked_reason"], "needs input")
        self.assertIsNone(blocked["completed_at"])
        self.assertIsNone(done["blocked_reason"])
        self.assertIsNotNone(done["completed_at"])
        self.assertEqual(done["summary"], "finished")
        self.assertEqual(blocked_again["blocked_reason"], "reopened")
        self.assertIsNone(blocked_again["completed_at"])

    def test_task_list_keeps_blocked_tasks_visible_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            blocked = tmux_state.build_task(
                task_id="blocked",
                instruction="Resolve blocked work",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            blocked["status"] = "blocked"
            blocked["blocked_reason"] = "needs input"
            tmux_state.write_task(paths, blocked)
            cancelled = tmux_state.build_task(
                task_id="cancelled",
                instruction="Ignore cancelled work",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            cancelled["status"] = "cancelled"
            tmux_state.write_task(paths, cancelled)

            default_output = self.cli(["task", "list"], tmp).stdout
            all_output = self.cli(["task", "list", "--all"], tmp).stdout

        self.assertIn("blocked [blocked] Resolve blocked work", default_output)
        self.assertNotIn("cancelled [cancelled] Ignore cancelled work", default_output)
        self.assertIn("cancelled [cancelled] Ignore cancelled work", all_output)

    def test_task_next_uses_oldest_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            older = tmux_state.build_task(
                task_id="older",
                instruction="Handle older ready task",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            newer = tmux_state.build_task(
                task_id="newer",
                instruction="Handle newer ready task",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            older["updated_at"] = "2026-05-30T00:00:00Z"
            newer["updated_at"] = "2026-05-30T00:01:00Z"
            tmux_state.atomic_write_json(tmux_state.task_path(paths, "older"), older)
            tmux_state.atomic_write_json(tmux_state.task_path(paths, "newer"), newer)

            next_task = json.loads(self.cli(["task", "next", "--json"], tmp).stdout)

        self.assertEqual(next_task["task_id"], "older")

    def test_task_finish_writes_under_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            task = tmux_state.build_task(
                task_id="task",
                instruction="Finish under lock",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            task["status"] = "in_progress"
            tmux_state.write_task(paths, task)
            lock_active = False

            class FakeLock:
                def __enter__(self) -> None:
                    nonlocal lock_active
                    lock_active = True

                def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
                    nonlocal lock_active
                    lock_active = False

            def fake_task_lock(_paths: dict[str, Path], task_id: str) -> FakeLock:
                self.assertEqual(task_id, "task")
                return FakeLock()

            original_write_task = tmux_control.tmux_state.write_task

            def checking_write_task(_paths: dict[str, Path], updated: dict[str, object]) -> dict[str, object]:
                self.assertTrue(lock_active)
                return original_write_task(_paths, updated)

            args = argparse.Namespace(task_id="task", note="finished", workspace=tmp, state_dir=None)
            with mock.patch.object(tmux_control, "task_lock", side_effect=fake_task_lock):
                with mock.patch.object(tmux_control.tmux_state, "write_task", side_effect=checking_write_task):
                    result = tmux_control.task_finish(args, "done")

        self.assertEqual(result["status"], "done")
        self.assertEqual(result["summary"], "finished")

    def test_task_claim_rejects_blank_task_id_without_claiming_default_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="job",
                instruction="Do not claim by blank id",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "claim",
                    "--task-id",
                    "",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            stored = tmux_state.read_json(tmux_state.task_path(paths, "job"))[0]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task command requires nonblank --task-id", result.stderr)
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")

    def test_task_finish_rejects_blank_task_id_without_finishing_default_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            task = tmux_state.build_task(
                task_id="job",
                instruction="Do not finish by blank id",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            task["status"] = "in_progress"
            tmux_state.write_task(paths, task)

            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "done",
                    "--task-id",
                    " ",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            stored = tmux_state.read_json(tmux_state.task_path(paths, "job"))[0]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task command requires nonblank --task-id", result.stderr)
        assert stored is not None
        self.assertEqual(stored["status"], "in_progress")

    def test_task_add_requires_job_or_event_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "add",
                    "--trigger-on",
                    "terminal",
                    "--instruction",
                    "Unanchored task",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--after-job", result.stderr)
        self.assertIn("--after-event", result.stderr)

    def test_task_add_rejects_blank_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = [
                ("--after-job", "   "),
                ("--after-event", "\t"),
            ]
            results = []
            for flag, value in cases:
                results.append(
                    subprocess.run(
                        [
                            sys.executable,
                            str(CONTROL),
                            "task",
                            "add",
                            flag,
                            value,
                            "--trigger-on",
                            "terminal",
                            "--instruction",
                            "Blank anchored task",
                            "--workspace",
                            tmp,
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                )
            paths = tmux_state.state_paths(tmp)
            task_files = list(paths["tasks"].glob("*.json")) if paths["tasks"].exists() else []

        for result in results:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--after-job", result.stderr)
            self.assertIn("--after-event", result.stderr)
        self.assertEqual(task_files, [])

    def test_task_add_rejects_blank_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "add",
                    "--after-job",
                    "job",
                    "--trigger-on",
                    "terminal",
                    "--instruction",
                    " \n\t ",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            paths = tmux_state.state_paths(tmp)
            task_files = list(paths["tasks"].glob("*.json")) if paths["tasks"].exists() else []

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-empty --instruction", result.stderr)
        self.assertEqual(task_files, [])

    def test_task_add_rejects_blank_task_id_without_creating_default_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "add",
                    "--task-id",
                    " \n\t ",
                    "--after-job",
                    "job",
                    "--trigger-on",
                    "terminal",
                    "--instruction",
                    "Do not create a default task",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            paths = tmux_state.state_paths(tmp)
            task_files = list(paths["tasks"].glob("*.json")) if paths["tasks"].exists() else []

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task add requires nonblank --task-id when provided", result.stderr)
        self.assertEqual(task_files, [])

    def test_task_add_rejects_both_job_and_event_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "add",
                    "--after-job",
                    "job",
                    "--after-event",
                    "event",
                    "--trigger-on",
                    "terminal",
                    "--instruction",
                    "Ambiguous task",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with argument", result.stderr)

    def test_task_add_after_job_matches_sanitized_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, tmux_state.safe_id("job with space"))

            added = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job with space",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Inspect spaced job",
                    ],
                    tmp,
                ).stdout
            )
            next_result = json.loads(self.cli(["task", "next", "--json"], tmp).stdout)

        self.assertEqual(added["after_job_id"], "job-with-space")
        self.assertEqual(next_result["task_id"], added["task_id"])
        self.assertEqual(next_result["effective_status"], "ready")

    def test_task_add_rejects_duplicate_task_id_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            original = tmux_state.build_task(
                task_id="same-task",
                instruction="Original instruction",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, original)

            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "add",
                    "--task-id",
                    "same task",
                    "--after-job",
                    "job",
                    "--trigger-on",
                    "succeeded",
                    "--instruction",
                    "Replacement instruction",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            stored = tmux_state.read_json(tmux_state.task_path(paths, "same-task"))[0]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task already exists: same-task", result.stderr)
        assert stored is not None
        self.assertEqual(stored["instruction"], "Original instruction")

    def test_task_add_writes_under_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_active = False

            class FakeLock:
                def __enter__(self) -> None:
                    nonlocal lock_active
                    lock_active = True

                def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
                    nonlocal lock_active
                    lock_active = False

            def fake_task_lock(_paths: dict[str, Path], task_id: str) -> FakeLock:
                self.assertEqual(task_id, "same-task")
                return FakeLock()

            def fake_write_task(_paths: dict[str, Path], task: dict[str, object]) -> dict[str, object]:
                self.assertTrue(lock_active)
                return dict(task)

            args = argparse.Namespace(
                task_id="same task",
                after_job="job",
                after_event=None,
                trigger_on="succeeded",
                instruction="Add under lock",
                summary=None,
                intent=None,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_control, "task_lock", side_effect=fake_task_lock):
                with mock.patch.object(tmux_control.tmux_state, "write_task", side_effect=fake_write_task):
                    result = tmux_control.task_add(args)

        self.assertEqual(result["task_id"], "same-task")
        self.assertEqual(result["instruction"], "Add under lock")

    def test_concurrent_claim_allows_only_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="task",
                instruction="Claim once",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)
            command = [
                sys.executable,
                str(CONTROL),
                "task",
                "claim",
                "--task-id",
                "task",
                "--workspace",
                tmp,
            ]
            procs = [subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            results = []
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=10)
                results.append((proc.returncode, stdout, stderr))

        self.assertEqual(sum(1 for returncode, _stdout, _stderr in results if returncode == 0), 1)
        self.assertEqual(sum(1 for returncode, _stdout, _stderr in results if returncode != 0), 1)
        self.assertTrue(any("task is not ready" in stderr for _returncode, _stdout, stderr in results))

    def test_task_claim_does_not_persist_derived_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="task",
                instruction="Claim canonically",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            claimed = json.loads(self.cli(["task", "claim", "--task-id", "task"], tmp).stdout)
            stored = tmux_state.read_json(tmux_state.task_path(paths, "task"))[0]

        self.assertEqual(claimed["status"], "in_progress")
        assert stored is not None
        for key in ("effective_status", "matched_status", "stale", "task_path"):
            self.assertNotIn(key, stored)

    def test_task_claim_preserves_requested_file_id_for_mismatched_json_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="wrong-id",
                instruction="Claim by file id",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.atomic_write_json(tmux_state.task_path(paths, "file-id"), task)

            claimed = json.loads(self.cli(["task", "claim", "--task-id", "file id"], tmp).stdout)
            stored = tmux_state.read_json(tmux_state.task_path(paths, "file-id"))[0]

        self.assertEqual(claimed["task_id"], "file-id")
        self.assertEqual(claimed["status"], "in_progress")
        assert stored is not None
        self.assertEqual(stored["task_id"], "file-id")
        self.assertEqual(stored["status"], "in_progress")
        self.assertFalse(tmux_state.task_path(paths, "wrong-id").exists())

    def test_task_load_is_read_only_and_for_skill_has_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="task",
                instruction="Continue analysis",
                summary="continue",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)
            before = tmux_state.read_json(tmux_state.task_path(paths, "task"))[0]

            output = self.cli(["task", "load", "--for-skill"], tmp).stdout
            after = tmux_state.read_json(tmux_state.task_path(paths, "task"))[0]

            self.assertEqual(before, after)
            for heading in (
                "What happened",
                "Current state",
                "Next actionable instruction",
                "Blocked or stale",
                "Evidence files",
                "Safe commands to inspect",
                "Do not auto-run",
            ):
                self.assertIn(heading, output)
            self.assertIn("Continue analysis", output)

    def test_task_load_reports_managed_running_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": "watch",
                    "status": "starting",
                    "pid": 0,
                    "pane_id": "%2\nextra",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            plain = self.cli(["task", "load"], tmp).stdout
            for_skill = self.cli(["task", "load", "--for-skill"], tmp).stdout

        expected = "- watch starting kind=watch pane=%2 extra"
        self.assertIn(expected, plain)
        self.assertIn(expected, for_skill)
        self.assertNotIn("\nextra", plain)
        self.assertNotIn("\nextra", for_skill)

    def test_task_load_for_skill_compacts_multiline_display_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")
            status = tmux_state.build_status(
                kind="job",
                item_id="job",
                attempt=1,
                name="job",
                status="succeeded",
                pane_id="%1",
                command_preview_text="echo ok",
                cwd=str(paths["workspace"]),
                status_file=status_file,
                log_file=tmux_state.log_path(paths, "job"),
                exit_code=0,
                last_output="line one\nline two",
            )
            tmux_state.write_status(status_file, status)
            task = tmux_state.build_task(
                task_id="task",
                instruction="Inspect first line\nthen inspect second line",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            output = self.cli(["task", "load", "--for-skill"], tmp).stdout

        self.assertIn("tail=line one line two", output)
        self.assertIn("task_id=task: Inspect first line then inspect second line", output)
        self.assertNotIn("\nline two", output)
        self.assertNotIn("\nthen inspect second line", output)

    def test_task_load_compacts_multiline_recent_job_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "legacy")
            tmux_state.atomic_write_json(
                status_file,
                {
                    "id": "legacy",
                    "kind": "job",
                    "status": "succeeded",
                    "exit_code": "2\n3",
                    "log_path": "logs/one.log\nlogs/two.log",
                    "last_output": "done",
                    "updated_at": tmux_state.utc_now(),
                },
            )

            plain = self.cli(["task", "load"], tmp).stdout
            for_skill = self.cli(["task", "load", "--for-skill"], tmp).stdout

        self.assertIn("exit=2 3", plain)
        self.assertIn("log=logs/one.log logs/two.log", plain)
        self.assertIn("exit=2 3", for_skill)
        self.assertNotIn("\n3", plain)
        self.assertNotIn("\nlogs/two.log", plain)
        self.assertNotIn("\n3", for_skill)

    def test_task_load_for_skill_bounds_long_instruction_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            long_instruction = "Inspect prefix " + ("x" * 1000) + " suffix should be omitted"
            task = tmux_state.build_task(
                task_id="task",
                instruction=long_instruction,
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            text_output = self.cli(["task", "load", "--for-skill"], tmp).stdout
            json_output = json.loads(self.cli(["task", "load", "--json"], tmp).stdout)

        self.assertIn("Inspect prefix", text_output)
        self.assertIn("...", text_output)
        self.assertNotIn("suffix should be omitted", text_output)
        self.assertEqual(json_output["ready_tasks"][0]["instruction"], long_instruction)

    def test_task_load_rejects_nonpositive_max_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "load",
                    "--json",
                    "--max-items",
                    "0",
                    "--workspace",
                    tmp,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive integer", result.stderr)

    def test_task_load_limits_ready_tasks_by_max_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            for index in range(3):
                task = tmux_state.build_task(
                    task_id=f"task-{index}",
                    instruction=f"Task {index}",
                    summary=None,
                    intent=None,
                    after_job_id="job",
                    after_event_id=None,
                    trigger_on="succeeded",
                )
                tmux_state.write_task(paths, task)

            data = json.loads(self.cli(["task", "load", "--json", "--max-items", "2"], tmp).stdout)

        self.assertEqual(len(data["ready_tasks"]), 2)
        self.assertGreaterEqual(len(data["all_tasks"]), 3)

    def test_task_load_safe_commands_preserve_custom_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "custom-state"
            paths = tmux_state.state_paths(tmp, str(state_dir))
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="task",
                instruction="Inspect custom state",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            result = subprocess.run(
                [
                    sys.executable,
                    str(CONTROL),
                    "task",
                    "load",
                    "--json",
                    "--workspace",
                    tmp,
                    "--state-dir",
                    str(state_dir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            data = json.loads(result.stdout)

        task_commands = [command for command in data["safe_commands"] if " task " in command]
        self.assertTrue(task_commands)
        for command in task_commands:
            self.assertIn("--state-dir", command)
            self.assertIn(str(state_dir), command)

    def test_json_load_handles_old_status_and_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "old.json").write_text(json.dumps({"id": "old", "status": "succeeded"}), encoding="utf-8")
            (paths["status"] / "bad.json").write_text("{", encoding="utf-8")

            result = self.cli(["task", "load", "--json"], tmp)
            data = json.loads(result.stdout)
            self.assertEqual(data["recent_jobs"][0]["id"], "old")
            self.assertEqual(len(data["errors"]), 1)

    def test_text_load_reports_unreadable_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "bad.json").write_text("{", encoding="utf-8")

            plain = self.cli(["task", "load"], tmp).stdout
            for_skill = self.cli(["task", "load", "--for-skill"], tmp).stdout

        self.assertIn("State Warnings", plain)
        self.assertIn("Skipped unreadable state file", plain)
        self.assertIn("State warnings", for_skill)
        self.assertIn("Skipped unreadable state file", for_skill)

    def test_load_tasks_tolerates_corrupt_task_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            good = tmux_state.build_task(
                task_id="good",
                instruction="Keep loading",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            tmux_state.write_task(paths, good)
            (paths["tasks"] / "bad.json").write_text(
                json.dumps({"task_id": "bad", "version": "not-an-int", "instruction": "x"}),
                encoding="utf-8",
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])
            by_id = {task["task_id"]: task for task in tasks}
            self.assertEqual(errors, [])
            self.assertIn("good", by_id)
            self.assertEqual(by_id["bad"]["version"], tmux_state.TASK_VERSION)

            state = tmux_state.load_task_state(paths)
            state_by_id = {task["task_id"]: task for task in state["tasks"]}
            self.assertEqual(state["errors"], [])
            self.assertIn("good", state_by_id)
            self.assertEqual(state_by_id["bad"]["version"], tmux_state.TASK_VERSION)

    def test_load_tasks_normalizes_legacy_string_evidence_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_status(paths, "job")
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy"),
                {
                    "task_id": "legacy",
                    "status": "waiting",
                    "instruction": "Inspect legacy evidence",
                    "after_job_id": "job",
                    "trigger_on": "succeeded",
                    "evidence_paths": "legacy.log",
                },
            )

            data = json.loads(self.cli(["task", "load", "--json"], tmp).stdout)

        ready = data["ready_tasks"][0]
        self.assertEqual(ready["task_id"], "legacy")
        self.assertIn("legacy.log", ready["evidence_paths"])
        self.assertIn("legacy.log", data["evidence_files"])
        self.assertNotIn("l", data["evidence_files"])
        self.assertIn(status["status_path"], ready["evidence_paths"])

    def test_task_with_invalid_legacy_trigger_defaults_to_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy-trigger"),
                {
                    "task_id": "legacy-trigger",
                    "status": "waiting",
                    "instruction": "Inspect legacy trigger",
                    "after_job_id": "job",
                    "trigger_on": "success",
                },
            )

            data = json.loads(self.cli(["task", "next", "--json"], tmp).stdout)

        self.assertEqual(data["task_id"], "legacy-trigger")
        self.assertEqual(data["trigger_on"], "succeeded")
        self.assertEqual(data["effective_status"], "ready")

    def test_task_with_padded_legacy_after_event_id_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_status(paths, "job")
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy-event"),
                {
                    "task_id": "legacy-event",
                    "status": "waiting",
                    "instruction": "Inspect legacy event",
                    "after_event_id": f" {status['event_id']} ",
                    "trigger_on": "succeeded",
                },
            )

            data = json.loads(self.cli(["task", "next", "--json"], tmp).stdout)

        self.assertEqual(data["task_id"], "legacy-event")
        self.assertEqual(data["after_event_id"], status["event_id"])
        self.assertEqual(data["effective_status"], "ready")

    def test_stale_in_progress_and_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds").replace("+00:00", "Z")
            task = tmux_state.build_task(
                task_id="stale",
                instruction="Resume stale",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            task["status"] = "in_progress"
            task["claimed_at"] = old
            task["completed_at"] = old
            task["blocked_reason"] = "old block"
            tmux_state.write_task(paths, task)

            load = json.loads(self.cli(["task", "load", "--json"], tmp).stdout)
            text_load = self.cli(["task", "load"], tmp).stdout
            for_skill_load = self.cli(["task", "load", "--for-skill"], tmp).stdout
            self.assertEqual(load["blocked"][0]["task_id"], "stale")
            claimed = json.loads(self.cli(["task", "claim", "--task-id", "stale", "--reclaim-stale"], tmp).stdout)
            self.assertEqual(claimed["status"], "in_progress")
            self.assertIsNone(claimed["completed_at"])
            self.assertIsNone(claimed["blocked_reason"])
            self.assertIn("stale [stale] Resume stale", text_load)
            self.assertIn("## Blocked or stale", for_skill_load)
            self.assertIn("stale [stale] Resume stale", for_skill_load)

    def test_run_next_instruction_creates_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent: dict[str, str] = {}

            def fake_send(args: argparse.Namespace) -> dict[str, object]:
                sent["text"] = args.command_text
                return {"sent_to_pane": True}

            args = argparse.Namespace(
                pane="%1",
                command_text="printf ok",
                command_file=None,
                job_id="job-next",
                name="job",
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction="Summarize the result",
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send", side_effect=fake_send):
                result = tmux_control.run_job(args)

            self.assertTrue(result["next_task"])
            task_id = result["next_task"]["task_id"]
            paths = tmux_state.state_paths(tmp)
            task = tmux_state.read_json(tmux_state.task_path(paths, task_id))[0]
            self.assertEqual(task["status"], "waiting")
            self.assertEqual(task["after_job_id"], "job-next")
            self.assertEqual(task["instruction"], "Summarize the result")

    def test_run_next_instruction_file_creates_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instruction_file = Path(tmp) / "next.txt"
            instruction_file.write_text("Inspect file result\nChoose the next run\n", encoding="utf-8")

            args = argparse.Namespace(
                pane="%1",
                command_text="printf ok",
                command_file=None,
                job_id="job-next-file",
                name="job",
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=str(instruction_file),
                next_on="terminal",
            )
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                result = tmux_control.run_job(args)

            self.assertTrue(result["next_task"])
            task_id = result["next_task"]["task_id"]
            paths = tmux_state.state_paths(tmp)
            task = tmux_state.read_json(tmux_state.task_path(paths, task_id))[0]
            self.assertEqual(task["status"], "waiting")
            self.assertEqual(task["after_job_id"], "job-next-file")
            self.assertEqual(task["trigger_on"], "terminal")
            self.assertEqual(task["instruction"], "Inspect file result\nChoose the next run\n")

    def autopilot_start_args(self, workspace: str, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "objective_id": "train",
            "pane": "%1",
            "command_text": "python train.py",
            "command_file": None,
            "goal": "Finish training",
            "cwd": None,
            "max_attempts": 3,
            "require_idle_shell": False,
            "workspace": workspace,
            "state_dir": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def autopilot_simple_args(self, workspace: str, objective_id: str = "train", **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "objective_id": objective_id,
            "command_text": None,
            "command_file": None,
            "require_idle_shell": False,
            "workspace": workspace,
            "state_dir": None,
            "autopilot_action": "status",
            "reason": None,
            "for_agent": False,
            "json": False,
            "max_chars": tmux_control.AUTOPILOT_TICK_MAX_CHARS,
            "kind": "status",
            "attempt": "current",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_autopilot_start_snapshots_command_file_and_starts_first_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command_file = Path(tmp) / "train.sh"
            command_file.write_text("python train.py --epochs 1\n", encoding="utf-8")
            args = self.autopilot_start_args(tmp, command_text=None, command_file=str(command_file))

            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                result = tmux_control.autopilot_start(args)

            command_file.write_text("python changed.py\n", encoding="utf-8")
            paths = tmux_state.state_paths(tmp)
            objective = tmux_state.read_json(tmux_state.objective_path(paths, "train"))[0]

        self.assertTrue(result["started"])
        assert objective is not None
        self.assertEqual(objective["command_snapshot"], "python train.py --epochs 1\n")
        self.assertEqual(objective["current_attempt"]["job_id"], "train-attempt-1")
        self.assertEqual(objective["attempts"][0]["attempt"], 1)

    def test_autopilot_tick_completes_succeeded_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            self.write_status(paths, "train-attempt-1", "succeeded")

            tick = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))
            objective = tmux_state.read_json(tmux_state.objective_path(paths, "train"))[0]

        self.assertEqual(tick["action"], "completed")
        assert objective is not None
        self.assertEqual(objective["status"], "succeeded")
        self.assertIsNotNone(objective["completed_at"])

    def test_autopilot_tick_claims_repair_once_for_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            self.write_status(paths, "train-attempt-1", "failed")

            first = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))
            second = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))

        self.assertEqual(first["action"], "repair")
        self.assertEqual(first["status"], "repairing")
        self.assertIn("bounded workspace repairs", first["agent_instruction"])
        self.assertIn("--state-dir", " ".join(first["commands"]))
        self.assertEqual(second["action"], "no_action")
        self.assertEqual(second["reason"], "repair already claimed")
        self.assertEqual(first["lease"]["attempt_job_id"], "train-attempt-1")

    def test_autopilot_tick_includes_bounded_summary_and_evidence_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            status = self.write_status(paths, "train-attempt-1", "failed")
            status["last_output"] = "prefix-" + ("x" * 40)
            tmux_state.write_status(tmux_state.status_path(paths, "train-attempt-1"), status)

            tick = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick", max_chars=12))

        self.assertEqual(tick["action"], "repair")
        for field in ("objective_id", "status", "action", "reason", "evidence_paths", "policy", "commands", "agent_instruction"):
            self.assertIn(field, tick)
        summary = tick["attempt_summary"]
        self.assertEqual(summary["attempt_job_id"], "train-attempt-1")
        self.assertEqual(summary["attempt_status"], "failed")
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["last_output_tail"], "x" * 12)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["total_chars_known"], 47)
        self.assertEqual(summary["source"], "status.last_output")
        self.assertIn("evidence_commands", tick)
        self.assertIn("--kind status", tick["evidence_commands"][0]["command"])
        self.assertIn("--max-chars 8000", tick["evidence_commands"][0]["command"])

    def test_autopilot_tick_no_action_does_not_offer_evidence_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            self.write_status(paths, "train-attempt-1", "failed")
            tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))

            duplicate = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))

        self.assertEqual(duplicate["action"], "no_action")
        self.assertNotIn("evidence_commands", duplicate)
        self.assertIn("do not open evidence files", duplicate["agent_instruction"])
        self.assertEqual(duplicate["attempt_summary"]["attempt_status"], "failed")
        self.assertEqual(duplicate["attempt_summary"]["source"], "status.last_output")

    def test_autopilot_rerun_uses_snapshot_and_increments_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent: list[str] = []

            def fake_send(args: argparse.Namespace) -> dict[str, object]:
                sent.append(args.command_text)
                return {"sent_to_pane": True}

            with mock.patch.object(tmux_control, "send", side_effect=fake_send):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp, command_text="python train.py"))
                paths = tmux_state.state_paths(tmp)
                self.write_status(paths, "train-attempt-1", "failed")
                tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))
                result = tmux_control.autopilot_rerun(self.autopilot_simple_args(tmp, autopilot_action="rerun"))

            objective = result["objective"]

        self.assertEqual(result["action"], "rerun_started")
        self.assertEqual(objective["status"], "active")
        self.assertIsNone(objective["lease"])
        self.assertEqual(objective["current_attempt"]["job_id"], "train-attempt-2")
        self.assertEqual(len(objective["attempts"]), 2)
        self.assertIn("train-attempt-2", sent[-1])

    def test_autopilot_rerun_send_failure_stays_repairable_until_attempts_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            send_results = [
                {"sent_to_pane": True},
                {"sent_to_pane": False, "reason": "pane is busy"},
                {"sent_to_pane": False, "reason": "pane is missing"},
            ]

            with mock.patch.object(tmux_control, "send", side_effect=send_results):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp, max_attempts=3))
                paths = tmux_state.state_paths(tmp)
                self.write_status(paths, "train-attempt-1", "failed")
                tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))
                failed_rerun = tmux_control.autopilot_rerun(self.autopilot_simple_args(tmp, autopilot_action="rerun"))
                exhausted_rerun = tmux_control.autopilot_rerun(self.autopilot_simple_args(tmp, autopilot_action="rerun"))

            objective = tmux_state.read_json(tmux_state.objective_path(paths, "train"))[0]

        self.assertEqual(failed_rerun["action"], "rerun_failed")
        self.assertEqual(failed_rerun["objective"]["status"], "repairing")
        self.assertEqual(failed_rerun["objective"]["lease"]["attempt_job_id"], "train-attempt-2")
        self.assertIn("pane is busy", failed_rerun["reason"])
        self.assertIn("autopilot block", " ".join(failed_rerun["commands"]))
        self.assertEqual(exhausted_rerun["action"], "blocked")
        self.assertIn("maximum attempts reached", exhausted_rerun["reason"])
        assert objective is not None
        self.assertEqual(objective["status"], "blocked")
        self.assertEqual(objective["current_attempt"]["job_id"], "train-attempt-3")
        self.assertEqual(len(objective["attempts"]), 3)

    def test_autopilot_evidence_status_and_log_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            status = self.write_status(paths, "train-attempt-1", "failed")
            status["last_output"] = "status-" + ("a" * 20)
            tmux_state.write_status(tmux_state.status_path(paths, "train-attempt-1"), status)
            tmux_state.log_path(paths, "train-attempt-1").write_text("log-" + ("b" * 20), encoding="utf-8")

            status_evidence = tmux_control.autopilot_evidence(
                self.autopilot_simple_args(tmp, autopilot_action="evidence", kind="status", max_chars=6)
            )
            log_evidence = tmux_control.autopilot_evidence(
                self.autopilot_simple_args(tmp, autopilot_action="evidence", kind="log", max_chars=7)
            )
            zero_status = tmux_control.autopilot_evidence(
                self.autopilot_simple_args(tmp, autopilot_action="evidence", kind="status", max_chars=0)
            )

        self.assertEqual(status_evidence["kind"], "status")
        self.assertTrue(status_evidence["readable"])
        self.assertEqual(status_evidence["content"], "a" * 6)
        self.assertTrue(status_evidence["truncated"])
        self.assertEqual(status_evidence["total_chars_known"], 27)
        self.assertEqual(status_evidence["status_core"]["status"], "failed")
        self.assertEqual(log_evidence["kind"], "log")
        self.assertEqual(log_evidence["content"], "b" * 7)
        self.assertIsNone(log_evidence["status_core"])
        self.assertEqual(zero_status["content"], "")
        self.assertTrue(zero_status["content_omitted"])
        self.assertEqual(zero_status["total_chars_known"], 27)
        self.assertEqual(zero_status["status_core"]["status"], "failed")

    def test_autopilot_evidence_reports_missing_and_malformed_artifacts_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)

            missing_log = tmux_control.autopilot_evidence(
                self.autopilot_simple_args(tmp, autopilot_action="evidence", kind="log", max_chars=0)
            )
            tmux_state.status_path(paths, "train-attempt-1").write_text("{not-json", encoding="utf-8")
            malformed_status = tmux_control.autopilot_evidence(
                self.autopilot_simple_args(tmp, autopilot_action="evidence", kind="status", max_chars=0)
            )

        self.assertFalse(missing_log["exists"])
        self.assertFalse(missing_log["readable"])
        self.assertEqual(missing_log["content"], "")
        self.assertTrue(malformed_status["exists"])
        self.assertFalse(malformed_status["readable"])
        self.assertIn("Expecting property name", malformed_status["error"])

    def test_autopilot_blocks_at_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp, max_attempts=1))
            paths = tmux_state.state_paths(tmp)
            self.write_status(paths, "train-attempt-1", "failed")

            tick = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))
            objective = tmux_state.read_json(tmux_state.objective_path(paths, "train"))[0]

        self.assertEqual(tick["action"], "blocked")
        assert objective is not None
        self.assertEqual(objective["status"], "blocked")
        self.assertEqual(objective["blocked_reason"], "maximum attempts reached")

    def test_autopilot_heartbeat_prompt_contains_policy_and_tick_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))

            prompt = tmux_control.autopilot_heartbeat_prompt(self.autopilot_simple_args(tmp, autopilot_action="heartbeat-prompt"))

        self.assertIn("autopilot tick", prompt)
        self.assertIn("--objective-id train", prompt)
        self.assertIn("Bounded repair", prompt)
        self.assertIn("heartbeat can be paused or removed", prompt)

    def test_autopilot_cancelled_tick_reports_cancelled_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            tmux_control.autopilot_finish(self.autopilot_simple_args(tmp, autopilot_action="cancel"), "cancelled")

            tick = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))

        self.assertEqual(tick["action"], "cancelled")
        self.assertEqual(tick["status"], "cancelled")
        self.assertEqual(tick["attempt_summary"]["attempt_job_id"], "train-attempt-1")

    def test_autopilot_tick_reclaims_malformed_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": True}):
                tmux_control.autopilot_start(self.autopilot_start_args(tmp))
            paths = tmux_state.state_paths(tmp)
            self.write_status(paths, "train-attempt-1", "failed")
            objective = tmux_state.read_json(tmux_state.objective_path(paths, "train"))[0]
            assert objective is not None
            objective["status"] = "repairing"
            objective["lease"] = {"expires_at": "not-a-date", "attempt_job_id": "train-attempt-1"}
            tmux_state.atomic_write_json(tmux_state.objective_path(paths, "train"), objective)

            tick = tmux_control.autopilot_tick(self.autopilot_simple_args(tmp, autopilot_action="tick"))

        self.assertEqual(tick["action"], "repair")
        self.assertNotEqual(tick["lease"]["expires_at"], "not-a-date")


if __name__ == "__main__":
    unittest.main()
