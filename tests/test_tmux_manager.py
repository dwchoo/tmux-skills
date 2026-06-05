from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import tmux_manager  # noqa: E402
import tmux_state  # noqa: E402


class TmuxManagerTests(unittest.TestCase):
    def build_record(self, paths: dict[str, Path], notify: dict[str, object] | None = None) -> dict[str, object]:
        request_path = tmux_manager.write_command_request(paths, "manager-one", "job-one", "echo ok")
        record = tmux_manager.build_manager_record(
            manager_id="manager-one",
            manager_pane_id="%3",
            worker_pane_id="%2",
            pending_job=tmux_manager.build_pending_job("job-one", request_path, str(paths["workspace"])),
            notify=notify or {"mode": "none"},
            workspace=str(paths["workspace"]),
            state_dir=str(paths["root"]),
        )
        return tmux_manager.write_manager_record(paths, record)

    def build_terminal_status(self, paths: dict[str, Path], job_id: str = "job-one") -> dict[str, object]:
        return tmux_state.build_status(
            kind="job",
            item_id=job_id,
            attempt=1,
            name=None,
            status="succeeded",
            pane_id="%2",
            command_preview_text="echo ok",
            cwd=str(paths["workspace"]),
            status_file=tmux_state.status_path(paths, job_id),
            log_file=tmux_state.log_path(paths, job_id),
            exit_code=0,
            last_output="SECRET OUTPUT SHOULD NOT APPEAR",
        )

    def test_manager_state_read_write_has_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))

            record = self.build_record(paths)
            loaded, error = tmux_manager.read_manager_record(paths, "manager-one")

            self.assertIsNone(error)
            assert loaded is not None
            for key in (
                "manager_id",
                "status",
                "manager_pane_id",
                "worker_pane_id",
                "current_job_id",
                "job_ids",
                "notify",
                "heartbeat_at",
                "last_terminal_event_id",
                "workspace",
                "state_dir",
            ):
                self.assertIn(key, loaded)
            self.assertEqual(record["manager_path"], str(paths["managers"] / "manager-one.json"))

    def test_terminal_transition_waits_for_codex_and_does_not_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["job_ids"] = ["job-one"]
            record["status"] = "running"
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), self.build_terminal_status(paths))

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                updated = tmux_manager.manager_cycle(record, paths=paths)

            self.assertEqual(updated["status"], "waiting_for_codex")
            self.assertEqual(updated["last_terminal_event_id"], updated["notified_event_ids"][0])
            self.assertEqual(updated["last_notification"]["mode"], "none")

    def test_bridge_prompt_is_path_only_and_sent_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(
                paths,
                {
                    "mode": "bridge",
                    "thread_id": "thr-test",
                    "endpoint": "unix:///tmp/codex.sock",
                    "socket_path": "/tmp/codex.sock",
                },
            )
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            status = self.build_terminal_status(paths)

            with mock.patch.object(
                tmux_manager.tmux_bridge,
                "deliver_bridge_candidate",
                return_value={"prompt_sha256": "abc123", "event_id": status["event_id"]},
            ) as deliver:
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                second = tmux_manager.transition_terminal(first, paths=paths, status=status)

            self.assertEqual(deliver.call_count, 1)
            prompt = deliver.call_args.args[2]
            self.assertIn(f"Workspace: {paths['workspace']}", prompt)
            self.assertIn(f"Manager path: {paths['managers'] / 'manager-one.json'}", prompt)
            self.assertIn(f"Status path: {tmux_state.status_path(paths, 'job-one')}", prompt)
            self.assertIn(f"Log path: {tmux_state.log_path(paths, 'job-one')}", prompt)
            for forbidden in ("SECRET OUTPUT", "last_output", "traceback", "retry", "command was"):
                self.assertNotIn(forbidden, prompt)
            self.assertEqual(second["status"], "waiting_for_codex")

    def test_run_next_queues_command_for_existing_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertTrue(result["queued"])
            request_path = Path(result["command_request_path"])
            self.assertEqual(request_path.read_text(encoding="utf-8"), "echo next")
            loaded, _error = tmux_manager.read_manager_record(paths, "manager-one")
            assert loaded is not None
            self.assertEqual(loaded["pending_job"]["job_id"], "job-two")

    def test_external_cancel_update_wins_over_stale_dashboard_record(self) -> None:
        stale = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "pending_job": None,
            "heartbeat_at": "old-heartbeat",
        }
        latest = {
            "manager_id": "manager-one",
            "status": "cancel_requested",
            "pending_job": None,
            "heartbeat_at": "new-heartbeat",
        }

        merged = tmux_manager.merge_external_manager_update(stale, latest)

        self.assertEqual(merged["status"], "cancel_requested")
        self.assertEqual(merged["heartbeat_at"], "old-heartbeat")

    def test_external_pending_job_update_wins_over_stale_waiting_record(self) -> None:
        stale = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "pending_job": None,
            "heartbeat_at": "old-heartbeat",
        }
        latest = {
            "manager_id": "manager-one",
            "status": "queued",
            "pending_job": {"job_id": "job-two"},
            "heartbeat_at": "new-heartbeat",
        }

        merged = tmux_manager.merge_external_manager_update(stale, latest)

        self.assertEqual(merged["pending_job"]["job_id"], "job-two")
        self.assertEqual(merged["heartbeat_at"], "old-heartbeat")

    def test_cancel_does_not_stop_worker_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["status"] = "running"
            tmux_manager.write_manager_record(paths, record)

            with mock.patch.object(tmux_manager, "send_worker_interrupt") as interrupt:
                result = tmux_manager.cancel_manager("manager-one", workspace=str(workspace))

            self.assertTrue(result["cancelled"])
            interrupt.assert_not_called()
            self.assertEqual(result["record"]["status"], "cancel_requested")
            self.assertFalse(result["record"]["stop_worker_requested"])

    def test_render_dashboard_to_pane_uses_one_shot_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            dashboard_file = Path(tmp_name) / "manager.dashboard.txt"
            dashboard_file.write_text("manager status\n", encoding="utf-8")

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                with mock.patch.object(tmux_manager, "tmux_command_prefix", return_value=["tmux"]):
                    with mock.patch.object(
                        tmux_manager.subprocess,
                        "run",
                        return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                    ) as run:
                        result = tmux_manager.render_dashboard_to_pane("%2", dashboard_file)

            self.assertTrue(result["rendered"])
            argv = run.call_args.args[0]
            command = argv[-2]
            self.assertEqual(argv[:4], ["tmux", "send-keys", "-t", "%2"])
            self.assertEqual(argv[-1], "Enter")
            self.assertIn(str(dashboard_file), command)
            self.assertIn("cat", command)
            self.assertNotIn("while", command)
            self.assertNotIn("sleep", command)

    def test_dashboard_text_exposes_bridge_delivery_ids(self) -> None:
        record = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "manager_pane_id": "%3",
            "worker_pane_id": "%2",
            "current_job_id": "job-one",
            "heartbeat_at": "now",
            "manager_path": "/tmp/manager.json",
            "last_terminal_event_id": "evt-one",
            "last_notification": {
                "mode": "bridge",
                "delivered": True,
                "delivery": {"response_id": "resp-one", "turn_id": "turn-one"},
            },
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("last_notification: bridge delivered=True", text)
        self.assertIn("last_notification_response_id: resp-one", text)
        self.assertIn("last_notification_turn_id: turn-one", text)

    def test_dashboard_text_exposes_bridge_delivery_error(self) -> None:
        record = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "manager_pane_id": "%3",
            "worker_pane_id": "%2",
            "current_job_id": "job-one",
            "heartbeat_at": "now",
            "manager_path": "/tmp/manager.json",
            "last_terminal_event_id": "evt-one",
            "last_notification": {
                "mode": "bridge",
                "delivered": False,
                "error": "connection refused",
            },
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("last_notification: bridge delivered=False", text)
        self.assertIn("last_notification_error: connection refused", text)


if __name__ == "__main__":
    unittest.main()
