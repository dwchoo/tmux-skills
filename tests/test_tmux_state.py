from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_state


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGED_WORKERS_DOC = PROJECT_ROOT / "docs" / "managed-workers.md"


class TmuxStateTests(unittest.TestCase):
    @staticmethod
    def markdown_bullet_tokens_after_heading(text: str, heading: str) -> set[str]:
        pattern = rf"^{re.escape(heading)}:\n\n(?P<body>(?:- `[^`]+`\n)+)"
        match = re.search(pattern, text, flags=re.MULTILINE)
        if not match:
            raise AssertionError(f"missing managed worker state list: {heading}")
        return set(re.findall(r"^- `([^`]+)`$", match.group("body"), flags=re.MULTILINE))

    def test_managed_worker_state_model_doc_matches_code_status_sets(self) -> None:
        doc = MANAGED_WORKERS_DOC.read_text(encoding="utf-8")

        self.assertEqual(
            self.markdown_bullet_tokens_after_heading(doc, "Active managed states"),
            tmux_state.MANAGED_ACTIVE_STATUSES,
        )
        self.assertEqual(
            self.markdown_bullet_tokens_after_heading(doc, "Terminal managed states"),
            tmux_state.MANAGED_TERMINAL_STATUSES,
        )

    def test_state_dir_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace), "state")
            self.assertEqual(paths["root"], (workspace / "state").resolve())

            absolute = Path(tmp) / "absolute"
            paths = tmux_state.state_paths(str(workspace), str(absolute))
            self.assertEqual(paths["root"], absolute.resolve())

    def test_status_tail_takes_lines_before_character_cap(self) -> None:
        text = "\n".join(f"line {index}" for index in range(15))
        self.assertEqual(tmux_state.status_tail(text, lines=3, max_chars=100), "line 12\nline 13\nline 14")
        self.assertEqual(tmux_state.status_tail(text, lines=3, max_chars=9), "3\nline 14")

    def test_status_tail_handles_empty_boundaries_and_crlf(self) -> None:
        self.assertEqual(tmux_state.status_tail("", lines=10, max_chars=1200), "")
        self.assertEqual(tmux_state.status_tail("one\r\ntwo\rthree", lines=2, max_chars=1200), "two\nthree")
        self.assertEqual(tmux_state.status_tail("x" * 1200, lines=10, max_chars=1200), "x" * 1200)
        self.assertEqual(tmux_state.status_tail("x" * 1201, lines=10, max_chars=1200), "x" * 1200)

    def test_status_tail_rejects_nonpositive_limits(self) -> None:
        with self.assertRaises(ValueError):
            tmux_state.status_tail("text", lines=0, max_chars=1200)
        with self.assertRaises(ValueError):
            tmux_state.status_tail("text", lines=10, max_chars=0)

    def test_atomic_status_write_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job 1")
            status = tmux_state.build_status(
                kind="job",
                item_id="job 1",
                attempt=1,
                name="test",
                status="succeeded",
                pane_id="%1",
                command_preview_text="echo ok",
                cwd=tmp,
                status_file=status_file,
                log_file=tmux_state.log_path(paths, "job 1"),
                exit_code=0,
                last_output="ok",
            )
            tmux_state.write_status(status_file, status)

            statuses, errors = tmux_state.load_statuses(paths["root"])
            self.assertEqual(errors, [])
            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0]["status"], "succeeded")
            self.assertTrue(statuses[0]["event_id"])

    def test_write_status_canonicalizes_identity_and_status_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job with space")

            written = tmux_state.write_status(
                status_file,
                {
                    "id": "wrong id",
                    "job_id": "wrong id",
                    "status": "succeeded",
                    "status_path": "/stale/status.json",
                    "last_output": "ok",
                    "exit_code": 0,
                },
            )
            stored, error = tmux_state.read_json(status_file)

        self.assertIsNone(error)
        assert stored is not None
        self.assertEqual(written["id"], "job-with-space")
        self.assertEqual(written["job_id"], "job-with-space")
        self.assertEqual(written["status_path"], str(status_file))
        self.assertEqual(stored["id"], "job-with-space")
        self.assertEqual(stored["job_id"], "job-with-space")
        self.assertEqual(stored["status_path"], str(status_file))
        self.assertTrue(stored["event_id"])

    def test_write_status_terminal_transition_uses_write_time_as_ended_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")
            running = {
                "id": "job",
                "status": "running",
                "started_at": "2026-05-30T00:00:00Z",
                "updated_at": "2026-05-30T00:01:00Z",
                "last_output": "still running",
            }
            finished = dict(running)
            finished.update({"status": "succeeded", "exit_code": 0, "last_output": "done"})

            with mock.patch.object(tmux_state, "utc_now", return_value="2026-05-30T00:05:00Z"):
                stored = tmux_state.write_status(status_file, finished)

        self.assertEqual(stored["updated_at"], "2026-05-30T00:05:00Z")
        self.assertEqual(stored["ended_at"], "2026-05-30T00:05:00Z")
        self.assertTrue(stored["event_id"])

    def test_write_status_preserves_explicit_terminal_ended_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")

            with mock.patch.object(tmux_state, "utc_now", return_value="2026-05-30T00:05:00Z"):
                stored = tmux_state.write_status(
                    status_file,
                    {
                        "id": "job",
                        "status": "succeeded",
                        "ended_at": "2026-05-30T00:04:00Z",
                        "last_output": "done",
                    },
                )

        self.assertEqual(stored["updated_at"], "2026-05-30T00:05:00Z")
        self.assertEqual(stored["ended_at"], "2026-05-30T00:04:00Z")
        self.assertTrue(stored["event_id"])

    def test_write_status_normalizes_terminal_status_before_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")

            with mock.patch.object(tmux_state, "utc_now", return_value="2026-05-30T00:05:00Z"):
                stored = tmux_state.write_status(
                    status_file,
                    {
                        "id": "job",
                        "status": " Succeeded ",
                        "last_output": "done",
                    },
                )

        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["ended_at"], "2026-05-30T00:05:00Z")
        self.assertTrue(stored["event_id"])

    def test_corrupt_json_is_soft_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "bad.json").write_text("{", encoding="utf-8")

            statuses, errors = tmux_state.load_statuses(paths["root"])
            self.assertEqual(statuses, [])
            self.assertEqual(len(errors), 1)

    def test_invalid_utf8_json_is_soft_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "bad-encoding.json").write_bytes(b"\xff\xfe{")

            statuses, errors = tmux_state.load_statuses(paths["root"])

        self.assertEqual(statuses, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("codec", errors[0]["error"])

    def test_normalized_legacy_terminal_status_gets_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "old.json").write_text(
                '{"id": "old", "status": "succeeded", "updated_at": "2026-05-30T00:00:00Z"}',
                encoding="utf-8",
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["status"], "succeeded")
        self.assertEqual(statuses[0]["ended_at"], "2026-05-30T00:00:00Z")
        self.assertTrue(statuses[0]["event_id"])

    def test_normalized_terminal_status_with_event_id_gets_ended_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "succeeded",
                    "event_id": "legacy-event",
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(statuses[0]["ended_at"], "2026-05-30T00:00:00Z")
        self.assertEqual(statuses[0]["event_id"], "legacy-event")

    def test_normalized_terminal_status_normalizes_legacy_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "succeeded",
                    "event_id": 12345,
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(statuses[0]["event_id"], "12345")

    def test_normalized_status_strips_status_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "kind": " Job ",
                    "status": " Succeeded ",
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(statuses[0]["kind"], "job")
        self.assertEqual(statuses[0]["status"], "succeeded")
        self.assertEqual(statuses[0]["ended_at"], "2026-05-30T00:00:00Z")
        self.assertTrue(statuses[0]["event_id"])

    def test_normalized_status_stringifies_and_truncates_legacy_last_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "failed",
                    "updated_at": "2026-05-30T00:00:00Z",
                    "last_output": {"tail": "x" * 5000},
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertIsInstance(statuses[0]["last_output"], str)
        self.assertLessEqual(len(statuses[0]["last_output"]), 4000)
        self.assertTrue(statuses[0]["event_id"])

    def test_normalized_status_stringifies_and_compacts_legacy_command_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "running",
                    "command_preview": {"argv": ["python", "script.py"], "padding": "x" * 500},
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertIsInstance(statuses[0]["command_preview"], str)
        self.assertLessEqual(len(statuses[0]["command_preview"]), 200)
        self.assertNotIn("\n", statuses[0]["command_preview"])

    def test_normalized_non_terminal_status_clears_terminal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "running",
                    "ended_at": "2026-05-30T00:00:00Z",
                    "event_id": "old-event",
                    "updated_at": "2026-05-30T00:01:00Z",
                },
            )

            statuses, errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(statuses[0]["status"], "running")
        self.assertIsNone(statuses[0]["ended_at"])
        self.assertIsNone(statuses[0]["event_id"])

    def test_write_status_non_terminal_clears_terminal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "job")

            stored = tmux_state.write_status(
                status_file,
                {
                    "id": "job",
                    "status": "running",
                    "ended_at": "2026-05-30T00:00:00Z",
                    "event_id": "old-event",
                    "last_output": "still running",
                },
            )
            raw, error = tmux_state.read_json(status_file)

        self.assertIsNone(error)
        assert raw is not None
        self.assertIsNone(stored["ended_at"])
        self.assertIsNone(stored["event_id"])
        self.assertIsNone(raw["ended_at"])
        self.assertIsNone(raw["event_id"])

    def test_normalized_status_uses_actual_status_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_path = tmux_state.status_path(paths, "job")
            tmux_state.atomic_write_json(
                status_path,
                {
                    "id": "job",
                    "status": "succeeded",
                    "status_path": "/stale/status.json",
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect current status path",
                summary=None,
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            state = tmux_state.load_task_state(paths)

        self.assertEqual(state["statuses"][0]["status_path"], str(status_path))
        self.assertIn(str(status_path), state["tasks"][0]["evidence_paths"])
        self.assertNotIn("/stale/status.json", state["tasks"][0]["evidence_paths"])

    def test_normalized_status_uses_file_id_when_json_id_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_path = tmux_state.status_path(paths, "file-id")
            tmux_state.atomic_write_json(
                status_path,
                {
                    "id": "wrong-id",
                    "job_id": "wrong-id",
                    "status": "succeeded",
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect file identity",
                summary=None,
                intent=None,
                after_job_id="file-id",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        self.assertEqual(state["statuses"][0]["id"], "file-id")
        self.assertEqual(state["statuses"][0]["job_id"], "file-id")
        self.assertEqual(classified["ready_tasks"][0]["task_id"], "follow-up")

    def test_legacy_terminal_status_without_timestamp_gets_stable_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_path = paths["status"] / "old.json"
            status_path.write_text('{"id": "old", "status": "succeeded"}', encoding="utf-8")

            with mock.patch.object(tmux_state, "utc_now", return_value="2099-01-01T00:00:00Z"):
                first, first_errors = tmux_state.load_statuses_normalized(paths["root"])
            with mock.patch.object(tmux_state, "utc_now", return_value="2100-01-01T00:00:00Z"):
                second, second_errors = tmux_state.load_statuses_normalized(paths["root"])

        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(first[0]["event_id"], second[0]["event_id"])
        self.assertNotEqual(first[0]["ended_at"], "2099-01-01T00:00:00Z")
        self.assertNotEqual(second[0]["ended_at"], "2100-01-01T00:00:00Z")

    def test_ack_by_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status_file = tmux_state.status_path(paths, "same")
            first = tmux_state.build_status(
                kind="job",
                item_id="same",
                attempt=1,
                name=None,
                status="failed",
                pane_id="%1",
                command_preview_text="false",
                cwd=tmp,
                status_file=status_file,
                log_file=None,
                exit_code=1,
                last_output="first",
            )
            tmux_state.write_status(status_file, first)
            self.assertFalse(tmux_state.is_acked(paths, first))
            tmux_state.ack_status(paths, first)
            self.assertTrue(tmux_state.is_acked(paths, first))

            second = dict(first)
            second["attempt"] = 2
            second["last_output"] = "second"
            second["event_id"] = tmux_state.terminal_event_id(second)
            self.assertFalse(tmux_state.is_acked(paths, second))

    def test_managed_job_stale_reason_requires_active_and_old_heartbeat(self) -> None:
        fresh = {
            "job_id": "fresh",
            "status": "waiting_status",
            "pid": 0,
            "heartbeat_at": tmux_state.utc_now(),
            "check_interval_seconds": 1,
        }
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
        old = dict(fresh, job_id="old", heartbeat_at=old_time)
        submitted = dict(old, status="submitted")

        self.assertIsNone(tmux_state.managed_job_stale_reason(fresh, pid_running=False, pid_matches=False))
        self.assertIn("heartbeat older", tmux_state.managed_job_stale_reason(old, pid_running=False, pid_matches=False) or "")
        self.assertIsNone(tmux_state.managed_job_stale_reason(submitted, pid_running=False, pid_matches=False))

    def test_managed_worker_command_match_requires_exact_job_id_arg(self) -> None:
        command = "python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id abc --pane %1"
        self.assertTrue(tmux_state.managed_worker_command_matches(command, "abc"))
        self.assertFalse(tmux_state.managed_worker_command_matches(command, "ab"))
        self.assertFalse(tmux_state.managed_worker_command_matches(command, "abc-extra"))

    def test_managed_worker_command_match_rejects_embedded_script_text(self) -> None:
        command = "python3 -c 'tmux_queue.py queue-after-idle --job-id abc'"
        self.assertFalse(tmux_state.managed_worker_command_matches(command, "abc"))

    def test_managed_worker_command_match_accepts_equals_job_id_form(self) -> None:
        command = "python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id=abc --pane %1"
        self.assertTrue(tmux_state.managed_worker_command_matches(command, "abc"))
        self.assertFalse(tmux_state.managed_worker_command_matches(command, "ab"))

    def test_managed_worker_command_match_requires_worker_action(self) -> None:
        self.assertFalse(tmux_state.managed_worker_command_matches("python3 /repo/scripts/tmux_queue.py --job-id abc", "abc"))
        self.assertFalse(tmux_state.managed_worker_command_matches("python3 /repo/scripts/tmux_queue.py not-worker --job-id abc", "abc"))

    def test_load_task_state_marks_stale_managed_job_and_excludes_from_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "old"),
                {
                    "job_id": "old",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": old_time,
                    "updated_at": old_time,
                    "check_interval_seconds": 1,
                },
            )

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        self.assertEqual(len(state["jobs"]), 1)
        self.assertTrue(state["jobs"][0]["stale"])
        self.assertIn("no pid recorded", state["jobs"][0]["stale_reason"])
        self.assertEqual(classified["running"], [])

    def test_load_task_state_marks_dead_pid_managed_job_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "dead"),
                {
                    "job_id": "dead",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": old_time,
                    "updated_at": old_time,
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_state, "pid_is_running", return_value=False):
                state = tmux_state.load_task_state(paths)
                classified = tmux_state.classify_task_state(state)

        self.assertTrue(state["jobs"][0]["stale"])
        self.assertFalse(state["jobs"][0]["pid_running"])
        self.assertIn("pid is not running", state["jobs"][0]["stale_reason"])
        self.assertEqual(classified["running"], [])

    def test_load_task_state_tolerates_nonfinite_managed_job_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "nan-interval"),
                {
                    "job_id": "nan-interval",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": "2000-01-01T00:00:00Z",
                    "updated_at": "2000-01-01T00:00:00Z",
                    "check_interval_seconds": "nan",
                },
            )

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        self.assertEqual(state["errors"], [])
        self.assertTrue(state["jobs"][0]["stale"])
        self.assertIn("heartbeat older than 300s", state["jobs"][0]["stale_reason"])
        self.assertEqual(classified["running"], [])

    def test_load_task_state_marks_foreign_live_pid_managed_job_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            foreign = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                tmux_state.atomic_write_json(
                    tmux_state.job_path(paths, "foreign"),
                    {
                        "job_id": "foreign",
                        "kind": "queue-after-idle",
                        "status": "waiting_pane_idle",
                        "pid": foreign.pid,
                        "pane_id": "%1",
                        "heartbeat_at": old_time,
                        "updated_at": old_time,
                        "check_interval_seconds": 1,
                    },
                )

                state = tmux_state.load_task_state(paths)
                classified = tmux_state.classify_task_state(state)
            finally:
                foreign.terminate()
                try:
                    foreign.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    foreign.kill()
                    foreign.wait(timeout=5)

        self.assertTrue(state["jobs"][0]["pid_running"])
        self.assertFalse(state["jobs"][0]["pid_matches"])
        self.assertTrue(state["jobs"][0]["stale"])
        self.assertIn("pid is not a tmux-skills worker", state["jobs"][0]["stale_reason"])
        self.assertEqual(classified["running"], [])

    def test_load_task_state_sanitizes_legacy_managed_job_id_for_pid_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "watch",
                    "status": "running",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": old_time,
                    "updated_at": old_time,
                    "check_interval_seconds": 1,
                },
            )

            command = "python3 /repo/scripts/tmux_queue.py watch --job-id job-with-space --pane %1"
            with mock.patch.object(tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_state, "process_command_line", return_value=command):
                    state = tmux_state.load_task_state(paths)

        self.assertEqual(state["jobs"][0]["job_id"], "job-with-space")
        self.assertTrue(state["jobs"][0]["pid_matches"])
        self.assertFalse(state["jobs"][0]["stale"])

    def test_load_task_state_drops_legacy_stale_reason_when_job_is_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "fresh"),
                {
                    "job_id": "fresh",
                    "kind": "queue-after-idle",
                    "status": "starting",
                    "pid": 0,
                    "pane_id": "%1",
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            state = tmux_state.load_task_state(paths)

        self.assertFalse(state["jobs"][0]["stale"])
        self.assertNotIn("stale_reason", state["jobs"][0])

    def test_normalized_managed_job_uses_actual_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            job_path = tmux_state.job_path(paths, "job-with-space")
            tmux_state.atomic_write_json(
                job_path,
                {
                    "job_id": "job with space",
                    "kind": " Watch ",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "job_path": "/stale/job.json",
                    "status_path": "/stale/status.json",
                    "log_path": "/stale/log.log",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )

            jobs, errors = tmux_state.load_managed_jobs(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(jobs[0]["job_id"], "job-with-space")
        self.assertEqual(jobs[0]["kind"], "watch")
        self.assertEqual(jobs[0]["job_path"], str(job_path))
        self.assertEqual(jobs[0]["status_path"], str(tmux_state.status_path(paths, "job-with-space")))
        self.assertEqual(jobs[0]["log_path"], str(tmux_state.log_path(paths, "job-with-space")))

    def test_normalized_managed_job_uses_file_id_when_json_id_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "file-id"),
                {
                    "id": "wrong-id",
                    "job_id": "wrong-id",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )

            jobs, errors = tmux_state.load_managed_jobs(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(jobs[0]["job_id"], "file-id")
        self.assertEqual(jobs[0]["id"], "file-id")
        self.assertEqual(jobs[0]["status_path"], str(tmux_state.status_path(paths, "file-id")))

    def test_load_task_state_normalizes_legacy_managed_job_status_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": " Watch ",
                    "status": " Starting ",
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        self.assertEqual(state["jobs"][0]["status"], "starting")
        self.assertEqual(state["jobs"][0]["kind"], "watch")
        self.assertEqual(classified["running"][0]["job_id"], "watch")

    def test_load_tasks_uses_file_id_when_json_task_id_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "file-id"),
                {
                    "task_id": "wrong-id",
                    "status": "waiting",
                    "instruction": "Inspect file identity",
                    "after_job_id": "job",
                    "trigger_on": "succeeded",
                },
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(tasks[0]["task_id"], "file-id")
        self.assertEqual(tasks[0]["task_path"], str(tmux_state.task_path(paths, "file-id")))

    def test_load_tasks_normalizes_invalid_trigger_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy"),
                {
                    "task_id": "legacy",
                    "status": "waiting",
                    "instruction": "Inspect legacy trigger",
                    "after_job_id": "job",
                    "trigger_on": "success",
                },
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(tasks[0]["trigger_on"], "succeeded")

    def test_load_tasks_normalizes_legacy_status_and_trigger_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy"),
                {
                    "task_id": "legacy",
                    "status": " BLOCKED ",
                    "instruction": "Inspect legacy status token",
                    "after_job_id": "job",
                    "trigger_on": " FAILED ",
                },
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])

        self.assertEqual(errors, [])
        self.assertEqual(tasks[0]["status"], "blocked")
        self.assertEqual(tasks[0]["trigger_on"], "failed")

    def test_failed_trigger_matches_unsuccessful_terminal_statuses_only(self) -> None:
        for status in sorted(tmux_state.FAILED_TRIGGER_STATUSES):
            with self.subTest(status=status):
                self.assertTrue(tmux_state.status_matches_trigger({"status": status}, "failed"))

        for status in ("succeeded", "matched", "submitted", "running", "waiting"):
            with self.subTest(status=status):
                self.assertFalse(tmux_state.status_matches_trigger({"status": status}, "failed"))

    def test_load_tasks_normalizes_blank_legacy_after_job_id_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job"),
                {
                    "id": "job",
                    "status": "succeeded",
                    "updated_at": "2026-05-30T00:00:00Z",
                },
            )
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "legacy"),
                {
                    "task_id": "legacy",
                    "status": "waiting",
                    "instruction": "Inspect blank anchor",
                    "after_job_id": "   ",
                    "after_event_id": None,
                    "trigger_on": "succeeded",
                },
            )

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        task = state["tasks"][0]
        self.assertIsNone(task["after_job_id"])
        self.assertEqual(task["effective_status"], "waiting")
        self.assertEqual(classified["ready_tasks"], [])

    def test_load_tasks_normalizes_legacy_after_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "number-event"),
                {
                    "task_id": "number-event",
                    "status": "waiting",
                    "instruction": "Inspect numeric event",
                    "after_event_id": 12345,
                    "trigger_on": "succeeded",
                },
            )
            tmux_state.atomic_write_json(
                tmux_state.task_path(paths, "spaced-event"),
                {
                    "task_id": "spaced-event",
                    "status": "waiting",
                    "instruction": "Inspect spaced event",
                    "after_event_id": " event-abc ",
                    "trigger_on": "succeeded",
                },
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])

        tasks_by_id = {task["task_id"]: task for task in tasks}
        self.assertEqual(errors, [])
        self.assertEqual(tasks_by_id["number-event"]["after_event_id"], "12345")
        self.assertEqual(tasks_by_id["spaced-event"]["after_event_id"], "event-abc")

    def test_age_seconds_handles_naive_timestamp(self) -> None:
        parsed = tmux_state.parse_time("2026-05-30T12:00:00")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNotNone(parsed.tzinfo)
        self.assertIsNotNone(parsed.tzinfo.utcoffset(parsed))

        now = datetime(2026, 5, 30, 12, 1, tzinfo=timezone.utc)
        self.assertEqual(tmux_state.age_seconds("2026-05-30T12:00:00", now=now), 60.0)
        self.assertIsNone(tmux_state.age_seconds("not a timestamp", now=now))

    def test_stale_detection_with_naive_heartbeat(self) -> None:
        now = datetime(2026, 5, 30, 12, 10, tzinfo=timezone.utc)
        old_time = datetime(2026, 5, 30, 12, 0).isoformat(timespec="seconds")
        record = {
            "job_id": "old",
            "status": "waiting_status",
            "pid": 123,
            "heartbeat_at": old_time,
            "check_interval_seconds": 1,
        }

        reason = tmux_state.managed_job_stale_reason(record, pid_running=False, now=now)
        self.assertIsInstance(reason, str)
        self.assertIn("heartbeat older", reason)

    def test_unanchored_task_does_not_match_unrelated_terminal_status(self) -> None:
        status = {"id": "old", "status": "succeeded", "event_id": "event"}
        task = {
            "task_id": "legacy",
            "status": "waiting",
            "trigger_on": "succeeded",
            "after_job_id": None,
            "after_event_id": None,
        }

        effective, match, stale = tmux_state.effective_task_status(task, [status])

        self.assertEqual(effective, "waiting")
        self.assertIsNone(match)
        self.assertFalse(stale)

    def test_legacy_status_job_id_is_sanitized_for_task_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "job-with-space.json").write_text(
                '{"job_id": "job with space", "status": "succeeded", "updated_at": "2026-05-30T00:00:00Z"}',
                encoding="utf-8",
            )
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect legacy job",
                summary=None,
                intent=None,
                after_job_id="job with space",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)

            state = tmux_state.load_task_state(paths)
            classified = tmux_state.classify_task_state(state)

        self.assertEqual(state["statuses"][0]["id"], "job-with-space")
        self.assertEqual(classified["ready_tasks"][0]["task_id"], "follow-up")

    def test_task_summary_line_compacts_multiline_summary(self) -> None:
        line = tmux_state.task_summary_line(
            {
                "task_id": "task",
                "status": "waiting",
                "summary": "first line\nsecond line",
            }
        )

        self.assertEqual(line, "task [waiting] first line second line")

    def test_task_summary_line_uses_status_fallback_when_effective_status_is_none(self) -> None:
        line = tmux_state.task_summary_line(
            {
                "task_id": "task",
                "status": "waiting",
                "effective_status": None,
                "summary": "inspect",
            }
        )

        self.assertEqual(line, "task [waiting] inspect")

    def test_task_summary_line_labels_stale_tasks(self) -> None:
        line = tmux_state.task_summary_line(
            {
                "task_id": "task",
                "status": "in_progress",
                "effective_status": "in_progress",
                "stale": True,
                "summary": "resume me",
            }
        )

        self.assertEqual(line, "task [stale] resume me")

    def test_one_line_text_preserves_falsy_non_none_values(self) -> None:
        self.assertEqual(tmux_state.one_line_text(0), "0")
        self.assertEqual(tmux_state.one_line_text(False), "False")
        self.assertEqual(tmux_state.one_line_text(None), "")

    def test_bounded_one_line_text_compacts_and_bounds_display_text(self) -> None:
        self.assertEqual(tmux_state.bounded_one_line_text("first\nsecond", limit=20), "first second")
        self.assertEqual(tmux_state.bounded_one_line_text("abcdef", limit=5), "ab...")
        self.assertEqual(tmux_state.bounded_one_line_text("abcdef", limit=5, keep_tail=True), "...ef")
        self.assertEqual(tmux_state.bounded_one_line_text("abcdef", limit=3), "abc")


if __name__ == "__main__":
    unittest.main()
