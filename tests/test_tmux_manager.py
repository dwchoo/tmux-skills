from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import tmux_manager  # noqa: E402
import tmux_manager_viewer  # noqa: E402
import tmux_state  # noqa: E402


class TmuxManagerTests(unittest.TestCase):
    def tmux_inject_prompt(self, event_id: str = "event-one", manager_id: str = "manager-one") -> str:
        return tmux_manager.build_tmux_inject_wake_prompt(
            {"manager_id": manager_id},
            {"event_id": event_id, "wake_id": tmux_manager.tmux_inject_wake_id(event_id)},
        )

    def build_record(self, paths: dict[str, Path], notify: dict[str, object] | None = None) -> dict[str, object]:
        request_path = tmux_manager.write_command_request(paths, "manager-one", "job-one", "echo ok")
        record = tmux_manager.build_manager_record(
            manager_id="manager-one",
            manager_pane_id="%3",
            worker_pane_id="%2",
            manager_pane_index="0",
            worker_pane_index="1",
            pending_job=tmux_manager.build_pending_job("job-one", request_path, str(paths["workspace"]), "%2", "1"),
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

    def build_tmux_inject_record(self, paths: dict[str, Path]) -> dict[str, object]:
        record = self.build_record(paths, {"mode": "tmux-inject", "codex_pane_id": "%9"})
        record["notify"] = {"mode": "tmux-inject", "codex_pane_id": "%9"}
        record["codex_pane_id"] = "%9"
        record["pending_job"] = None
        record["current_job_id"] = "job-one"
        return record

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
                "worker_pane_ids",
                "current_job_id",
                "active_job_ids",
                "job_ids",
                "jobs",
                "events",
                "notify",
                "heartbeat_at",
                "last_terminal_event_id",
                "workspace",
                "state_dir",
                "dashboard_renderer",
                "dashboard_viewer_pid",
                "dashboard_viewer_state_path",
                "dashboard_viewer_heartbeat_at",
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

    def test_manager_ps_poc_verifies_live_background_manager_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%3",
                worker_pane_id="%2",
                manager_pane_index="0",
                worker_pane_index="1",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
                process_mode="background",
            )
            record["heartbeat_at"] = tmux_state.utc_now()
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.manager_ps_poc(str(workspace))

            self.assertTrue(result["supported"])
            self.assertEqual(result["status"], tmux_manager.MANAGER_PS_POC_STATUS_VERIFIED)
            self.assertEqual(result["background_managers"][0]["manager_launcher"], "codex-background-terminal")
            proof_path = Path(result["proof_path"])
            manual_path = Path(result["manual_note_path"])
            self.assertTrue(proof_path.exists())
            self.assertIn("operator_confirmation: recorded", manual_path.read_text(encoding="utf-8"))

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

    def test_tmux_inject_prompt_is_short_wake_only(self) -> None:
        record = {"manager_id": "manager-one", "workspace": "/tmp/workspace"}
        candidate = {
            "event_id": "event-one",
            "job_id": "job-one",
            "status_path": "/tmp/workspace/.codex/tmux-skills/status/job-one.json",
            "log_path": "/tmp/workspace/.codex/tmux-skills/logs/job-one.log",
        }

        prompt = tmux_manager.build_tmux_inject_wake_prompt(record, candidate)

        self.assertEqual(
            prompt,
            "\n".join(
                [
                    "ID:cbf7f0;",
                    "tmux-skills event ready. Use $tmux-control only.",
                    "",
                    "Manager ID: manager-one",
                    "Event ID: event-one",
                    "",
                    "Inspect manager status once. Handle only the latest unacked event.",
                    "If stale or already handled, ack/report only.",
                    "After run-next, wait for the next manager event; do not poll or monitor directly.",
                ]
            ),
        )
        self.assertEqual(tmux_manager.tmux_inject_prompt_wake_id(prompt), "cbf7f0")
        self.assertEqual(tmux_manager.tmux_inject_prompt_wake_id("ID:cbf7f0;tmux-skills event ready"), "cbf7f0")
        self.assertEqual(tmux_manager.tmux_inject_prompt_wake_id("ID:cbf7f0tmux-skills event ready"), "cbf7f0")
        self.assertIn("$tmux-control", prompt)
        for forbidden in (
            "Ack command",
            "python",
            "manager ack",
            "/tmp/workspace",
            "Status path",
            "Log path",
            "retry",
            "Codex queue",
        ):
            self.assertNotIn(forbidden, prompt)

    def test_tmux_inject_validates_pane_uses_sdk_and_injects_once_before_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(
                    tmux_manager,
                    "pane_codex_validation",
                    return_value={"safe": True, "status": "live_codex", "reason": "ok"},
                ) as validate,
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 0.9, "reason": "ok"},
                ) as planner,
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ) as inject,
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "decision": {"action": "confirmed", "source": "heuristic"},
                        "prompt_still_staged": False,
                    },
                ) as delivery,
            ):
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                second = tmux_manager.transition_terminal(first, paths=paths, status=status)

            self.assertEqual(validate.call_count, 1)
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(inject.call_count, 1)
            self.assertEqual(delivery.call_count, 1)
            self.assertEqual(inject.call_args.args[0], "%9")
            self.assertIn("Manager ID: manager-one", inject.call_args.args[1])
            self.assertTrue(inject.call_args.args[1].startswith(f"ID:{tmux_manager.tmux_inject_wake_id(str(status['event_id']))};"))
            self.assertEqual(first["last_notification"]["mode"], "tmux-inject")
            self.assertEqual(first["last_notification"]["status"], "awaiting_receipt")
            self.assertEqual(first["last_notification"]["wake_id"], tmux_manager.tmux_inject_wake_id(str(status["event_id"])))
            self.assertEqual(first["events"][status["event_id"]]["wake_id"], tmux_manager.tmux_inject_wake_id(str(status["event_id"])))
            self.assertEqual(first["last_notification"]["delivery_check"]["decision"]["action"], "confirmed")
            self.assertIn("total", first["last_notification"]["timing"])
            self.assertIn("terminal_assessment", first["last_notification"]["timing"])
            self.assertIn("pane_validation", first["last_notification"]["timing"])
            self.assertIn("inject_decision", first["last_notification"]["timing"])
            self.assertIn("prompt_injection", first["last_notification"]["timing"])
            self.assertIn("delivery_check", first["last_notification"]["timing"])
            self.assertTrue(first["last_notification"]["submitted_to_tmux"])
            self.assertFalse(first["last_notification"]["submitted_to_app_server"])
            self.assertEqual(first["submitted_event_ids"], [status["event_id"]])
            self.assertEqual(second["submitted_event_ids"], [status["event_id"]])

    def test_tmux_inject_coalesces_new_event_when_prior_wake_is_unacked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record = tmux_manager.upsert_notification(
                record,
                "evt-one",
                {
                    "event_id": "evt-one",
                    "mode": "tmux-inject",
                    "status": "injected",
                    "submitted_to_tmux": True,
                    "acknowledged_by_codex": False,
                },
            )
            candidate = self.build_terminal_status(paths, job_id="job-two")
            candidate["event_id"] = "evt-two"
            candidate["job_id"] = "job-two"
            candidate["source"] = "manager_terminal"

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation") as validate,
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
            ):
                updated = tmux_manager.notify_terminal_event(record, candidate)

            validate.assert_not_called()
            inject.assert_not_called()
            notification = updated["last_notification"]
            self.assertEqual(notification["event_id"], "evt-two")
            self.assertEqual(notification["status"], "coalesced")
            self.assertEqual(notification["coalesced_by_event_id"], "evt-one")
            self.assertFalse(notification["submitted_to_tmux"])
            self.assertNotIn("evt-two", updated["submitted_event_ids"])
            self.assertEqual(updated["events"]["evt-two"]["notification_status"], "coalesced")

    def test_tmux_inject_refuses_unsafe_pane_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(
                    tmux_manager,
                    "pane_codex_validation",
                    return_value={"safe": False, "status": "no_live_codex_process", "reason": "not Codex"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            inject.assert_not_called()
            self.assertEqual(updated["last_notification"]["mode"], "tmux-inject")
            self.assertEqual(updated["last_notification"]["status"], "inject_refused")
            self.assertEqual(updated["last_notification"]["reason"], "not Codex")
            self.assertFalse(updated["last_notification"]["submitted_to_tmux"])
            self.assertEqual(updated["submitted_event_ids"], [])

    def test_tmux_inject_sdk_defer_records_pending_without_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(
                    tmux_manager,
                    "pane_codex_validation",
                    return_value={"safe": True, "status": "live_codex", "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "defer", "target_pane": "%9", "confidence": 0.0, "reason": "SDK unavailable"},
                ),
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            inject.assert_not_called()
            self.assertEqual(updated["last_notification"]["status"], "inject_pending")
            self.assertEqual(updated["last_notification"]["reason"], "SDK unavailable")
            self.assertEqual(updated["submitted_event_ids"], [])

    def test_tmux_inject_guardrail_overrides_sdk_wrong_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(
                    tmux_manager,
                    "pane_codex_validation",
                    return_value={"safe": True, "status": "live_codex", "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%10", "confidence": 0.9, "reason": "wrong"},
                ),
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            inject.assert_not_called()
            self.assertEqual(updated["last_notification"]["status"], "inject_refused")
            self.assertIn("different from the bound Codex pane", updated["last_notification"]["reason"])
            self.assertEqual(updated["submitted_event_ids"], [])

    def test_tmux_inject_ack_marks_notification_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ),
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={"checked": True, "decision": {"action": "confirmed"}, "prompt_still_staged": False},
                ),
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)
            tmux_manager.write_manager_record(paths, updated)

            ack = tmux_manager.ack_manager_event(
                manager_id="manager-one",
                event_id=str(status["event_id"]),
                workspace=str(workspace),
            )

            self.assertTrue(ack["acked"])
            notification = tmux_manager.notification_for_event(ack["record"], str(status["event_id"]))
            self.assertEqual(notification["mode"], "tmux-inject")
            self.assertEqual(notification["status"], "acknowledged")
            self.assertTrue(notification["acknowledged_by_codex"])
            self.assertEqual(ack["record"]["last_ack"]["event_id"], status["event_id"])

    def test_tmux_inject_ack_marks_event_summary_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_terminal_event_assessment",
                    return_value={"source": "deterministic", "summary": "done", "recommended_action": "wake_codex", "confidence": 1.0, "reason": "test"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ),
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={"checked": True, "decision": {"action": "confirmed"}, "prompt_still_staged": False},
                ),
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)
            tmux_manager.write_manager_record(paths, updated)

            ack = tmux_manager.ack_manager_event(
                manager_id="manager-one",
                event_id=str(status["event_id"]),
                workspace=str(workspace),
                note="received",
            )

            event = ack["record"]["events"][str(status["event_id"])]
            self.assertTrue(event["acknowledged_by_codex"])
            self.assertEqual(event["ack_note"], "received")
            self.assertEqual(event["notification_status"], "acknowledged")

    def test_tmux_inject_pending_submission_records_delivery_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_terminal_event_assessment",
                    return_value={"source": "deterministic", "summary": "done", "recommended_action": "wake_codex", "confidence": 1.0, "reason": "test"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ),
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={"checked": True, "decision": {"action": "submit"}, "prompt_still_staged": True},
                ),
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            self.assertEqual(updated["last_notification"]["status"], "inject_pending")
            self.assertEqual(updated["submitted_event_ids"], [status["event_id"]])
            self.assertEqual(updated["notified_event_ids"], [status["event_id"]])
            self.assertTrue(updated["events"][str(status["event_id"])]["submitted_to_tmux"])

    def test_terminal_event_assessment_uses_codex_sidecar_when_enabled(self) -> None:
        with (
            mock.patch.object(tmux_manager, "codex_sidecar_fast_path_enabled", return_value=False),
            mock.patch.object(
                tmux_manager,
                "codex_sidecar_decision",
                return_value={
                    "source": "codex_sidecar",
                    "summary": "random digit 7 observed",
                    "recommended_action": "wake_codex",
                    "confidence": 0.94,
                    "reason": "terminal output requires Codex inspection",
                    "setup": {"ok": True, "reused": True},
                },
            ),
        ):
            result = tmux_manager.codex_sdk_terminal_event_assessment(
                {"manager_id": "manager-one", "workspace": "/tmp/workspace", "state_dir": "/tmp/state"},
                {"event_id": "evt-one", "job_id": "job-one", "status": "succeeded", "last_output": "RANDOM_DIGIT=7"},
            )

        self.assertEqual(result["source"], "codex_sidecar")
        self.assertEqual(result["recommended_action"], "wake_codex")
        self.assertEqual(result["sidecar_setup"], {"ok": True, "reused": True})

    def test_tmux_inject_followup_staged_prompt_overrides_sidecar_confirmed(self) -> None:
        prompt = (
            "tmux-skills event ready. Use $tmux-control only.\n\n"
            "Manager ID: manager-one\n"
            "Event ID: evt-one\n\n"
            "Inspect the manager state for this workspace, decide the next action, and acknowledge the event after inspection."
        )
        capture_output = f"previous output\n› {prompt}\n\nCtrl+J newline   Enter to submit message\n"

        result = tmux_manager.normalize_tmux_inject_followup_decision(
            {"action": "confirmed", "confidence": 0.99, "reason": "sidecar thought it was sent"},
            prompt=prompt,
            capture_output=capture_output,
        )

        self.assertEqual(result["action"], "submit")
        self.assertIn("deterministic staged prompt override", result["reason"])

    def test_tmux_inject_followup_staged_prompt_skips_sidecar_for_immediate_submit(self) -> None:
        prompt = (
            "tmux-skills event ready. Use $tmux-control only.\n\n"
            "Manager ID: manager-one\n"
            "Event ID: evt-one\n\n"
            "Inspect the manager state for this workspace, decide the next action, and acknowledge the event after inspection."
        )
        capture = {"captured": True, "output": f"Working\n› {prompt}\n\nCtrl+J newline   Enter to submit message\n"}

        with mock.patch.object(tmux_manager, "codex_sidecar_decision") as sidecar:
            result = tmux_manager.codex_sdk_inject_followup_decision(
                {"manager_id": "manager-one", "workspace": "/tmp/workspace", "state_dir": "/tmp/state", "codex_pane_id": "%1"},
                {"event_id": "evt-one"},
                {"safe": True, "status": "live_codex"},
                {"injected": True, "submitted_to_tmux": True},
                capture,
                prompt,
            )

        sidecar.assert_not_called()
        self.assertEqual(result["action"], "submit")
        self.assertEqual(result["source"], "deterministic")
        self.assertEqual(result["sidecar_skipped"], "staged prompt cannot wait for SDK confirmation")

    def test_manager_status_adds_manager_sequence_without_overwriting_job_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            status = self.build_terminal_status(paths)
            status["attempt"] = 1
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            record["pending_job"] = None
            record["current_job_id"] = "job-one"
            record["jobs"] = {"job-one": {"job_id": "job-one", "manager_sequence": 2}}
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.manager_status("manager-one", workspace=str(workspace))

            self.assertEqual(result["current_job_status"]["attempt"], 1)
            self.assertEqual(result["current_job_status"]["manager_sequence"], 2)

    def test_tmux_inject_planner_uses_deterministic_guardrails_without_api_key(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "TMUX_SKILLS_CODEX_SDK_DECISION": ""}, clear=False):
            result = tmux_manager.codex_sdk_inject_decision(
                {"manager_id": "manager-one", "codex_pane_id": "%9"},
                {"event_id": "event-one"},
                {"safe": True},
            )

        self.assertEqual(result["decision"], "inject")
        self.assertEqual(result["target_pane"], "%9")
        self.assertEqual(result["source"], "deterministic_fast_path")
        self.assertNotIn("OPENAI_API_KEY", result["reason"])

    def test_codex_sidecar_defaults_use_gpt55_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            missing_config = Path(tmp_name) / "missing.json"
            with mock.patch.dict(os.environ, {"TMUX_SKILLS_CONFIG": str(missing_config)}, clear=False):
                result = tmux_manager.codex_sidecar_config()

        self.assertEqual(result["model"], "gpt-5.5")
        self.assertEqual(result["reasoning_effort"], "low")

    def test_tmux_inject_planner_can_use_codex_sidecar_with_configured_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            state_dir = Path(tmp_name) / "state"
            config_path = Path(tmp_name) / "tmux-skills.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_sidecar": {
                            "model": "5.3-codex-spark",
                            "reasoning_effort": "medium",
                            "deterministic_fast_path": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            python_path = state_dir / "sidecar-venv" / "bin" / "python"
            calls: list[list[str]] = []
            helper_requests: list[dict[str, object]] = []

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                if command[:2] == ["uv", "venv"]:
                    python_path.parent.mkdir(parents=True, exist_ok=True)
                    python_path.write_text("", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if command[:3] == ["uv", "pip", "install"]:
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                helper_requests.append(json.loads(str(kwargs.get("input") or "{}")))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "source": "codex_sidecar",
                            "output": json.dumps({"decision": "inject", "target_pane": "%9", "confidence": 0.75, "reason": "safe"}),
                        }
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(tmux_manager.subprocess, "run", side_effect=fake_run),
                mock.patch.dict(
                    os.environ,
                    {"TMUX_SKILLS_CODEX_SIDECAR": "1", "TMUX_SKILLS_CODEX_SDK_DECISION": "", "TMUX_SKILLS_CONFIG": str(config_path)},
                    clear=False,
                ),
            ):
                result = tmux_manager.codex_sdk_inject_decision(
                    {"manager_id": "manager-one", "codex_pane_id": "%9", "workspace": "/tmp/workspace", "state_dir": str(state_dir)},
                    {"event_id": "event-one"},
                    {"safe": True},
                )

        self.assertEqual(calls[0], ["uv", "venv", str(state_dir / "sidecar-venv")])
        self.assertEqual(calls[1][:4], ["uv", "pip", "install", "--python"])
        self.assertEqual(calls[2][0], str(python_path))
        self.assertTrue(calls[2][1].endswith("tmux_codex_sidecar.py"))
        self.assertEqual(helper_requests[0]["sidecar_config"]["model"], "5.3-codex-spark")
        self.assertEqual(helper_requests[0]["sidecar_config"]["reasoning_effort"], "medium")
        self.assertFalse(helper_requests[0]["sidecar_config"]["deterministic_fast_path"])
        self.assertEqual(result["decision"], "inject")
        self.assertEqual(result["source"], "codex_sidecar")
        self.assertEqual(result["sidecar_setup"]["venv"], str(state_dir / "sidecar-venv"))
        self.assertEqual(result["sidecar_config"]["model"], "5.3-codex-spark")

    def test_tmux_inject_delivery_check_sends_followup_when_prompt_staged(self) -> None:
        record = {"manager_id": "manager-one", "codex_pane_id": "%9"}
        candidate = {"event_id": "event-one", "job_id": "job-one"}
        validation = {"safe": True}
        injection = {"injected": True, "pasted": True, "entered": True}
        prompt = self.tmux_inject_prompt("event-one")
        staged_capture = "\n".join(
            [
                "› ID:cbf7f0;",
                "  tmux-skills event ready. Use $tmux-control only.",
                "",
                "  Manager ID: manager-one",
                "  Event ID: event-one",
                "",
                "  tab to queue message",
            ]
        )
        submitted_capture = "\n".join(
            [
                "› ID:cbf7f0;",
                "  tmux-skills event ready. Use $tmux-control only.",
                "",
                "• Working (0s • esc to interrupt)",
            ]
        )

        with (
            mock.patch.object(tmux_manager.time, "sleep"),
            mock.patch.object(
                tmux_manager,
                "capture_tmux_pane_text",
                side_effect=[
                    {"captured": True, "returncode": 0, "output": staged_capture, "omitted_chars": 0},
                    {"captured": True, "returncode": 0, "output": submitted_capture, "omitted_chars": 0},
                ],
            ) as capture,
            mock.patch.object(
                tmux_manager,
                "codex_sdk_inject_followup_decision",
                return_value={
                    "action": "submit",
                    "submit_key": "C-m",
                    "confidence": 0.9,
                    "reason": "wake prompt remains staged",
                    "source": "sdk",
                },
            ),
            mock.patch.object(tmux_manager, "send_tmux_submit_key", return_value={"sent": True, "submit_key": "C-m"}) as send,
        ):
            result = tmux_manager.verify_tmux_inject_delivery(record, candidate, validation, injection, prompt)

        send.assert_called_once_with("%9", "C-m")
        self.assertEqual(capture.call_count, 2)
        for call in capture.call_args_list:
            self.assertEqual(call.kwargs, {})
        self.assertEqual(tmux_manager.TMUX_INJECT_CAPTURE_LINES, 30)
        self.assertEqual(tmux_manager.TMUX_INJECT_CAPTURE_MAX_CHARS, 4000)
        self.assertIn("timing", result)
        self.assertIn("capture_before", result["timing"])
        self.assertTrue(result["checked"])
        self.assertEqual(result["decision"]["action"], "submit")
        self.assertTrue(result["capture_before"]["prompt_still_staged"])
        self.assertFalse(result["capture_after"]["prompt_still_staged"])
        self.assertFalse(result["prompt_still_staged"])

    def test_tmux_inject_staged_prompt_requires_matching_event(self) -> None:
        prompt = self.tmux_inject_prompt("event-two")
        capture = "\n".join(
            [
                "› ID:cbf7f0;",
                "  tmux-skills event ready. Use $tmux-control only.",
                "",
                "  Manager ID: manager-one",
                "  Event ID: event-one",
                "",
                "  tab to queue message",
            ]
        )

        self.assertFalse(tmux_manager.wake_prompt_still_staged(prompt, capture))
        state = tmux_manager.tmux_inject_composer_state(prompt, capture)
        self.assertEqual(state["status"], "other_wake_prompt_staged")
        self.assertFalse(state["safe_to_submit"])

    def test_tmux_inject_preflight_refuses_user_composer_text(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = {
            "captured": True,
            "returncode": 0,
            "output": "\n".join(["› user typed draft", "", "  press enter to submit"]),
            "omitted_chars": 0,
        }

        with (
            mock.patch.object(tmux_manager, "capture_tmux_pane_text", return_value=capture),
            mock.patch.object(tmux_manager.subprocess, "run") as run,
            mock.patch.object(tmux_manager, "send_tmux_submit_key") as send,
        ):
            result = tmux_manager.inject_tmux_wake_prompt("%9", prompt)

        run.assert_not_called()
        send.assert_not_called()
        self.assertFalse(result["pasted"])
        self.assertFalse(result["entered"])
        self.assertEqual(result["preflight"]["composer_state"]["status"], "composer_text_present")
        self.assertIn("not written by tmux-skills", result["reason"])

    def test_tmux_inject_preflight_ignores_historical_prompt_transcript(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = "\n".join(
            [
                "› 지금 manager가 프롬프트 주입을 안하는거 같은데?",
                "",
                "• 현재 manager record와 Codex pane preflight 상태를 다시 확인하겠습니다.",
                "",
                "• Ran python3 scripts/tmux_control.py manager status --manager-id manager-one",
                "  └ {",
                "      \"status\": \"waiting_for_codex\"",
                "    }",
                "",
                "  gpt-5.5 high · main · Context 85% left · weekly 82% left",
            ]
        )

        state = tmux_manager.tmux_inject_composer_state(prompt, capture)

        self.assertEqual(state["status"], "no_composer_text_detected")
        self.assertTrue(state["safe_to_inject"])

    def test_tmux_inject_preflight_refuses_active_user_composer_text_with_footer(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = "\n".join(
            [
                "› Explain this codebase",
                "",
                "  gpt-5.5 high · main · Context 85% left · weekly 82% left",
            ]
        )

        state = tmux_manager.tmux_inject_composer_state(prompt, capture)

        self.assertEqual(state["status"], "composer_text_present")
        self.assertFalse(state["safe_to_inject"])
        self.assertEqual(state["composer_preview"], "Explain this codebase")

    def test_tmux_inject_preflight_allows_dim_placeholder_suggestion(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = "\n".join(
            [
                "› Explain this codebase",
                "",
                "  gpt-5.5 high · main · Context 85% left",
            ]
        )
        raw_capture = "\n".join(
            [
                "\x1b[1m›\x1b[0m \x1b[2mExplain this codebase\x1b[0m",
                "",
                "  gpt-5.5 high · main · Context 85% left",
            ]
        )

        state = tmux_manager.tmux_inject_composer_state(prompt, capture, raw_capture)

        self.assertEqual(state["status"], "placeholder_composer_suggestion")
        self.assertTrue(state["safe_to_inject"])
        self.assertFalse(state["safe_to_submit"])

    def test_receipt_sidecar_capture_tail_omits_placeholder_composer(self) -> None:
        capture = {
            "output": "\n".join(
                [f"old line {index}" for index in range(20)]
                + [
                    "• Ran prior command",
                    "",
                    "› Explain this codebase",
                    "",
                    "  gpt-5.5 high · main · Context 85% left",
                ]
            )
        }
        composer_state = {
            "status": "placeholder_composer_suggestion",
            "safe_to_inject": True,
            "composer_preview": "Explain this codebase",
        }

        tail = tmux_manager.receipt_sidecar_pane_capture_tail(capture, composer_state)

        self.assertIn("Ran prior command", tail)
        self.assertIn("placeholder suggestion omitted", tail)
        self.assertNotIn("Explain this codebase", tail)
        self.assertLessEqual(len(tail), 1200)
        self.assertLessEqual(len(tail.splitlines()), 15)

    def test_receipt_sidecar_capture_tail_is_short_for_non_placeholder(self) -> None:
        capture = {"output": "\n".join(f"line {index} " + ("x" * 100) for index in range(40))}
        tail = tmux_manager.receipt_sidecar_pane_capture_tail(capture, {"status": "no_composer_text_detected"})

        self.assertLessEqual(len(tail), 1200)
        self.assertLessEqual(len(tail.splitlines()), 15)
        self.assertIn("line 39", tail)

    def test_tmux_inject_followup_defers_on_user_composer_text(self) -> None:
        record = {"manager_id": "manager-one", "codex_pane_id": "%9"}
        candidate = {"event_id": "event-one", "job_id": "job-one"}
        validation = {"safe": True}
        injection = {"injected": True, "pasted": True, "entered": True}
        prompt = self.tmux_inject_prompt("event-one")
        capture = {
            "captured": True,
            "returncode": 0,
            "output": "\n".join(["› user draft text", "", "  press enter to submit"]),
            "omitted_chars": 0,
        }

        result = tmux_manager.codex_sdk_inject_followup_decision(record, candidate, validation, injection, capture, prompt)

        self.assertEqual(result["action"], "defer")
        self.assertEqual(result["composer_state"]["status"], "composer_text_present")

    def test_tmux_inject_detects_queued_composer_prompt_while_codex_is_working(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = "\n".join(
            [
                "• Working (8m 01s • esc to interrupt)",
                "",
                "› ID:cbf7f0;",
                "  tmux-skills event ready. Use $tmux-control only.",
                "",
                "  Manager ID: manager-one",
                "  Event ID: event-one",
                "",
                "  Inspect the manager state for this workspace, decide the next action, and acknowledge the event",
                "  after inspection.",
                "",
                "",
                "  tab to queue message",
            ]
        )

        self.assertTrue(tmux_manager.wake_prompt_still_staged(prompt, capture))
        self.assertEqual(tmux_manager.default_tmux_inject_followup_submit_key(prompt, capture), "Tab")

    def test_tmux_inject_deterministic_inspection_queues_staged_prompt_while_working(self) -> None:
        record = {"manager_id": "manager-one", "codex_pane_id": "%9"}
        candidate = {"event_id": "event-one", "job_id": "job-one"}
        validation = {"safe": True}
        injection = {"injected": True, "pasted": True, "entered": True}
        prompt = self.tmux_inject_prompt("event-one")
        capture = {
            "captured": True,
            "returncode": 0,
            "output": "\n".join(
                [
                    "• Working (8m 01s • esc to interrupt)",
                    "",
                    "› ID:cbf7f0;",
                    "  tmux-skills event ready. Use $tmux-control only.",
                    "",
                    "  Manager ID: manager-one",
                    "  Event ID: event-one",
                    "",
                    "  tab to queue message",
                ]
            ),
            "omitted_chars": 0,
        }

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "TMUX_SKILLS_CODEX_SDK_FOLLOWUP_ACTION": ""}, clear=False):
            result = tmux_manager.codex_sdk_inject_followup_decision(record, candidate, validation, injection, capture, prompt)

        self.assertEqual(result["action"], "submit")
        self.assertEqual(result["submit_key"], "Tab")
        self.assertEqual(result["source"], "deterministic")
        self.assertNotIn("OPENAI_API_KEY", result["reason"])

    def test_tmux_inject_user_composer_text_defers_without_auto_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={
                        "injected": False,
                        "pasted": False,
                        "entered": False,
                        "reason": "Codex composer contains text that was not written by tmux-skills",
                        "preflight": {
                            "captured": True,
                            "composer_state": {
                                "status": "composer_text_present",
                                "safe_to_inject": False,
                                "safe_to_submit": False,
                                "reason": "Codex composer contains text that was not written by tmux-skills",
                            },
                        },
                    },
                ) as inject,
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            self.assertEqual(updated["last_notification"]["status"], "deferred")
            self.assertTrue(updated["last_notification"]["requires_manual_resume"])
            self.assertEqual(updated["submitted_event_ids"], [])
            self.assertEqual(updated["events"][status["event_id"]]["notification_status"], "deferred")
            inject.assert_called_once()

            with (
                mock.patch.object(tmux_manager, "tmux_inject_ack_recheck_due", return_value=True) as due,
                mock.patch.object(tmux_manager, "notify_terminal_event") as notify,
            ):
                cycled = tmux_manager.manager_cycle(updated, paths=paths)

            due.assert_not_called()
            notify.assert_not_called()
            self.assertEqual(cycled["last_notification"]["status"], "deferred")

    def test_manager_notification_retry_resumes_deferred_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            event_id = "event-one"
            record = tmux_manager.upsert_notification(
                record,
                event_id,
                {
                    "event_id": event_id,
                    "job_id": "job-one",
                    "mode": "tmux-inject",
                    "status": "deferred",
                    "notification_phase": "deferred",
                    "requires_manual_resume": True,
                    "defer_reason": "composer_text_present",
                    "submitted_to_tmux": True,
                    "injected_to_tmux": False,
                },
            )
            record["submitted_event_ids"] = [event_id]
            record["events"] = {
                event_id: {
                    "event_id": event_id,
                    "job_id": "job-one",
                    "notification_status": "deferred",
                    "requires_manual_resume": True,
                    "defer_reason": "composer_text_present",
                }
            }
            tmux_manager.write_manager_record(paths, record)

            listed = tmux_manager.manager_notification_list(manager_id="manager-one", workspace=str(workspace), status="deferred")
            self.assertTrue(listed["found"])
            self.assertEqual([item["event_id"] for item in listed["notifications"]], [event_id])
            self.assertTrue(listed["notifications"][0]["requires_manual_resume"])

            retried = tmux_manager.manager_notification_retry(
                manager_id="manager-one",
                event_id=event_id,
                workspace=str(workspace),
                note="resume after user review",
            )

            self.assertTrue(retried["retried"])
            updated = retried["record"]
            self.assertEqual(updated["last_notification"]["status"], "inject_pending")
            self.assertEqual(updated["last_notification"]["notification_phase"], "manual_retry_ready")
            self.assertFalse(updated["last_notification"]["requires_manual_resume"])
            self.assertEqual(updated["submitted_event_ids"], [])
            self.assertEqual(updated["events"][event_id]["notification_status"], "inject_pending")

    def test_manager_notification_discard_and_clear_finished_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            event_id = "event-one"
            record = tmux_manager.upsert_notification(
                record,
                event_id,
                {
                    "event_id": event_id,
                    "job_id": "job-one",
                    "mode": "tmux-inject",
                    "status": "deferred",
                    "requires_manual_resume": True,
                },
            )
            record["events"] = {event_id: {"event_id": event_id, "notification_status": "deferred"}}
            tmux_manager.write_manager_record(paths, record)

            discarded = tmux_manager.manager_notification_discard(
                manager_id="manager-one",
                event_id=event_id,
                workspace=str(workspace),
                note="drop stale wake",
            )
            self.assertTrue(discarded["discarded"])
            self.assertEqual(discarded["record"]["last_notification"]["status"], "discarded")
            self.assertTrue(discarded["record"]["events"][event_id]["acknowledged_by_codex"])

            cleared = tmux_manager.manager_notification_clear(manager_id="manager-one", workspace=str(workspace))
            self.assertTrue(cleared["cleared"])
            self.assertEqual(cleared["removed_event_ids"], [event_id])
            self.assertEqual(cleared["record"]["notifications"], [])

    def test_tmux_inject_followup_can_use_codex_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            state_dir = Path(tmp_name) / "state"
            python_path = state_dir / "sidecar-venv" / "bin" / "python"
            python_path.parent.mkdir(parents=True)
            python_path.write_text("", encoding="utf-8")
            (state_dir / "sidecar-venv" / ".openai-codex-installed").write_text("ok", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "source": "codex_sidecar",
                            "output": json.dumps({"action": "confirmed", "submit_key": "Enter", "confidence": 0.7, "reason": "prompt is no longer staged"}),
                        }
                    ),
                    stderr="",
                )

            record = {"manager_id": "manager-one", "codex_pane_id": "%9", "workspace": "/tmp/workspace", "state_dir": str(state_dir)}
            candidate = {"event_id": "event-one", "job_id": "job-one"}
            validation = {"safe": True}
            injection = {"injected": True, "pasted": True, "entered": True}
            prompt = self.tmux_inject_prompt("event-one")
            capture = {
                "captured": True,
                "returncode": 0,
                "output": "\n".join(
                    [
                        "• Working (8m 01s • esc to interrupt)",
                        "",
                        "received manager event event-one",
                    ]
                ),
                "omitted_chars": 0,
            }

            with (
                mock.patch.object(tmux_manager, "codex_sidecar_fast_path_enabled", return_value=False),
                mock.patch.object(tmux_manager.subprocess, "run", side_effect=fake_run),
                mock.patch.dict(os.environ, {"TMUX_SKILLS_CODEX_SIDECAR": "1", "TMUX_SKILLS_CODEX_SDK_FOLLOWUP_ACTION": ""}, clear=False),
            ):
                result = tmux_manager.codex_sdk_inject_followup_decision(record, candidate, validation, injection, capture, prompt)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], str(python_path))
        self.assertTrue(calls[0][1].endswith("tmux_codex_sidecar.py"))
        self.assertEqual(result["action"], "confirmed")
        self.assertEqual(result["submit_key"], "Enter")
        self.assertEqual(result["source"], "codex_sidecar")
        self.assertTrue(result["sidecar_setup"]["reused"])

    def test_tmux_inject_stays_pending_when_prompt_remains_staged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ),
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "decision": {"action": "submit"},
                        "prompt_still_staged": True,
                    },
                ),
            ):
                updated = tmux_manager.transition_terminal(record, paths=paths, status=status)

            self.assertEqual(updated["last_notification"]["status"], "inject_pending")
            self.assertIn("still staged", updated["last_notification"]["reason"])

    def test_tmux_inject_pending_recheck_does_not_paste_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)
            record["submitted_event_ids"] = []
            record["last_notification"] = {
                "mode": "tmux-inject",
                "event_id": status["event_id"],
                "status": "inject_pending",
                "submitted_to_tmux": True,
                "injected_to_tmux": True,
                "injection": {"injected": True, "pasted": True, "entered": True},
                "prompt_sha256": "old-sha",
            }
            record["notifications"] = [record["last_notification"]]

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(tmux_manager, "codex_sdk_inject_decision") as planner,
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={"checked": True, "decision": {"action": "submit"}, "prompt_still_staged": False},
                ) as delivery,
            ):
                updated = tmux_manager.notify_terminal_event(record, status)

            planner.assert_not_called()
            inject.assert_not_called()
            delivery.assert_called_once()
            self.assertEqual(updated["last_notification"]["status"], "awaiting_receipt")
            self.assertEqual(updated["submitted_event_ids"], [status["event_id"]])

    def test_tmux_inject_rechecks_unacked_injected_event_after_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ) as inject,
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "checked_at": "2000-01-01T00:00:00Z",
                        "decision": {"action": "confirmed"},
                        "prompt_still_staged": False,
                    },
                ) as delivery,
                mock.patch.object(
                    tmux_manager,
                    "capture_tmux_pane_text",
                    return_value={"captured": True, "returncode": 0, "output": "• Working (1s • esc to interrupt)", "omitted_chars": 0},
                ) as capture,
            ):
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                updated = tmux_manager.manager_cycle(first, paths=paths)

            self.assertEqual(inject.call_count, 1)
            self.assertEqual(delivery.call_count, 1)
            capture.assert_called_once()
            self.assertEqual(updated["last_notification"]["status"], "awaiting_receipt")
            self.assertEqual(updated["last_notification"]["receipt_check"]["action"], "wait")
            self.assertEqual(updated["submitted_event_ids"], [status["event_id"]])

    def test_tmux_inject_waits_on_idle_empty_receipt_when_sidecar_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ) as inject,
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "checked_at": "2000-01-01T00:00:00Z",
                        "decision": {"action": "confirmed"},
                        "prompt_still_staged": False,
                    },
                ) as delivery,
                mock.patch.object(
                    tmux_manager,
                    "capture_tmux_pane_text",
                    return_value={
                        "captured": True,
                        "returncode": 0,
                        "output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "raw_output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "omitted_chars": 0,
                    },
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_receipt_recheck_decision",
                    return_value={
                        "action": "wait",
                        "status": "awaiting_receipt",
                        "confidence": 0.0,
                        "reason": "Codex sidecar receipt decision unavailable; waiting instead of retrying",
                        "source": "codex_sidecar_unavailable",
                        "retry_count": 0,
                        "sidecar_check_count": 1,
                    },
                ),
            ):
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                updated = tmux_manager.manager_cycle(first, paths=paths)

            self.assertEqual(inject.call_count, 1)
            self.assertEqual(delivery.call_count, 1)
            self.assertEqual(updated["last_notification"]["status"], "awaiting_receipt")
            self.assertEqual(updated["last_notification"]["receipt_check"]["action"], "wait")
            self.assertEqual(updated["last_notification"]["receipt_check"]["source"], "codex_sidecar_unavailable")
            self.assertEqual(updated["last_notification"]["receipt_retry_count"], 0)
            self.assertEqual(updated["last_notification"]["receipt_sidecar_check_count"], 1)
            self.assertEqual(updated["submitted_event_ids"], [status["event_id"]])

    def test_receipt_sidecar_history_is_debug_only(self) -> None:
        receipt_check = {
            "action": "wait",
            "status": "awaiting_receipt",
            "confidence": 0.42,
            "reason": "sidecar chose wait while Codex may be queued",
            "source": "codex_sidecar",
            "retry_count": 0,
            "next_sidecar_check_count": 1,
            "composer_state": {"status": "placeholder_composer_suggestion"},
            "sidecar_debug": {"final_response_tail": '{"action":"wait"}'},
        }
        with mock.patch.dict(os.environ, {"TMUX_SKILLS_DEBUG_SIDECAR": "0"}, clear=False):
            self.assertIsNone(tmux_manager.append_receipt_sidecar_history({}, receipt_check, checked_at="now"))
        with mock.patch.dict(os.environ, {"TMUX_SKILLS_DEBUG_SIDECAR": "1"}, clear=False):
            history = tmux_manager.append_receipt_sidecar_history({}, receipt_check, checked_at="now")

        self.assertIsNotNone(history)
        self.assertEqual(history[0]["action"], "wait")
        self.assertEqual(history[0]["sidecar_check_count"], 1)
        self.assertEqual(history[0]["sidecar_debug"]["final_response_tail"], '{"action":"wait"}')

    def test_receipt_sidecar_block_on_placeholder_is_overridden_to_wait(self) -> None:
        record = {"manager_id": "manager-one", "workspace": "/tmp/workspace", "state_dir": "/tmp/state", "codex_pane_id": "%9"}
        candidate = {"event_id": "event-one", "job_id": "job-one"}
        capture = {"output": "› Explain this codebase\n\n  gpt-5.5 high · main · Context 85% left"}
        composer_state = {
            "status": "placeholder_composer_suggestion",
            "safe_to_inject": True,
            "safe_to_submit": False,
            "composer_preview": "Explain this codebase",
        }
        calls: list[dict[str, object]] = []

        def fake_sidecar(payload: dict[str, object], *, timeout_seconds: float = 90.0) -> dict[str, object]:
            calls.append(payload)
            return {
                "source": "codex_sidecar",
                "action": "block",
                "submit_key": "",
                "confidence": 0.96,
                "reason": "composer contains visible text Explain this codebase",
            }

        with mock.patch.object(tmux_manager, "codex_sidecar_decision", side_effect=fake_sidecar):
            result = tmux_manager.codex_sdk_receipt_recheck_decision(
                record,
                candidate,
                capture,
                composer_state,
                retry_count=0,
                sidecar_check_count=1,
            )

        self.assertEqual(result["action"], "wait")
        self.assertEqual(result["status"], "awaiting_receipt")
        self.assertEqual(result["sidecar_overridden_action"], "block")
        self.assertNotIn("Explain this codebase", calls[0]["pane_capture_tail"])

    def test_debug_receipt_sidecar_history_is_saved_on_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.dict(os.environ, {"TMUX_SKILLS_DEBUG_SIDECAR": "1"}, clear=False),
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ),
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "checked_at": "2000-01-01T00:00:00Z",
                        "decision": {"action": "confirmed"},
                        "prompt_still_staged": False,
                    },
                ),
                mock.patch.object(
                    tmux_manager,
                    "capture_tmux_pane_text",
                    return_value={
                        "captured": True,
                        "returncode": 0,
                        "output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "raw_output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "omitted_chars": 0,
                    },
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_receipt_recheck_decision",
                    return_value={
                        "action": "wait",
                        "status": "awaiting_receipt",
                        "confidence": 0.7,
                        "reason": "sidecar chose wait because the prompt may be queued",
                        "source": "codex_sidecar",
                        "retry_count": 0,
                        "sidecar_check_count": 1,
                        "sidecar_debug": {"final_response_tail": '{"action":"wait"}'},
                    },
                ),
            ):
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                updated = tmux_manager.manager_cycle(first, paths=paths)

        history = updated["last_notification"]["receipt_sidecar_history"]
        self.assertEqual(history[0]["action"], "wait")
        self.assertEqual(history[0]["source"], "codex_sidecar")
        self.assertEqual(history[0]["sidecar_debug"]["final_response_tail"], '{"action":"wait"}')
        self.assertIn("last action=wait", updated["last_notification"]["receipt_debug_summary"])

    def test_tmux_inject_retries_unacked_receipt_only_when_sidecar_chooses_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)

            with (
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_inject_decision",
                    return_value={"decision": "inject", "target_pane": "%9", "confidence": 1.0, "reason": "ok"},
                ),
                mock.patch.object(
                    tmux_manager,
                    "inject_tmux_wake_prompt",
                    return_value={"injected": True, "pasted": True, "entered": True},
                ) as inject,
                mock.patch.object(
                    tmux_manager,
                    "verify_tmux_inject_delivery",
                    return_value={
                        "checked": True,
                        "checked_at": "2000-01-01T00:00:00Z",
                        "decision": {"action": "confirmed"},
                        "prompt_still_staged": False,
                    },
                ) as delivery,
                mock.patch.object(
                    tmux_manager,
                    "capture_tmux_pane_text",
                    return_value={
                        "captured": True,
                        "returncode": 0,
                        "output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "raw_output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "omitted_chars": 0,
                    },
                ),
                mock.patch.object(
                    tmux_manager,
                    "codex_sdk_receipt_recheck_decision",
                    return_value={
                        "action": "retry",
                        "status": "awaiting_receipt",
                        "confidence": 0.8,
                        "reason": "sidecar judged prior wake prompt was lost",
                        "source": "codex_sidecar",
                        "retry_count": 0,
                        "sidecar_check_count": 1,
                    },
                ) as sidecar,
            ):
                first = tmux_manager.transition_terminal(record, paths=paths, status=status)
                updated = tmux_manager.manager_cycle(first, paths=paths)

            sidecar.assert_called_once()
            self.assertEqual(inject.call_count, 2)
            self.assertEqual(delivery.call_count, 2)
            self.assertEqual(updated["last_notification"]["status"], "awaiting_receipt")
            self.assertEqual(updated["last_notification"]["receipt_check"]["action"], "retry")
            self.assertEqual(updated["last_notification"]["receipt_retry_count"], 1)
            self.assertEqual(updated["last_notification"]["receipt_sidecar_check_count"], 1)
            self.assertEqual(updated["submitted_event_ids"], [status["event_id"]])

    def test_tmux_inject_receipt_recheck_does_not_retry_when_same_wake_id_is_visible(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = {
            "captured": True,
            "returncode": 0,
            "output": "queued messages\nID:cbf7f0;\ntmux-skills event ready. Use $tmux-control only.\n",
            "raw_output": "queued messages\nID:cbf7f0;\ntmux-skills event ready. Use $tmux-control only.\n",
            "omitted_chars": 0,
        }
        existing = {"receipt_retry_count": 0, "receipt_sidecar_check_count": 0}

        with mock.patch.object(tmux_manager, "codex_sdk_receipt_recheck_decision") as sidecar:
            result = tmux_manager.tmux_inject_receipt_recheck_decision(prompt, capture, existing, record={}, candidate={})

        sidecar.assert_not_called()
        self.assertEqual(result["action"], "wait")
        self.assertEqual(result["status"], "queued_in_codex")
        self.assertTrue(result["wake_visibility"]["same_wake_visible"])

    def test_tmux_inject_preflight_blocks_visible_wake_id_without_active_composer(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")

        same = tmux_manager.tmux_inject_composer_state(prompt, "queued messages\nID:cbf7f0;\n")
        other = tmux_manager.tmux_inject_composer_state(prompt, "queued messages\nID:0ef821;\n")

        self.assertEqual(same["status"], "same_wake_prompt_visible")
        self.assertFalse(same["safe_to_inject"])
        self.assertEqual(other["status"], "other_wake_prompt_staged")
        self.assertFalse(other["safe_to_inject"])

    def test_tmux_inject_receipt_recheck_blocks_when_other_wake_id_is_visible(self) -> None:
        prompt = self.tmux_inject_prompt("event-one")
        capture = {
            "captured": True,
            "returncode": 0,
            "output": "queued messages\nID:0ef821;\ntmux-skills event ready. Use $tmux-control only.\n",
            "raw_output": "queued messages\nID:0ef821;\ntmux-skills event ready. Use $tmux-control only.\n",
            "omitted_chars": 0,
        }

        with mock.patch.object(tmux_manager, "codex_sdk_receipt_recheck_decision") as sidecar:
            result = tmux_manager.tmux_inject_receipt_recheck_decision(prompt, capture, {}, record={}, candidate={})

        sidecar.assert_not_called()
        self.assertEqual(result["action"], "block")
        self.assertEqual(result["status"], "blocked_by_other_wake")
        self.assertEqual(result["wake_visibility"]["other_wake_ids"], ["0ef821"])

    def test_tmux_inject_stale_handled_visible_wake_does_not_block_latest_event(self) -> None:
        prompt = self.tmux_inject_prompt("event-two")
        record = {
            "notifications": [
                {
                    "event_id": "event-one",
                    "wake_id": "cbf7f0",
                    "status": "queued_in_codex",
                    "acknowledged_by_codex": True,
                    "handled_by_job_id": "job-two",
                }
            ],
            "events": {
                "event-one": {
                    "wake_id": "cbf7f0",
                    "acknowledged_by_codex": True,
                    "handled_by_job_id": "job-two",
                }
            },
        }
        capture_output = "old transcript\nID:cbf7f0;\ntmux-skills event ready. Use $tmux-control only.\n"
        state = tmux_manager.tmux_inject_composer_state(prompt, capture_output, record=record)

        self.assertEqual(state["status"], "no_composer_text_detected")
        self.assertTrue(state["safe_to_inject"])
        self.assertEqual(state["stale_or_handled_wake_ids"], ["cbf7f0"])
        self.assertEqual(state["other_wake_ids"], [])

        capture = {"captured": True, "returncode": 0, "output": capture_output, "raw_output": capture_output, "omitted_chars": 0}
        with mock.patch.object(
            tmux_manager,
            "codex_sdk_receipt_recheck_decision",
            return_value={"action": "wait", "status": "awaiting_receipt", "reason": "sidecar waits"},
        ) as sidecar:
            result = tmux_manager.tmux_inject_receipt_recheck_decision(prompt, capture, {}, record=record, candidate={"event_id": "event-two"})

        sidecar.assert_called_once()
        self.assertEqual(result["status"], "awaiting_receipt")
        self.assertEqual(result["composer_state"]["stale_or_handled_wake_ids"], ["cbf7f0"])

    def test_tmux_inject_unacked_other_visible_wake_blocks_latest_event(self) -> None:
        prompt = self.tmux_inject_prompt("event-two")
        record = {
            "notifications": [
                {
                    "event_id": "event-one",
                    "wake_id": "cbf7f0",
                    "status": "queued_in_codex",
                    "acknowledged_by_codex": False,
                    "submitted_to_tmux": True,
                }
            ],
            "events": {"event-one": {"wake_id": "cbf7f0", "notification_status": "queued_in_codex"}},
        }
        state = tmux_manager.tmux_inject_composer_state(prompt, "queued messages\nID:cbf7f0;\n", record=record)

        self.assertEqual(state["status"], "other_wake_prompt_staged")
        self.assertFalse(state["safe_to_inject"])
        self.assertEqual(state["active_other_wake_ids"], ["cbf7f0"])

    def test_tmux_inject_unknown_visible_wake_blocks_latest_event(self) -> None:
        prompt = self.tmux_inject_prompt("event-two")
        state = tmux_manager.tmux_inject_composer_state(prompt, "queued messages\nID:cbf7f0;\n", record={"notifications": [], "events": {}})

        self.assertEqual(state["status"], "other_wake_prompt_staged")
        self.assertFalse(state["safe_to_inject"])
        self.assertEqual(state["unknown_wake_ids"], ["cbf7f0"])

    def test_tmux_inject_blocks_receipt_recovery_after_sidecar_check_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            status = self.build_terminal_status(paths)
            record["submitted_event_ids"] = [status["event_id"]]
            record["last_notification"] = {
                "mode": "tmux-inject",
                "event_id": status["event_id"],
                "status": "awaiting_receipt",
                "submitted_to_tmux": True,
                "injected_to_tmux": True,
                "injection": {"injected": True, "pasted": True, "entered": True},
                "receipt_retry_count": 0,
                "receipt_sidecar_check_count": 1,
                "delivery_check": {"checked_at": "2000-01-01T00:00:00Z"},
            }
            record["notifications"] = [record["last_notification"]]
            record["events"] = {status["event_id"]: {"event_id": status["event_id"], "source": "manager_terminal", "acknowledged_by_codex": False}}

            with (
                mock.patch.dict(os.environ, {"TMUX_SKILLS_TMUX_INJECT_RECEIPT_SIDECAR_CHECK_MAX": "1"}, clear=False),
                mock.patch.object(tmux_manager, "pane_codex_validation", return_value={"safe": True, "status": "live_codex"}),
                mock.patch.object(
                    tmux_manager,
                    "capture_tmux_pane_text",
                    return_value={
                        "captured": True,
                        "returncode": 0,
                        "output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "raw_output": "› \n\n  gpt-5.5 high · main · Context 85% left",
                        "omitted_chars": 0,
                    },
                ),
                mock.patch.object(tmux_manager, "codex_sdk_receipt_recheck_decision") as sidecar,
                mock.patch.object(tmux_manager, "inject_tmux_wake_prompt") as inject,
            ):
                updated = tmux_manager.notify_terminal_event(record, status)

            sidecar.assert_not_called()
            inject.assert_not_called()
            self.assertEqual(updated["last_notification"]["status"], "receipt_blocked")
            self.assertIn("sidecar check limit", updated["last_notification"]["reason"])

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
            self.assertIn(f"Event ID: {status['event_id']}", prompt)
            self.assertIn("Job ID: job-one", prompt)
            self.assertIn(f"Workspace: {paths['workspace']}", prompt)
            self.assertIn(f"Manager path: {paths['managers'] / 'manager-one.json'}", prompt)
            self.assertIn(f"Status path: {tmux_state.status_path(paths, 'job-one')}", prompt)
            self.assertIn(f"Log path: {tmux_state.log_path(paths, 'job-one')}", prompt)
            for forbidden in ("SECRET OUTPUT", "last_output", "traceback", "retry", "command was", "Please inspect"):
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
            self.assertIn(f"Event ID: {result['event_id']}", prompt)
            self.assertIn("Job ID: none", prompt)
            self.assertIn(f"Workspace: {paths['workspace']}", prompt)
            self.assertIn(f"Manager path: {paths['managers'] / 'manager-one.json'}", prompt)
            self.assertIn("Status path: none", prompt)
            self.assertIn("Task path: none", prompt)
            self.assertIn("Log path: none", prompt)
            for forbidden in ("preflight", "retry", "Please inspect", "last_output"):
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
                    "delivery": {"turn_id": "turn-delivery"},
                },
            )
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.ack_manager_event(
                manager_id="manager-one",
                event_id=event_id,
                workspace=str(workspace),
                note="received",
            )

            self.assertTrue(result["acked"])
            verification = result["record"]["bridge_verification"]
            self.assertEqual(verification["status"], "verified")
            self.assertTrue(verification["acknowledged_by_codex"])
            self.assertEqual(verification["ack_turn_id"], "turn-delivery")
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

    def test_default_notify_route_starts_verifies_submits_or_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))

            self.assertEqual(tmux_manager.default_notify_route(None)["action"], "start_bridge_check")

            diagnostic = self.build_record(paths, notify={"mode": "none"})
            diagnostic["pending_job"] = None
            diagnostic = tmux_manager.write_manager_record(paths, diagnostic)
            refused_diagnostic = tmux_manager.default_notify_route(diagnostic)
            self.assertEqual(refused_diagnostic["action"], "refuse")
            self.assertIn("diagnostics-only", refused_diagnostic["reason"])

            bridge = self.build_record(
                paths,
                {
                    "mode": "bridge",
                    "thread_id": "thr-test",
                    "endpoint": "unix:///tmp/codex.sock",
                    "socket_path": "/tmp/codex.sock",
                },
            )
            bridge["pending_job"] = None
            refused_unverified = tmux_manager.default_notify_route(bridge)
            self.assertEqual(refused_unverified["action"], "refuse")
            self.assertIn("bridge receipt is not verified", refused_unverified["reason"])

            verified = self.mark_bridge_verified(paths, bridge)
            routed = tmux_manager.default_notify_route(verified)
            self.assertEqual(routed["action"], "submit")
            self.assertTrue(routed["allowed"])

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
            self.assertEqual(result["record"]["pending_job"]["pane_id"], "%2")
            self.assertEqual(result["record"]["pending_job"]["pane_index"], "1")

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

    def test_run_next_blocks_tmux_inject_terminal_event_until_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_tmux_inject_record(paths)
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record["last_terminal_event_id"] = "evt-one"
            record = tmux_manager.upsert_notification(
                record,
                "evt-one",
                {
                    "event_id": "evt-one",
                    "mode": "tmux-inject",
                    "status": "injected",
                    "submitted_to_tmux": True,
                    "acknowledged_by_codex": False,
                },
            )
            record["events"] = {
                "evt-one": {
                    "event_id": "evt-one",
                    "source": "manager_terminal",
                    "acknowledged_by_codex": False,
                }
            }
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

    def test_external_ack_merge_preserves_event_summary_ack(self) -> None:
        stale = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "pending_job": None,
            "last_notification": {
                "event_id": "evt-one",
                "status": "inject_pending",
                "acknowledged_by_codex": False,
            },
            "notifications": [
                {
                    "event_id": "evt-one",
                    "status": "inject_pending",
                    "acknowledged_by_codex": False,
                }
            ],
            "events": {
                "evt-one": {
                    "event_id": "evt-one",
                    "source": "manager_terminal",
                    "status": "succeeded",
                    "acknowledged_by_codex": False,
                }
            },
        }
        latest = {
            "manager_id": "manager-one",
            "status": "idle",
            "pending_job": None,
            "last_ack": {"event_id": "evt-one", "turn_id": "turn-main"},
            "notifications": [
                {
                    "event_id": "evt-one",
                    "status": "acknowledged",
                    "acknowledged_by_codex": True,
                    "ack_turn_id": "turn-main",
                    "ack_note": "received",
                }
            ],
            "events": {
                "evt-one": {
                    "event_id": "evt-one",
                    "source": "manager_terminal",
                    "status": "succeeded",
                    "acknowledged_by_codex": True,
                    "ack_turn_id": "turn-main",
                    "ack_note": "received",
                }
            },
        }

        merged = tmux_manager.merge_external_manager_update(stale, latest)

        self.assertTrue(merged["events"]["evt-one"]["acknowledged_by_codex"])
        self.assertEqual(merged["events"]["evt-one"]["ack_note"], "received")
        self.assertTrue(merged["notifications"][0]["acknowledged_by_codex"])

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

    def test_manager_cycle_records_one_terminal_event_and_keeps_other_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            status = self.build_terminal_status(paths, "job-one")
            tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            log_one = tmux_state.log_path(paths, "job-one")
            log_two = tmux_state.log_path(paths, "job-two")
            record["pending_job"] = None
            record["current_job_id"] = "job-two"
            record["worker_pane_id"] = "%2"
            record["worker_pane_ids"] = ["%2", "%4"]
            record["active_job_ids"] = ["job-one", "job-two"]
            record["job_ids"] = ["job-one", "job-two"]
            record["jobs"] = {
                "job-one": {"job_id": "job-one", "pane_id": "%2", "status_path": str(tmux_state.status_path(paths, "job-one")), "log_path": str(log_one)},
                "job-two": {"job_id": "job-two", "pane_id": "%4", "status_path": str(tmux_state.status_path(paths, "job-two")), "log_path": str(log_two)},
            }
            record["status"] = "running"

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                updated = tmux_manager.manager_cycle(record, paths=paths)

            event_id = str(status["event_id"])
            self.assertEqual(updated["status"], "waiting_for_codex")
            self.assertEqual(updated["active_job_ids"], ["job-two"])
            self.assertEqual(updated["jobs"]["job-one"]["terminal_event_id"], event_id)
            self.assertEqual(updated["events"][event_id]["job_id"], "job-one")
            self.assertEqual(updated["events"][event_id]["pane_id"], "%2")
            self.assertEqual(updated["jobs"]["job-two"]["pane_id"], "%4")

    def test_dashboard_text_lists_active_jobs_and_recent_events(self) -> None:
        record = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "manager_pane_id": "%3",
            "manager_pane_index": "0",
            "worker_pane_id": "%2",
            "worker_pane_index": "1",
            "worker_pane_ids": ["%2", "%4"],
            "current_job_id": "job-two",
            "active_job_ids": ["job-two"],
            "job_ids": ["job-one", "job-two"],
            "heartbeat_at": "now",
            "manager_path": "/tmp/manager.json",
            "jobs": {
                "job-one": {"job_id": "job-one", "pane_id": "%2", "pane_index": "1", "status": "failed", "terminal_event_id": "evt-one"},
                "job-two": {"job_id": "job-two", "pane_id": "%4", "pane_index": "2", "status": "running"},
            },
            "events": {
                "evt-one": {
                    "event_id": "evt-one",
                    "source": "manager_terminal",
                    "job_id": "job-one",
                    "pane_id": "%2",
                    "pane_index": "1",
                    "status": "failed",
                    "acknowledged_by_codex": False,
                }
            },
            "last_terminal_event_id": "evt-one",
            "pending_job": {"job_id": "job-three", "pane_id": "%2", "command_file": "/tmp/job-three.sh"},
            "last_notification": {
                "event_id": "evt-one",
                "mode": "none",
                "status": "handled",
                "handled_by_job_id": "job-three",
                "handled_without_ack": False,
            },
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("ACTIVE", text)
        self.assertIn("LATEST EVENT", text)
        self.assertIn("job-two", text)
        self.assertIn("2:%4", text)
        self.assertIn("evt-one", text)
        self.assertIn("failed", text)
        self.assertNotIn("job-three", text)
        self.assertNotIn("/tmp/job-three.sh", text)
        self.assertNotIn("/tmp/manager.json", text)

        jobs_text = tmux_manager.dashboard_text(record, mode="jobs")
        self.assertIn("job-one", jobs_text)
        self.assertIn("job-two", jobs_text)
        self.assertIn("1:%2", jobs_text)
        self.assertIn("2:%4", jobs_text)
        self.assertIn("evt-one", jobs_text)

        events_text = tmux_manager.dashboard_text(record, mode="events")
        self.assertIn("evt-one", events_text)
        self.assertIn("job-one", events_text)
        self.assertIn("failed", events_text)

        clipped = tmux_manager.dashboard_text(record, width=20, height=4)
        clipped_lines = clipped.splitlines()
        self.assertLessEqual(len(clipped_lines), 4)
        self.assertTrue(all(len(line) <= 20 for line in clipped_lines))

    def test_delete_terminal_jobs_removes_evidence_and_preserves_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            done_command = tmux_state.command_path(paths, "done-job")
            done_status = tmux_state.status_path(paths, "done-job")
            done_log = tmux_state.log_path(paths, "done-job")
            error_command = tmux_state.command_path(paths, "error-job")
            error_status = tmux_state.status_path(paths, "error-job")
            error_log = tmux_state.log_path(paths, "error-job")
            active_command = tmux_state.command_path(paths, "active-job")
            active_status = tmux_state.status_path(paths, "active-job")
            active_log = tmux_state.log_path(paths, "active-job")
            for path in (done_command, done_status, done_log, error_command, error_status, error_log, active_command, active_status, active_log):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("evidence", encoding="utf-8")
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%3",
                worker_pane_id="%2",
                manager_pane_index="0",
                worker_pane_index="1",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
            )
            record["status"] = "running"
            record["current_job_id"] = "active-job"
            record["active_job_ids"] = ["active-job"]
            record["job_ids"] = ["done-job", "error-job", "active-job"]
            record["jobs"] = {
                "done-job": {
                    "job_id": "done-job",
                    "status": "succeeded",
                    "command_request_path": str(done_command),
                    "status_path": str(done_status),
                    "log_path": str(done_log),
                    "terminal_event_id": "evt-done",
                },
                "error-job": {
                    "job_id": "error-job",
                    "status": "error",
                    "command_request_path": str(error_command),
                    "status_path": str(error_status),
                    "log_path": str(error_log),
                    "terminal_event_id": "evt-error",
                },
                "active-job": {
                    "job_id": "active-job",
                    "status": "running",
                    "command_request_path": str(active_command),
                    "status_path": str(active_status),
                    "log_path": str(active_log),
                },
            }
            record["events"] = {
                "evt-done": {"event_id": "evt-done", "job_id": "done-job", "status": "succeeded"},
                "evt-error": {"event_id": "evt-error", "job_id": "error-job", "status": "error"},
            }
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.delete_terminal_jobs("manager-one", workspace=str(workspace))

            self.assertTrue(result["deleted"])
            self.assertEqual(result["deleted_job_ids"], ["done-job", "error-job"])
            loaded = result["record"]
            self.assertEqual(loaded["active_job_ids"], ["active-job"])
            self.assertEqual(loaded["current_job_id"], "active-job")
            self.assertIn("active-job", loaded["jobs"])
            self.assertNotIn("done-job", loaded["jobs"])
            self.assertNotIn("error-job", loaded["jobs"])
            for path in (done_command, done_status, done_log, error_command, error_status, error_log):
                self.assertFalse(path.exists(), path)
            for path in (active_command, active_status, active_log):
                self.assertTrue(path.exists(), path)

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
            self.assertEqual(result["codex_next_action"], "wait_for_next_manager_event")
            self.assertIn("do not poll", result["manager_controlled_reminder"])
            request_path = Path(result["command_request_path"])
            self.assertEqual(request_path.read_text(encoding="utf-8"), "echo next")
            loaded, _error = tmux_manager.read_manager_record(paths, "manager-one")
            assert loaded is not None
            self.assertEqual(loaded["pending_job"]["job_id"], "job-two")
            self.assertEqual(loaded["last_notification"]["status"], "handled")
            self.assertEqual(loaded["last_notification"]["handled_by_job_id"], "job-two")

    def test_run_next_refuses_when_any_manager_job_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record["active_job_ids"] = ["job-active"]
            record["worker_pane_id"] = "%2"
            record["worker_pane_ids"] = ["%2", "%4"]
            record["jobs"] = {"job-active": {"job_id": "job-active", "pane_id": "%4", "status": "running"}}
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-next",
                command_text="echo next",
                command_file=None,
                workspace=str(workspace),
            )

            self.assertFalse(result["queued"])
            self.assertIn("active jobs", result["reason"])
            self.assertFalse(tmux_manager.manager_command_request_path(paths, "manager-one", "job-next").exists())

    def test_submit_parallel_queue_allows_other_active_job_on_different_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "running"
            record["active_job_ids"] = ["job-active"]
            record["worker_pane_id"] = "%2"
            record["worker_pane_ids"] = ["%2", "%4"]
            record["jobs"] = {"job-active": {"job_id": "job-active", "pane_id": "%4", "status": "running"}}
            tmux_manager.write_manager_record(paths, record)

            result = tmux_manager.queue_manager_job(
                manager_id="manager-one",
                job_id="job-parallel",
                command_text="echo parallel",
                command_file=None,
                workspace=str(workspace),
                pane_id="%2",
                allow_parallel=True,
            )

            self.assertTrue(result["queued"])
            loaded, _error = tmux_manager.read_manager_record(paths, "manager-one")
            assert loaded is not None
            self.assertEqual(loaded["pending_job"]["job_id"], "job-parallel")
            self.assertEqual(loaded["pending_job"]["pane_id"], "%2")

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

    def test_external_pending_job_does_not_requeue_already_started_job(self) -> None:
        processed = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "pending_job": None,
            "heartbeat_at": "new-heartbeat",
            "job_ids": ["job-two"],
            "jobs": {"job-two": {"job_id": "job-two", "status": "failed"}},
        }
        stale_latest = {
            "manager_id": "manager-one",
            "status": "queued",
            "pending_job": {"job_id": "job-two"},
            "heartbeat_at": "old-heartbeat",
        }

        merged = tmux_manager.merge_external_manager_update(processed, stale_latest)

        self.assertIsNone(merged["pending_job"])
        self.assertEqual(merged["heartbeat_at"], "new-heartbeat")

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

    def test_cancel_job_id_interrupts_only_that_job_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = self.build_record(paths)
            record["pending_job"] = None
            record["status"] = "running"
            record["active_job_ids"] = ["job-one", "job-two"]
            record["worker_pane_ids"] = ["%2", "%4"]
            record["jobs"] = {
                "job-one": {"job_id": "job-one", "pane_id": "%2"},
                "job-two": {"job_id": "job-two", "pane_id": "%4"},
            }
            tmux_manager.write_manager_record(paths, record)

            with mock.patch.object(
                tmux_manager,
                "send_worker_interrupt",
                return_value={"sent": True, "returncode": 0, "stderr": ""},
            ) as interrupt:
                result = tmux_manager.cancel_manager("manager-one", workspace=str(workspace), job_id="job-two")

            self.assertTrue(result["cancelled"])
            interrupt.assert_called_once_with("%4")
            self.assertEqual(result["record"]["cancel_job_id"], "job-two")
            self.assertEqual(result["record"]["worker_stop_results"][0]["pane_id"], "%4")

    def test_ensure_dashboard_viewer_launches_once_and_reuses_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
            )
            dashboard_file = Path(str(record["dashboard_path"]))
            tmux_manager.write_dashboard_file(dashboard_file, tmux_manager.dashboard_text(record))
            state_path = tmux_manager.manager_dashboard_viewer_state_path(paths, "manager-one")

            def launch_viewer(*_args: object, **_kwargs: object) -> mock.Mock:
                tmux_state.atomic_write_json(
                    state_path,
                    {
                        "manager_id": "manager-one",
                        "pid": 4242,
                        "pane_id": "%2",
                        "mode": "summary",
                        "heartbeat_at": "now",
                    },
                )
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(tmux_manager, "pane_exists", return_value=True):
                with mock.patch.object(tmux_manager, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_manager, "tmux_command_prefix", return_value=["tmux"]):
                        with mock.patch.object(tmux_manager.subprocess, "run", side_effect=launch_viewer) as run:
                            updated, result = tmux_manager.ensure_dashboard_viewer(record, paths)
                            reused_record, reused = tmux_manager.ensure_dashboard_viewer(updated, paths)

            self.assertTrue(result["started"])
            self.assertFalse(result["reused"])
            self.assertTrue(reused["reused"])
            self.assertEqual(reused_record["dashboard_viewer_pid"], 4242)
            run.assert_called_once()
            argv = run.call_args.args[0]
            command = argv[-2]
            self.assertEqual(argv[:4], ["tmux", "send-keys", "-t", "%2"])
            self.assertEqual(argv[-1], "Enter")
            self.assertIn("tmux_manager_viewer.py", command)
            self.assertIn(str(dashboard_file), command)
            self.assertNotIn("cat", command)
            self.assertNotIn("clear", command)
            self.assertNotIn("while", command)
            self.assertNotIn("sleep", command)

    def test_ensure_dashboard_viewer_can_select_textual_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
            )
            state_path = tmux_manager.manager_dashboard_viewer_state_path(paths, "manager-one")

            def launch_viewer(*_args: object, **_kwargs: object) -> mock.Mock:
                tmux_state.atomic_write_json(
                    state_path,
                    {
                        "manager_id": "manager-one",
                        "pid": 4242,
                        "pane_id": "%2",
                        "backend": "textual",
                        "heartbeat_at": "now",
                    },
                )
                return mock.Mock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.dict(os.environ, {"TMUX_SKILLS_MANAGER_TUI": "textual"}),
                mock.patch.object(tmux_manager, "pane_exists", return_value=True),
                mock.patch.object(tmux_manager, "pid_is_running", return_value=True),
                mock.patch.object(tmux_manager, "tmux_command_prefix", return_value=["tmux"]),
                mock.patch.object(
                    tmux_manager,
                    "ensure_manager_tui_venv",
                    return_value={"ok": True, "python": "/tmp/manager-tui-venv/bin/python", "venv": "/tmp/manager-tui-venv"},
                ) as setup,
                mock.patch.object(tmux_manager.subprocess, "run", side_effect=launch_viewer) as run,
            ):
                updated, result = tmux_manager.ensure_dashboard_viewer(record, paths)

            setup.assert_called_once_with(paths)
            self.assertEqual(updated["dashboard_viewer_backend"], "textual")
            self.assertEqual(result["renderer"], "pane")
            command = run.call_args.args[0][-2]
            self.assertIn("/tmp/manager-tui-venv/bin/python", command)
            self.assertIn("tmux_manager_tui.py", command)

    def test_viewer_state_heartbeat_round_trips_to_manager_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(paths["workspace"]),
                state_dir=str(paths["root"]),
            )
            args = argparse.Namespace(
                manager_id="manager-one",
                manager_file=tmux_manager.manager_record_path(paths, "manager-one"),
                dashboard_file=tmux_manager.manager_dashboard_path(paths, "manager-one"),
                state_file=tmux_manager.manager_dashboard_viewer_state_path(paths, "manager-one"),
                pane_id="%2",
            )

            tmux_manager_viewer.write_viewer_state(args, "events", {"manager_pid": 1234})

            state = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
            self.assertEqual(state["mode"], "events")
            self.assertEqual(state["pane_id"], "%2")
            with mock.patch.object(tmux_manager, "pid_is_running", return_value=True):
                updated = tmux_manager.refresh_dashboard_viewer_fields(record, paths)

            self.assertEqual(updated["dashboard_viewer_pid"], os.getpid())
            self.assertEqual(updated["dashboard_viewer_heartbeat_at"], state["heartbeat_at"])

    def test_viewer_exit_conditions_do_not_cancel_manager_or_workers(self) -> None:
        self.assertTrue(tmux_manager_viewer.should_exit({"status": "cancelled", "manager_pid": os.getpid()}))
        self.assertTrue(tmux_manager_viewer.should_exit(None))
        with mock.patch.object(tmux_manager_viewer, "pid_is_running", return_value=False):
            self.assertTrue(tmux_manager_viewer.should_exit({"status": "running", "manager_pid": 4242}))
        with mock.patch.object(tmux_manager_viewer, "pid_is_running", return_value=True):
            self.assertFalse(tmux_manager_viewer.should_exit({"status": "running", "manager_pid": 4242}))

    def test_dashboard_text_hides_bridge_delivery_ids(self) -> None:
        notification = {
            "event_id": "evt-one",
            "mode": "bridge",
            "status": "acknowledged",
            "submitted_to_app_server": True,
            "acknowledged_by_codex": True,
            "delivery": {"response_id": "resp-one", "turn_id": "turn-one"},
            "ack_turn_id": "turn-main",
        }
        record = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "manager_pane_id": "%3",
            "worker_pane_id": "%2",
            "current_job_id": "job-one",
            "heartbeat_at": "now",
            "manager_path": "/tmp/manager.json",
            "last_terminal_event_id": "evt-one",
            "events": {
                "evt-one": {
                    "event_id": "evt-one",
                    "job_id": "job-one",
                    "status": "succeeded",
                    "acknowledged_by_codex": True,
                }
            },
            "notifications": [notification],
            "last_notification": notification,
            "last_ack": {"event_id": "evt-one", "acknowledged_at": "now", "turn_id": "turn-main"},
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("notify=yes ack=yes", text)
        self.assertNotIn("resp-one", text)
        self.assertNotIn("turn-one", text)
        self.assertNotIn("turn-main", text)
        self.assertNotIn("last_ack_event_id", text)

    def test_dashboard_text_hides_bridge_submission_error_body(self) -> None:
        notification = {
            "event_id": "evt-one",
            "mode": "bridge",
            "status": "submission_failed",
            "submitted_to_app_server": False,
            "acknowledged_by_codex": False,
            "error": "connection refused",
        }
        record = {
            "manager_id": "manager-one",
            "status": "waiting_for_codex",
            "manager_pane_id": "%3",
            "worker_pane_id": "%2",
            "current_job_id": "job-one",
            "heartbeat_at": "now",
            "manager_path": "/tmp/manager.json",
            "last_terminal_event_id": "evt-one",
            "events": {
                "evt-one": {
                    "event_id": "evt-one",
                    "job_id": "job-one",
                    "status": "failed",
                    "acknowledged_by_codex": False,
                }
            },
            "notifications": [notification],
            "last_notification": notification,
        }

        text = tmux_manager.dashboard_text(record)

        self.assertIn("notify=submission_failed ack=no", text)
        self.assertNotIn("connection refused", text)


if __name__ == "__main__":
    unittest.main()
