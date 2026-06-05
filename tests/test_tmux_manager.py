from __future__ import annotations

import json
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

    def mark_bridge_verified(self, paths: dict[str, Path], record: dict[str, object]) -> dict[str, object]:
        record["bridge_verification"] = tmux_manager.bridge_notify_identity(record) | {
            "event_id": "preflight-one",
            "mode": "bridge",
            "status": "verified",
            "prompt_sha256": "preflight-sha",
            "submitted_to_app_server": True,
            "acknowledged_by_codex": True,
            "submitted_at": "now",
            "acknowledged_at": "now",
            "ack_turn_id": "turn-main",
            "expires_at": None,
        }
        return tmux_manager.write_manager_record(paths, record)

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
            self.assertEqual(loaded["manager_process_mode"], "foreground")
            self.assertEqual(loaded["manager_launcher"], "foreground-codex-command")

    def test_manager_ps_poc_writes_unsupported_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()

            result = tmux_manager.manager_ps_poc(str(workspace))

            self.assertFalse(result["supported"])
            self.assertEqual(result["status"], tmux_manager.MANAGER_PS_POC_STATUS_UNSUPPORTED)
            proof_path = Path(result["proof_path"])
            manual_path = Path(result["manual_note_path"])
            self.assertTrue(proof_path.exists())
            self.assertTrue(manual_path.exists())
            self.assertEqual(proof_path.parent, (workspace / ".codex" / "tmux-skills" / "proofs").resolve())
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertEqual(proof["status"], tmux_manager.MANAGER_PS_POC_STATUS_UNSUPPORTED)
            self.assertIn("codex_ps_visibility", {item["name"] for item in proof["checks"]})
            self.assertIn("operator_confirmation: pending", manual_path.read_text(encoding="utf-8"))

    def test_idle_manager_record_waits_for_run_next(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))

            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%3",
                worker_pane_id="%2",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
            )
            updated = tmux_manager.manager_cycle(record, paths=paths)

            self.assertEqual(updated["status"], "idle")
            self.assertIsNone(updated["current_job_id"])
            self.assertIsNone(updated["pending_job"])

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
            self.assertEqual(updated["last_terminal_event_id"], updated["last_notification"]["event_id"])
            self.assertEqual(updated["last_notification"]["mode"], "none")
            self.assertEqual(updated["last_notification"]["status"], "dashboard_only")
            self.assertFalse(updated["last_notification"]["submitted_to_app_server"])
            self.assertEqual(updated["submitted_event_ids"], [])

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
            self.assertEqual(first["last_notification"]["status"], "awaiting_ack")
            self.assertTrue(first["last_notification"]["submitted_to_app_server"])
            self.assertFalse(first["last_notification"]["acknowledged_by_codex"])
            self.assertEqual(first["submitted_event_ids"], [status["event_id"]])
            prompt = deliver.call_args.args[2]
            self.assertIn(f"Workspace: {paths['workspace']}", prompt)
            self.assertIn(f"Manager path: {paths['managers'] / 'manager-one.json'}", prompt)
            self.assertIn(f"Status path: {tmux_state.status_path(paths, 'job-one')}", prompt)
            self.assertIn(f"Log path: {tmux_state.log_path(paths, 'job-one')}", prompt)
            for forbidden in ("SECRET OUTPUT", "last_output", "traceback", "retry", "command was", "Job ID", "Please inspect"):
                self.assertNotIn(forbidden, prompt)
            self.assertEqual(second["status"], "waiting_for_codex")

    def test_bridge_submission_failure_retries_until_submitted(self) -> None:
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
                side_effect=[
                    RuntimeError("connection refused"),
                    {"prompt_sha256": "abc123", "event_id": status["event_id"]},
                ],
            ) as deliver:
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                self.assertEqual(first["status"], "waiting_for_codex")
                self.assertEqual(first["last_notification"]["status"], "submission_failed")
                self.assertFalse(first["last_notification"]["submitted_to_app_server"])
                self.assertEqual(first.get("submitted_event_ids"), [])
                second = tmux_manager.manager_cycle(first, paths=paths)
                third = tmux_manager.manager_cycle(second, paths=paths)

            self.assertEqual(deliver.call_count, 2)
            self.assertEqual(second["last_notification"]["status"], "awaiting_ack")
            self.assertTrue(second["last_notification"]["submitted_to_app_server"])
            self.assertEqual(second["submitted_event_ids"], [status["event_id"]])
            self.assertEqual(third["submitted_event_ids"], [status["event_id"]])

    def test_bridge_check_submits_path_only_preflight_and_times_out_without_ack(self) -> None:
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
            tmux_manager.write_manager_record(paths, record)

            with mock.patch.object(
                tmux_manager.tmux_bridge,
                "deliver_bridge_candidate",
                return_value={"prompt_sha256": "preflight-sha", "event_id": "ignored"},
            ) as deliver:
                result = tmux_manager.bridge_check_manager(
                    manager_id="manager-one",
                    workspace=str(workspace),
                    ack_timeout_seconds=0,
                )

            self.assertFalse(result["verified"])
            self.assertTrue(result["submitted_to_app_server"])
            self.assertFalse(result["acknowledged_by_codex"])
            self.assertEqual(result["reason"], "bridge receipt ack timed out")
            verification = result["record"]["bridge_verification"]
            self.assertEqual(verification["status"], "awaiting_ack")
            self.assertEqual(verification["event_id"], result["event_id"])
            prompt = deliver.call_args.args[2]
            self.assertIn(f"Workspace: {paths['workspace']}", prompt)
            self.assertIn(f"Manager path: {paths['managers'] / 'manager-one.json'}", prompt)
            self.assertIn("Status path: none", prompt)
            self.assertIn("Task path: none", prompt)
            self.assertIn("Log path: none", prompt)
            for forbidden in ("event_id", "preflight", "retry", "Please inspect", "Job ID", "last_output"):
                self.assertNotIn(forbidden, prompt)

    def test_ack_marks_bridge_preflight_verified(self) -> None:
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
            event_id = "preflight-one"
            record["bridge_verification"] = tmux_manager.bridge_notify_identity(record) | {
                "event_id": event_id,
                "mode": "bridge",
                "status": "awaiting_ack",
                "prompt_sha256": "preflight-sha",
                "submitted_to_app_server": True,
                "acknowledged_by_codex": False,
                "submitted_at": "now",
                "acknowledged_at": None,
                "expires_at": None,
            }
            record = tmux_manager.upsert_notification(
                record,
                event_id,
                {
                    "event_id": event_id,
                    "mode": "bridge",
                    "source": "manager_bridge_check",
                    "status": "awaiting_ack",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": False,
                    "prompt_sha256": "preflight-sha",
                },
            )
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.ack_manager_event(
                manager_id="manager-one",
                event_id=event_id,
                workspace=str(workspace),
                turn_id="turn-main",
                note="received",
            )

            self.assertTrue(result["acked"])
            verification = result["record"]["bridge_verification"]
            self.assertEqual(verification["status"], "verified")
            self.assertTrue(verification["acknowledged_by_codex"])
            self.assertEqual(verification["ack_turn_id"], "turn-main")
            self.assertEqual(result["record"]["last_notification"]["source"], "manager_bridge_check")

    def test_bridge_verification_is_invalidated_when_endpoint_changes(self) -> None:
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
            record = self.mark_bridge_verified(paths, record)
            record["notify"] = {
                "mode": "bridge",
                "thread_id": "thr-test",
                "endpoint": "unix:///tmp/other.sock",
                "socket_path": "/tmp/other.sock",
            }
            normalized = tmux_manager.normalize_manager_record(record, paths)

            self.assertEqual(normalized["bridge_verification"]["status"], "mismatched_config")
            verified, reason = tmux_manager.bridge_receipt_verified(normalized)
            self.assertFalse(verified)
            self.assertIn("endpoint", reason or "")

    def test_run_next_blocks_until_bridge_preflight_verified_without_writing_command(self) -> None:
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
            record["status"] = "idle"
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertFalse(result["queued"])
            self.assertIn("bridge receipt is not verified", result["reason"])
            self.assertFalse(tmux_manager.manager_command_request_path(paths, "manager-one", "job-two").exists())

    def test_run_next_queues_after_bridge_preflight_verified(self) -> None:
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
            record["status"] = "idle"
            self.mark_bridge_verified(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertTrue(result["queued"])
            self.assertEqual(Path(result["command_request_path"]).read_text(encoding="utf-8"), "echo next")

    def test_run_next_blocks_terminal_event_until_acknowledged(self) -> None:
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
            record["status"] = "waiting_for_codex"
            record["last_terminal_event_id"] = "evt-one"
            record = self.mark_bridge_verified(paths, record)
            record = tmux_manager.upsert_notification(
                record,
                "evt-one",
                {
                    "event_id": "evt-one",
                    "mode": "bridge",
                    "status": "awaiting_ack",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": False,
                },
            )
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertFalse(result["queued"])
            self.assertIn("last terminal event has not been acknowledged", result["reason"])
            self.assertFalse(tmux_manager.manager_command_request_path(paths, "manager-one", "job-two").exists())

    def test_external_ack_fields_survive_dashboard_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            stale = self.build_record(
                paths,
                {
                    "mode": "bridge",
                    "thread_id": "thr-test",
                    "endpoint": "unix:///tmp/codex.sock",
                    "socket_path": "/tmp/codex.sock",
                },
            )
            stale["pending_job"] = None
            stale["bridge_verification"] = tmux_manager.bridge_notify_identity(stale) | {
                "event_id": "preflight-one",
                "mode": "bridge",
                "status": "awaiting_ack",
                "prompt_sha256": "preflight-sha",
                "submitted_to_app_server": True,
                "acknowledged_by_codex": False,
                "expires_at": None,
            }
            latest = dict(stale)
            latest["bridge_verification"] = dict(stale["bridge_verification"]) | {
                "status": "verified",
                "acknowledged_by_codex": True,
                "acknowledged_at": "now",
                "ack_turn_id": "turn-main",
            }
            latest = tmux_manager.upsert_notification(
                latest,
                "preflight-one",
                {
                    "event_id": "preflight-one",
                    "mode": "bridge",
                    "source": "manager_bridge_check",
                    "status": "acknowledged",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": True,
                    "ack_turn_id": "turn-main",
                },
            )
            latest["last_ack"] = {"event_id": "preflight-one", "turn_id": "turn-main"}

            merged = tmux_manager.merge_external_manager_update(stale, latest)

            self.assertEqual(merged["bridge_verification"]["status"], "verified")
            self.assertTrue(merged["bridge_verification"]["acknowledged_by_codex"])
            self.assertEqual(merged["last_ack"]["event_id"], "preflight-one")

    def test_external_preflight_ack_merge_preserves_newer_terminal_last_notification(self) -> None:
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
            record = self.mark_bridge_verified(paths, record)
            latest = dict(record)
            terminal = tmux_manager.upsert_notification(
                dict(record),
                "evt-terminal",
                {
                    "event_id": "evt-terminal",
                    "mode": "bridge",
                    "source": "manager_terminal",
                    "job_id": "job-one",
                    "status": "awaiting_ack",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": False,
                },
            )
            terminal["last_terminal_event_id"] = "evt-terminal"

            merged = tmux_manager.merge_external_manager_update(terminal, latest)

            self.assertEqual(merged["last_notification"]["event_id"], "evt-terminal")
            self.assertEqual(merged["last_notification"]["status"], "awaiting_ack")
            self.assertFalse(merged["last_notification"]["acknowledged_by_codex"])

    def test_ack_marks_notification_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            status = self.build_terminal_status(paths)
            event_id = str(status["event_id"])
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["status"] = "waiting_for_codex"
            record["last_terminal_event_id"] = event_id
            record = tmux_manager.upsert_notification(
                record,
                event_id,
                {
                    "event_id": event_id,
                    "mode": "bridge",
                    "status": "awaiting_ack",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": False,
                },
            )
            record["submitted_event_ids"] = [event_id]
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.ack_manager_event(
                manager_id="manager-one",
                event_id=event_id,
                workspace=str(workspace),
                turn_id="turn-main",
                note="received",
            )

            self.assertTrue(result["acked"])
            notification = result["record"]["last_notification"]
            self.assertEqual(notification["status"], "acknowledged")
            self.assertTrue(notification["acknowledged_by_codex"])
            self.assertEqual(notification["ack_turn_id"], "turn-main")
            self.assertEqual(result["record"]["last_ack"]["event_id"], event_id)

    def test_run_next_refuses_cancelled_manager_without_writing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "cancel_requested"
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-after-cancel",
                command_text="echo should-not-run",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertFalse(result["queued"])
            self.assertIn("cancel", result["reason"])
            self.assertFalse(
                tmux_manager.manager_command_request_path(paths, "manager-one", "job-after-cancel").exists()
            )

    def test_bridge_check_delivery_update_does_not_overwrite_cancel_requested(self) -> None:
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
            tmux_manager.write_manager_record(paths, record)

            def cancel_then_deliver(*args: object) -> dict[str, object]:
                latest, _error = tmux_manager.read_manager_record(paths, "manager-one")
                assert latest is not None
                latest["status"] = "cancel_requested"
                latest["cancel_requested_at"] = "cancel-now"
                latest["pending_job"] = None
                tmux_manager.write_manager_record(paths, latest)
                return {"prompt_sha256": "preflight-sha", "event_id": "ignored"}

            with mock.patch.object(tmux_manager.tmux_bridge, "deliver_bridge_candidate", side_effect=cancel_then_deliver):
                result = tmux_manager.bridge_check_manager(
                    manager_id="manager-one",
                    workspace=str(workspace),
                    ack_timeout_seconds=0,
                )

            self.assertFalse(result["verified"])
            self.assertEqual(result["record"]["status"], "cancel_requested")
            self.assertEqual(result["record"]["cancel_requested_at"], "cancel-now")

    def test_manager_cycle_trims_current_job_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["job_ids"] = ["job-one"]
            record["status"] = "running"
            record["log_max_bytes"] = 10
            log_path = tmux_state.log_path(paths, "job-one")
            log_path.write_bytes(b"0123456789abcdef")
            record["jobs"] = {"job-one": {"job_id": "job-one", "log_path": str(log_path)}}

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                updated = tmux_manager.manager_cycle(record, paths=paths)

            self.assertEqual(log_path.read_bytes(), b"6789abcdef")
            self.assertEqual(updated["last_log_trim"]["job_id"], "job-one")
            self.assertEqual(updated["last_log_trim"]["size_after"], 10)

    def test_manager_cycle_keeps_cancelled_record_cancelled_after_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            status = self.build_terminal_status(paths)
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["job_ids"] = ["job-one"]
            record["status"] = "cancelled"

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                updated = tmux_manager.manager_cycle(record, paths=paths)

            self.assertEqual(updated["status"], "cancelled")
            self.assertIsNone(updated["last_terminal_event_id"])

    def test_cleanup_manager_removes_manager_and_job_evidence_only_in_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["job_ids"] = ["job-one"]
            record["status"] = "cancelled"
            status_path = tmux_state.status_path(paths, "job-one")
            log_path = tmux_state.log_path(paths, "job-one")
            command_path = tmux_state.command_path(paths, "job-one")
            tmux_state.write_status(status_path, self.build_terminal_status(paths))
            log_path.write_text("job log\n", encoding="utf-8")
            command_path.write_text("echo ok\n", encoding="utf-8")
            outside_path = Path(tmp_name) / "outside.log"
            outside_path.write_text("keep\n", encoding="utf-8")
            record["jobs"] = {
                "job-one": {
                    "job_id": "job-one",
                    "status_path": str(status_path),
                    "log_path": str(outside_path),
                    "run_result": {"command_path": str(command_path), "status_path": str(status_path)},
                }
            }
            tmux_manager.write_manager_record(paths, record)
            dashboard_path = tmux_manager.manager_dashboard_path(paths, "manager-one")
            dashboard_path.write_text("dashboard\n", encoding="utf-8")

            result = tmux_manager.cleanup_manager("manager-one", workspace=str(workspace), include_jobs=True)

            self.assertFalse(result["cleaned"])
            self.assertFalse(tmux_manager.manager_record_path(paths, "manager-one").exists())
            self.assertFalse(dashboard_path.exists())
            self.assertFalse(status_path.exists())
            self.assertFalse(command_path.exists())
            self.assertTrue(outside_path.exists())
            self.assertEqual(result["skipped"][0]["path"], str(outside_path))

    def test_run_next_queues_command_for_existing_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record["last_terminal_event_id"] = "evt-one"
            record = tmux_manager.upsert_notification(
                record,
                "evt-one",
                {
                    "event_id": "evt-one",
                    "mode": "bridge",
                    "status": "acknowledged",
                    "submitted_to_app_server": True,
                    "acknowledged_by_codex": True,
                },
            )
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
            self.assertEqual(loaded["last_notification"]["status"], "handled")
            self.assertEqual(loaded["last_notification"]["handled_by_job_id"], "job-two")

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

    def test_write_manager_record_preserves_existing_cancel_over_stale_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            stale = self.build_record(paths)
            stale["pending_job"] = None
            stale["status"] = "running"
            current = dict(stale)
            current["status"] = "cancel_requested"
            current["cancel_requested_at"] = "cancel-now"
            current["pending_job"] = None
            tmux_manager.write_manager_record(paths, current)

            stale["status"] = "waiting_for_codex"
            written = tmux_manager.write_manager_record(paths, stale)

            self.assertEqual(written["status"], "cancel_requested")
            self.assertEqual(written["cancel_requested_at"], "cancel-now")

    def test_external_cancel_update_wins_over_ack_merge(self) -> None:
        stale = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "pending_job": None,
            "heartbeat_at": "old-heartbeat",
            "last_notification": {
                "event_id": "evt-one",
                "status": "awaiting_ack",
                "acknowledged_by_codex": False,
            },
            "notifications": [
                {
                    "event_id": "evt-one",
                    "status": "awaiting_ack",
                    "acknowledged_by_codex": False,
                }
            ],
        }
        latest = {
            "manager_id": "manager-one",
            "status": "cancel_requested",
            "pending_job": None,
            "heartbeat_at": "new-heartbeat",
            "cancel_requested_at": "cancel-now",
            "last_ack": {"event_id": "evt-one", "turn_id": "turn-main"},
            "notifications": [
                {
                    "event_id": "evt-one",
                    "status": "acknowledged",
                    "acknowledged_by_codex": True,
                    "ack_turn_id": "turn-main",
                }
            ],
        }

        merged = tmux_manager.merge_external_manager_update(stale, latest)

        self.assertEqual(merged["status"], "cancel_requested")
        self.assertEqual(merged["cancel_requested_at"], "cancel-now")
        self.assertEqual(merged["last_ack"]["event_id"], "evt-one")
        self.assertEqual(merged["last_notification"]["status"], "acknowledged")

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

    def test_stop_worker_cancel_updates_existing_cancel_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["status"] = "cancelled"
            record["stop_worker_requested"] = False
            record["worker_stop_result"] = None
            tmux_manager.write_manager_record(paths, record)

            with mock.patch.object(
                tmux_manager,
                "send_worker_interrupt",
                return_value={"sent": True, "returncode": 0, "stderr": ""},
            ):
                result = tmux_manager.cancel_manager("manager-one", workspace=str(workspace), stop_worker=True)

            self.assertTrue(result["cancelled"])
            self.assertTrue(result["record"]["stop_worker_requested"])
            self.assertEqual(result["record"]["worker_stop_result"]["sent"], True)

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

    def test_dashboard_text_exposes_bridge_submission_and_ack_ids(self) -> None:
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
                "status": "acknowledged",
                "submitted_to_app_server": True,
                "acknowledged_by_codex": True,
                "delivery": {"response_id": "resp-one", "turn_id": "turn-one"},
                "ack_turn_id": "turn-main",
            },
            "last_ack": {"event_id": "evt-one", "acknowledged_at": "now", "turn_id": "turn-main"},
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("last_notification: bridge status=acknowledged submitted_to_app_server=True acknowledged_by_codex=True", text)
        self.assertIn("last_submission_response_id: resp-one", text)
        self.assertIn("last_submission_turn_id: turn-one", text)
        self.assertIn("last_ack_turn_id: turn-main", text)
        self.assertIn("last_ack_event_id: evt-one", text)

    def test_dashboard_text_exposes_bridge_submission_error(self) -> None:
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
                "status": "submission_failed",
                "submitted_to_app_server": False,
                "acknowledged_by_codex": False,
                "error": "connection refused",
            },
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("last_notification: bridge status=submission_failed submitted_to_app_server=False acknowledged_by_codex=False", text)
        self.assertIn("last_notification_error: connection refused", text)


if __name__ == "__main__":
    unittest.main()
