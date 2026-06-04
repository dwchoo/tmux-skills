from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import codex_app_server_client  # noqa: E402
import tmux_bridge  # noqa: E402
import tmux_state  # noqa: E402


class TmuxBridgeTests(unittest.TestCase):
    def test_state_paths_include_bridge_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace))
            tmux_state.ensure_state_dirs(paths)

            self.assertEqual(paths["bridge"], (workspace / ".codex" / "tmux-skills" / "bridge").resolve())
            self.assertTrue(paths["bridge"].is_dir())

    def test_poc_artifact_paths_stay_under_bridge_state_and_fixture_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            workspace = tmp / "workspace"
            workspace.mkdir()
            fixture_root = tmp / "fixtures"

            paths = tmux_bridge.poc_artifact_paths(str(workspace), "20260604-120000", fixture_root=fixture_root)

            self.assertEqual(paths["runtime"].parent, (workspace / ".codex" / "tmux-skills" / "bridge").resolve())
            self.assertEqual(paths["manual"].parent, (workspace / ".codex" / "tmux-skills" / "bridge").resolve())
            self.assertEqual(paths["fixture"].parent, fixture_root)

    def test_register_writes_bridge_record_under_bridge_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            socket_path = Path(tmp_name) / "app-server.sock"

            result = tmux_bridge.register_bridge(
                thread_id="thr_test",
                endpoint=f"unix://{socket_path}",
                workspace=str(workspace),
                bridge_id="bridge-test",
            )

            self.assertTrue(result["registered"])
            record_path = workspace / ".codex" / "tmux-skills" / "bridge" / "bridge-test.json"
            record, error = tmux_state.read_json(record_path)
            self.assertIsNone(error)
            assert record is not None
            self.assertEqual(record["status"], "registered")
            self.assertEqual(record["socket_path"], str(socket_path))
            self.assertEqual(record["observed_event_ids"], [])

    def test_register_rejects_exact_unix_endpoint_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()

            with self.assertRaises(codex_app_server_client.PermanentAppServerError):
                tmux_bridge.register_bridge(thread_id="thr_test", endpoint="unix://", workspace=str(workspace))

            self.assertFalse((workspace / ".codex" / "tmux-skills" / "bridge").exists())

    def test_prompt_builder_is_path_only_and_excludes_payload_content(self) -> None:
        prompt = tmux_bridge.build_wake_prompt(
            "/workspace",
            {
                "source": "ready_task",
                "job_id": "job-one",
                "job_path": "/workspace/.codex/tmux-skills/jobs/job-one.json",
                "status_path": "/workspace/.codex/tmux-skills/status/job-one.json",
                "task_path": "/workspace/.codex/tmux-skills/tasks/follow-up.json",
                "log_path": "/workspace/.codex/tmux-skills/logs/job-one.log",
                "last_output": "SECRET OUTPUT",
                "instruction": "fix the failing test",
            },
        )

        self.assertTrue(prompt.startswith("tmux-control observed a ready task."))
        for expected in (
            "Workspace: /workspace",
            "Job path: /workspace/.codex/tmux-skills/jobs/job-one.json",
            "Status path: /workspace/.codex/tmux-skills/status/job-one.json",
            "Task path: /workspace/.codex/tmux-skills/tasks/follow-up.json",
            "Log path: /workspace/.codex/tmux-skills/logs/job-one.log",
        ):
            self.assertIn(expected, prompt)
        for forbidden in ("SECRET OUTPUT", "last_output", "instruction", "traceback", "stdout", "stderr", "suggested"):
            self.assertNotIn(forbidden, prompt)

    def test_detects_ready_task_before_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace))
            tmux_state.ensure_state_dirs(paths)
            status = tmux_state.build_status(
                kind="job",
                item_id="job-one",
                attempt=1,
                name=None,
                status="succeeded",
                pane_id="%1",
                command_preview_text="test",
                cwd=str(workspace),
                status_file=tmux_state.status_path(paths, "job-one"),
                log_file=tmux_state.log_path(paths, "job-one"),
                ended_at="2026-06-04T00:00:00Z",
            )
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="do not include this body",
                summary="Follow up",
                intent=None,
                after_job_id="job-one",
                after_event_id=None,
                trigger_on="succeeded",
                evidence_paths=[],
            )
            tmux_state.write_task(paths, task)
            record = tmux_bridge.build_bridge_record(
                bridge_id="bridge-test",
                thread_id="thr_test",
                endpoint="unix:///tmp/app.sock",
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
                poll_seconds=2.0,
                quiet_seconds=10.0,
            )

            candidates, error = tmux_bridge.detect_bridge_candidates(paths, record)
            selected = tmux_bridge.select_bridge_candidate(candidates)

            self.assertIsNone(error)
            assert selected is not None
            self.assertEqual(selected["source"], "ready_task")
            self.assertEqual(selected["task_path"], str(tmux_state.task_path(paths, "follow-up")))

    def test_daemon_cycle_marks_delivered_event_observed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace))
            tmux_state.ensure_state_dirs(paths)
            status = tmux_state.build_status(
                kind="job",
                item_id="job-one",
                attempt=1,
                name=None,
                status="succeeded",
                pane_id="%1",
                command_preview_text="test",
                cwd=str(workspace),
                status_file=tmux_state.status_path(paths, "job-one"),
                log_file=tmux_state.log_path(paths, "job-one"),
                ended_at="2026-06-04T00:00:00Z",
            )
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            record = tmux_bridge.build_bridge_record(
                bridge_id="bridge-test",
                thread_id="thr_test",
                endpoint="unix:///tmp/app.sock",
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
                poll_seconds=2.0,
                quiet_seconds=0.1,
            )

            def fake_delivery(record: dict[str, object], candidate: dict[str, object], prompt: str) -> dict[str, object]:
                self.assertEqual(prompt.splitlines()[0], "tmux-control observed a terminal event.")
                return {
                    "event_id": candidate["event_id"],
                    "delivered_at": tmux_state.utc_now(),
                    "prompt_sha256": tmux_bridge.prompt_sha256(prompt),
                    "response_id": "3",
                    "turn_id": "turn_1",
                    "resume_thread_id": None,
                    "resume_error": "no rollout found",
                }

            with mock.patch.object(tmux_bridge, "deliver_bridge_candidate", side_effect=fake_delivery):
                updated = tmux_bridge.bridge_daemon_cycle(record)
                second = tmux_bridge.bridge_daemon_cycle(updated)

            self.assertEqual(updated["status"], "active")
            self.assertEqual(updated["observed_event_ids"], [status["event_id"]])
            self.assertIsNone(updated["pending_delivery"])
            self.assertEqual(second["observed_event_ids"], [status["event_id"]])

    def test_retryable_delivery_failure_leaves_event_unobserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace))
            tmux_state.ensure_state_dirs(paths)
            status = tmux_state.build_status(
                kind="job",
                item_id="job-one",
                attempt=1,
                name=None,
                status="failed",
                pane_id="%1",
                command_preview_text="test",
                cwd=str(workspace),
                status_file=tmux_state.status_path(paths, "job-one"),
                log_file=tmux_state.log_path(paths, "job-one"),
                ended_at="2026-06-04T00:00:00Z",
            )
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            record = tmux_bridge.build_bridge_record(
                bridge_id="bridge-test",
                thread_id="thr_test",
                endpoint="unix:///tmp/app.sock",
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
                poll_seconds=2.0,
                quiet_seconds=0.1,
            )

            with mock.patch.object(
                tmux_bridge,
                "deliver_bridge_candidate",
                side_effect=codex_app_server_client.RetryableAppServerError("socket timeout"),
            ):
                updated = tmux_bridge.bridge_daemon_cycle(record)

            self.assertEqual(updated["status"], "active")
            self.assertEqual(updated["observed_event_ids"], [])
            self.assertEqual(updated["pending_delivery"]["event_id"], status["event_id"])
            self.assertEqual(updated["pending_delivery"]["failure_class"], "retryable_failure")

    def test_cancel_marks_cancelled_before_signal_and_skips_foreign_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            result = tmux_bridge.register_bridge(
                thread_id="thr_test",
                endpoint="unix:///tmp/app.sock",
                workspace=str(workspace),
                bridge_id="bridge-test",
            )
            paths = tmux_state.state_paths(str(workspace))
            record = result["record"]
            record["pid"] = 999999
            record["status"] = "active"
            tmux_state.atomic_write_json(tmux_bridge.bridge_record_path(paths, "bridge-test"), record)

            with mock.patch.object(tmux_bridge, "process_command_line", return_value="sleep 1000"):
                with mock.patch.object(tmux_bridge.os, "kill") as kill:
                    cancelled = tmux_bridge.cancel_bridge(bridge_id="bridge-test", workspace=str(workspace))

            kill.assert_not_called()
            self.assertTrue(cancelled["cancelled"])
            stored, error = tmux_state.read_json(tmux_bridge.bridge_record_path(paths, "bridge-test"))
            self.assertIsNone(error)
            assert stored is not None
            self.assertEqual(stored["status"], "cancelled")

    def test_bridge_pid_match_requires_exact_bridge_id_and_workspace_tokens(self) -> None:
        record = {
            "bridge_id": "bridge-a",
            "workspace": "/tmp/workspace",
        }
        matching = "python /repo/scripts/tmux_bridge.py daemon --bridge-id bridge-a --workspace /tmp/workspace"
        prefix_only = "python /repo/scripts/tmux_bridge.py daemon --bridge-id bridge-a-other --workspace /tmp/workspace"
        wrong_workspace = "python /repo/scripts/tmux_bridge.py daemon --bridge-id bridge-a --workspace /tmp/workspace-other"

        self.assertTrue(tmux_bridge.command_matches_bridge(record, matching))
        self.assertFalse(tmux_bridge.command_matches_bridge(record, prefix_only))
        self.assertFalse(tmux_bridge.command_matches_bridge(record, wrong_workspace))


if __name__ == "__main__":
    unittest.main()
