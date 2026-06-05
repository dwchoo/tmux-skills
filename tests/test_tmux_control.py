from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import shlex
import signal
import threading
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_control
import tmux_bridge
import tmux_manager
import tmux_queue
import tmux_state


WORKFLOWS_DOC = Path(__file__).resolve().parents[1] / "references" / "WORKFLOWS.md"
WORKFLOWS_FEATURES_DOC = Path(__file__).resolve().parents[1] / "docs" / "workflows-and-features.md"
MANAGED_WORKERS_DOC = Path(__file__).resolve().parents[1] / "docs" / "managed-workers.md"
SKILL_DOC = Path(__file__).resolve().parents[1] / "SKILL.md"


def control_commands_from_bash_blocks(path: Path) -> list[str]:
    commands: list[str] = []
    in_bash = False
    current_command: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("```"):
            if in_bash and current_command:
                commands.append(" ".join(current_command))
                current_command = []
            in_bash = line.strip() == "```bash"
            continue
        if not in_bash:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if current_command or stripped.startswith("python scripts/tmux_control.py "):
            if stripped.endswith("\\"):
                current_command.append(stripped[:-1].strip())
            else:
                current_command.append(stripped)
                commands.append(" ".join(current_command))
                current_command = []
    return commands


def top_level_command_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("parser has no top-level subcommands")


class TmuxControlTests(unittest.TestCase):
    def test_parse_current_line_preserves_tabs_inside_fields(self) -> None:
        parts = [
            "session",
            "$1",
            "3",
            "@4",
            "window\tname",
            "%5",
            "0",
            "bash",
            "/tmp/path\twith-tab",
            "title\twith-tab",
            "123",
            "0",
            "80",
            "24",
            "10",
            "5",
            "/dev/ttys001",
        ]
        with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
            parsed = tmux_control.parse_current_line(tmux_control.FIELD_SEP.join(parts))

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["window_name"], "window\tname")
        self.assertEqual(parsed["current_path"], "/tmp/path\twith-tab")
        self.assertEqual(parsed["title"], "title\twith-tab")
        self.assertEqual(parsed["pane_left"], 10)
        self.assertEqual(parsed["pane_top"], 5)

    def test_parse_pane_line_preserves_tabs_inside_fields(self) -> None:
        parts = [
            "session",
            "3",
            "@4",
            "window\tname",
            "%5",
            "0",
            "1",
            "bash",
            "/tmp/path\twith-tab",
            "title\twith-tab",
            "123",
            "0",
            "80",
            "24",
            "10",
            "5",
            "/dev/ttys001",
        ]
        with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
            parsed = tmux_control.parse_pane_line(tmux_control.FIELD_SEP.join(parts), current_pane_id="%5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["window_name"], "window\tname")
        self.assertEqual(parsed["current_path"], "/tmp/path\twith-tab")
        self.assertEqual(parsed["title"], "title\twith-tab")
        self.assertEqual(parsed["pane_left"], 10)
        self.assertEqual(parsed["pane_top"], 5)
        self.assertTrue(parsed["current"])

    def test_parse_pane_line_accepts_tmux_octal_escaped_separator(self) -> None:
        parts = [
            "session",
            "3",
            "@4",
            "window",
            "%5",
            "0",
            "1",
            "bash",
            "/tmp/path",
            "title",
            "123",
            "0",
            "80",
            "24",
            "10",
            "5",
            "/dev/ttys001",
        ]
        with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
            parsed = tmux_control.parse_pane_line("\\037".join(parts), current_pane_id="%5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["session_name"], "session")
        self.assertEqual(parsed["pane_id"], "%5")
        self.assertTrue(parsed["current"])

    def test_capture_max_chars_after_strip(self) -> None:
        args = argparse.Namespace(pane="%1", lines=10, strip_ansi=True, max_chars=4)
        with mock.patch.object(tmux_control, "capture_text", return_value="abcdef"):
            result = tmux_control.capture(args)
        self.assertEqual(result["output"], "cdef")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted_chars"], 2)

    def test_capture_max_chars_zero_omits_all_output(self) -> None:
        args = argparse.Namespace(pane="%1", lines=10, strip_ansi=True, max_chars=0)
        with mock.patch.object(tmux_control, "capture_text", return_value="abcdef"):
            result = tmux_control.capture(args)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted_chars"], 6)

    def test_capture_parser_rejects_negative_max_chars(self) -> None:
        parser = tmux_control.build_parser()
        with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["capture", "--pane", "%1", "--max-chars", "-1"])

    def test_bridge_parser_exposes_contract_subcommands(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "bridge",
                "register",
                "--thread-id",
                "thr_test",
                "--endpoint",
                "unix:///tmp/app.sock",
                "--workspace",
                "/tmp/workspace",
            ]
        )
        self.assertEqual(args.action, "bridge")
        self.assertEqual(args.bridge_action, "register")
        self.assertEqual(args.poll_seconds, 2.0)
        self.assertEqual(args.quiet_seconds, 10.0)

        status_args = parser.parse_args(["bridge", "status", "--bridge-id", "bridge-thr-test", "--json"])
        self.assertEqual(status_args.bridge_action, "status")
        self.assertTrue(status_args.json)

    def test_manager_parser_exposes_contract_subcommands(self) -> None:
        parser = tmux_control.build_parser()
        start_args = parser.parse_args(
            [
                "manager",
                "start",
                "--manager-id",
                "manager-one",
                "--job-id",
                "job-one",
                "--command",
                "echo ok",
                "--notify",
                "bridge",
                "--thread-id",
                "thr_test",
                "--endpoint",
                "unix:///tmp/app.sock",
            ]
        )
        self.assertEqual(start_args.action, "manager")
        self.assertEqual(start_args.manager_action, "start")
        self.assertEqual(start_args.notify, "bridge")

        default_start_args = parser.parse_args(
            ["manager", "start", "--job-id", "job-one", "--command", "echo ok", "--notify", "none"]
        )
        self.assertIsNone(default_start_args.manager_id)

        idle_start_args = parser.parse_args(["manager", "start", "--notify", "none"])
        self.assertIsNone(idle_start_args.job_id)
        self.assertIsNone(idle_start_args.command_text)
        self.assertEqual(idle_start_args.log_max_bytes, tmux_manager.DEFAULT_MANAGER_LOG_MAX_BYTES)

        status_args = parser.parse_args(["manager", "status", "--manager-id", "manager-one"])
        self.assertEqual(status_args.manager_action, "status")
        default_status_args = parser.parse_args(["manager", "status"])
        self.assertIsNone(default_status_args.manager_id)

        bridge_check_args = parser.parse_args(
            ["manager", "bridge-check", "--manager-id", "manager-one", "--ack-timeout-seconds", "0.5"]
        )
        self.assertEqual(bridge_check_args.manager_action, "bridge-check")
        self.assertEqual(bridge_check_args.ack_timeout_seconds, 0.5)

        ack_args = parser.parse_args(
            ["manager", "ack", "--manager-id", "manager-one", "--event-id", "evt-one", "--turn-id", "turn-main"]
        )
        self.assertEqual(ack_args.manager_action, "ack")
        self.assertEqual(ack_args.event_id, "evt-one")
        self.assertEqual(ack_args.turn_id, "turn-main")

        next_args = parser.parse_args(
            ["manager", "run-next", "--manager-id", "manager-one", "--job-id", "job-two", "--command", "echo next"]
        )
        self.assertEqual(next_args.manager_action, "run-next")

        cancel_args = parser.parse_args(["manager", "cancel", "--manager-id", "manager-one", "--stop-worker"])
        self.assertEqual(cancel_args.manager_action, "cancel")
        self.assertTrue(cancel_args.stop_worker)

        cleanup_args = parser.parse_args(["manager", "cleanup", "--manager-id", "manager-one", "--jobs"])
        self.assertEqual(cleanup_args.manager_action, "cleanup")
        self.assertTrue(cleanup_args.jobs)

    def test_manager_start_missing_bridge_config_returns_json_error_before_tmux(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "manager",
                "start",
                "--manager-id",
                "manager-one",
                "--job-id",
                "job-one",
                "--command",
                "echo ok",
            ]
        )

        with mock.patch.object(tmux_control, "manager_layout") as layout:
            result = tmux_control.manager(args)

        layout.assert_not_called()
        self.assertFalse(result["started"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("--thread-id", result["reason"])

    def test_manager_start_rejects_command_without_job_before_tmux(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(["manager", "start", "--command", "echo ok", "--notify", "none"])

        with mock.patch.object(tmux_control, "manager_layout") as layout:
            result = tmux_control.manager(args)

        layout.assert_not_called()
        self.assertFalse(result["started"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("--job-id", result["reason"])

    def test_manager_start_with_bridge_command_requires_verified_receipt_before_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            parser = tmux_control.build_parser()
            args = parser.parse_args(
                [
                    "manager",
                    "start",
                    "--manager-id",
                    "manager-one",
                    "--job-id",
                    "job-one",
                    "--command",
                    "echo ok",
                    "--notify",
                    "bridge",
                    "--thread-id",
                    "thr-test",
                    "--endpoint",
                    "unix:///tmp/app.sock",
                    "--workspace",
                    str(workspace),
                ]
            )

            with mock.patch.object(tmux_control, "manager_layout") as layout:
                result = tmux_control.manager(args)

            layout.assert_not_called()
            self.assertFalse(result["started"])
            self.assertIn("bridge receipt is not verified", result["reason"])
            paths = tmux_manager.manager_paths(str(workspace))
            self.assertFalse(tmux_manager.manager_command_request_path(paths, "manager-one", "job-one").exists())

    def test_manager_ack_dispatches_to_tmux_manager(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "manager",
                "ack",
                "--manager-id",
                "manager-one",
                "--event-id",
                "evt-one",
                "--turn-id",
                "turn-main",
                "--note",
                "received",
                "--workspace",
                "/tmp/workspace",
            ]
        )
        result = {"manager_id": "manager-one", "event_id": "evt-one", "acked": True}

        with mock.patch.object(tmux_manager, "ack_manager_event", return_value=result) as ack:
            actual = tmux_control.manager(args)

        self.assertEqual(actual, result)
        ack.assert_called_once_with(
            manager_id="manager-one",
            event_id="evt-one",
            workspace="/tmp/workspace",
            state_dir=None,
            turn_id="turn-main",
            note="received",
        )

    def test_manager_bridge_check_dispatches_to_tmux_manager(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "manager",
                "bridge-check",
                "--manager-id",
                "manager-one",
                "--ack-timeout-seconds",
                "0.5",
                "--workspace",
                "/tmp/workspace",
            ]
        )
        result = {"manager_id": "manager-one", "event_id": "evt-one", "verified": True}

        with mock.patch.object(tmux_manager, "bridge_check_manager", return_value=result) as bridge_check:
            actual = tmux_control.manager(args)

        self.assertEqual(actual, result)
        bridge_check.assert_called_once_with(
            manager_id="manager-one",
            workspace="/tmp/workspace",
            state_dir=None,
            ack_timeout_seconds=0.5,
        )

    def test_manager_cleanup_refuses_live_manager_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            request_path = tmux_manager.write_command_request(paths, "manager-one", "job-one", "echo ok")
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=tmux_manager.build_pending_job("job-one", request_path, str(workspace)),
                notify={"mode": "none"},
                workspace=str(workspace),
                state_dir=str(paths["root"]),
            )
            record["manager_pid"] = 424242
            tmux_manager.write_manager_record(paths, record)
            args = argparse.Namespace(
                manager_action="cleanup",
                manager_id="manager-one",
                jobs=True,
                force=False,
                workspace=str(workspace),
                state_dir=None,
            )

            with mock.patch.object(tmux_control, "pid_is_running", return_value=True):
                result = tmux_control.manager(args)

            self.assertFalse(result["cleaned"])
            self.assertIn("live manager", result["reason"])

    def test_manager_layout_reuses_record_panes_without_splitting(self) -> None:
        codex_pane = {
            "session_name": "session",
            "window_index": "0",
            "window_id": "@1",
            "pane_id": "%1",
            "pane_dead": False,
            "pane_left": 0,
            "pane_top": 0,
            "pane_width": 100,
            "pane_height": 30,
        }
        manager_pane = {
            **codex_pane,
            "pane_id": "%2",
            "pane_top": 31,
            "pane_width": 100,
            "pane_height": 10,
        }
        worker_pane = {
            **codex_pane,
            "pane_id": "%3",
            "pane_left": 101,
            "pane_top": 0,
            "pane_width": 80,
            "pane_height": 41,
        }
        pane_by_id = {pane["pane_id"]: pane for pane in (codex_pane, manager_pane, worker_pane)}

        def fake_current_info(target: str | None = None) -> dict[str, object] | None:
            return codex_pane if target is None else pane_by_id.get(target)

        with mock.patch.object(tmux_control, "inside_tmux", return_value=True):
            with mock.patch.object(tmux_control, "current_info", side_effect=fake_current_info):
                with mock.patch.object(tmux_control, "current_window_target", return_value="session:0"):
                    with mock.patch.object(tmux_control, "panes_for_target", return_value=list(pane_by_id.values())):
                        with mock.patch.object(tmux_control, "idle_shell_check", return_value={"ok": True}):
                            with mock.patch.object(tmux_control, "run_tmux", return_value=mock.Mock(stdout="")) as run_tmux:
                                result = tmux_control.manager_layout(
                                    Path("/tmp/work"),
                                    existing_record={"manager_pane_id": "%2", "worker_pane_id": "%3"},
                                )

        split_calls = [call for call in run_tmux.call_args_list if call.args[0][0] == "split-window"]
        self.assertEqual(split_calls, [])
        self.assertEqual(result["manager_pane_id"], "%2")
        self.assertEqual(result["worker_pane_id"], "%3")
        self.assertTrue(result["manager_reused"])
        self.assertTrue(result["worker_reused"])

    def test_manager_layout_reuses_idle_pane_below_codex_for_manager(self) -> None:
        codex_pane = {
            "session_name": "session",
            "window_index": "0",
            "window_id": "@1",
            "pane_id": "%1",
            "pane_dead": False,
            "pane_left": 0,
            "pane_top": 0,
            "pane_width": 120,
            "pane_height": 30,
        }
        below_pane = {
            **codex_pane,
            "pane_id": "%2",
            "pane_top": 31,
            "pane_height": 10,
        }
        pane_by_id = {"%1": codex_pane, "%2": below_pane, "%3": {**below_pane, "pane_id": "%3", "pane_left": 60}}

        def fake_current_info(target: str | None = None) -> dict[str, object] | None:
            return codex_pane if target is None else pane_by_id.get(target)

        split_result = mock.Mock(stdout=tmux_control.FIELD_SEP.join(["session", "@1", "%3"]))
        with mock.patch.object(tmux_control, "inside_tmux", return_value=True):
            with mock.patch.object(tmux_control, "current_info", side_effect=fake_current_info):
                with mock.patch.object(tmux_control, "current_window_target", return_value="session:0"):
                    with mock.patch.object(tmux_control, "panes_for_target", return_value=[codex_pane, below_pane]):
                        with mock.patch.object(tmux_control, "idle_shell_check", return_value={"ok": True}):
                            with mock.patch.object(tmux_control, "run_tmux", return_value=split_result) as run_tmux:
                                result = tmux_control.manager_layout(Path("/tmp/work"))

        split_calls = [call.args[0] for call in run_tmux.call_args_list if call.args[0][0] == "split-window"]
        self.assertEqual(len(split_calls), 1)
        self.assertIn("-h", split_calls[0])
        self.assertIn("%1", split_calls[0])
        self.assertIn("-f", split_calls[0])
        self.assertNotIn("-v", split_calls[0])
        self.assertEqual(result["manager_pane_id"], "%2")
        self.assertEqual(result["worker_pane_id"], "%3")
        self.assertTrue(result["manager_reused"])
        self.assertFalse(result["worker_reused"])

    def test_manager_layout_creates_long_worker_before_small_manager(self) -> None:
        codex_pane = {
            "session_name": "session",
            "window_index": "0",
            "window_id": "@1",
            "pane_id": "%1",
            "pane_dead": False,
            "pane_left": 0,
            "pane_top": 0,
            "pane_width": 120,
            "pane_height": 40,
        }

        def fake_current_info(target: str | None = None) -> dict[str, object] | None:
            return codex_pane

        split_results = [
            mock.Mock(stdout=tmux_control.FIELD_SEP.join(["session", "@1", "%3"])),
            mock.Mock(stdout=tmux_control.FIELD_SEP.join(["session", "@1", "%2"])),
            mock.Mock(stdout=""),
        ]
        with mock.patch.object(tmux_control, "inside_tmux", return_value=True):
            with mock.patch.object(tmux_control, "current_info", side_effect=fake_current_info):
                with mock.patch.object(tmux_control, "current_window_target", return_value="session:0"):
                    with mock.patch.object(tmux_control, "panes_for_target", return_value=[codex_pane]):
                        with mock.patch.object(tmux_control, "idle_shell_check", return_value={"ok": True}):
                            with mock.patch.object(tmux_control, "run_tmux", side_effect=split_results) as run_tmux:
                                result = tmux_control.manager_layout(Path("/tmp/work"))

        split_calls = [call.args[0] for call in run_tmux.call_args_list if call.args[0][0] == "split-window"]
        self.assertEqual(len(split_calls), 2)
        self.assertIn("-h", split_calls[0])
        self.assertIn("-f", split_calls[0])
        self.assertIn("%1", split_calls[0])
        self.assertIn("-v", split_calls[1])
        self.assertNotIn("-f", split_calls[1])
        self.assertIn("20", split_calls[1])
        self.assertIn("%1", split_calls[1])
        self.assertEqual(result["worker_pane_id"], "%3")
        self.assertEqual(result["manager_pane_id"], "%2")
        self.assertFalse(result["worker_reused"])
        self.assertFalse(result["manager_reused"])

    def test_manager_start_reuses_record_and_does_not_send_renderer_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            request_path = tmux_manager.write_command_request(paths, "manager-one", "job-one", "echo old")
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=tmux_manager.build_pending_job("job-one", request_path, str(workspace)),
                notify={"mode": "none"},
                workspace=str(workspace),
                state_dir=str(paths["root"]),
            )
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record["job_ids"] = ["job-one"]
            tmux_manager.write_manager_record(paths, record)
            args = argparse.Namespace(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                notify="none",
                thread_id=None,
                endpoint=None,
                cwd=None,
                workspace=str(workspace),
                state_dir=None,
                poll_seconds=0.5,
            )
            layout = {
                "session_name": "session",
                "window_id": "@1",
                "manager_window_id": "@1",
                "worker_pane_id": "%3",
                "manager_pane_id": "%2",
                "manager_reused": True,
                "worker_reused": True,
                "target": "session:0",
                "cwd": str(workspace),
                "attach_command": None,
                "tmux_tmpdir": None,
            }

            with mock.patch.object(tmux_control, "manager_layout", return_value=layout):
                with mock.patch.object(tmux_control, "send") as send:
                    result = tmux_control.manager_start(args)

            send.assert_not_called()
            self.assertTrue(result["started"])
            loaded, error = tmux_manager.read_manager_record(paths, "manager-one")
            self.assertIsNone(error)
            assert loaded is not None
            self.assertEqual(loaded["job_ids"], ["job-one"])
            self.assertEqual(loaded["pending_job"]["job_id"], "job-two")

    def test_manager_start_queues_to_existing_live_manager_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            request_path = tmux_manager.write_command_request(paths, "manager-one", "job-one", "echo old")
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%2",
                worker_pane_id="%3",
                pending_job=tmux_manager.build_pending_job("job-one", request_path, str(workspace)),
                notify={"mode": "none"},
                workspace=str(workspace),
                state_dir=str(paths["root"]),
            )
            record["pending_job"] = None
            record["status"] = "waiting_for_codex"
            record["manager_pid"] = 424242
            tmux_manager.write_manager_record(paths, record)
            args = argparse.Namespace(
                manager_id="manager-one",
                job_id="job-two",
                command_text="echo next",
                command_file=None,
                notify="none",
                thread_id=None,
                endpoint=None,
                cwd=None,
                workspace=str(workspace),
                state_dir=None,
                poll_seconds=0.5,
            )
            layout = {
                "session_name": "session",
                "window_id": "@1",
                "manager_window_id": "@1",
                "worker_pane_id": "%3",
                "manager_pane_id": "%2",
                "manager_reused": True,
                "worker_reused": True,
                "target": "session:0",
                "cwd": str(workspace),
                "attach_command": None,
                "tmux_tmpdir": None,
            }

            with mock.patch.object(tmux_control, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control, "layout_from_existing_manager_record", return_value=layout):
                    with mock.patch.object(tmux_control, "manager_layout") as manager_layout:
                        result = tmux_control.manager_start(args)

            manager_layout.assert_not_called()
            self.assertTrue(result["started"])
            self.assertTrue(result["queued_on_existing_manager"])
            self.assertEqual(result["start_process_mode"], "existing")
            loaded, error = tmux_manager.read_manager_record(paths, "manager-one")
            self.assertIsNone(error)
            assert loaded is not None
            self.assertEqual(loaded["manager_pid"], 424242)
            self.assertEqual(loaded["manager_process_mode"], "foreground")
            self.assertEqual(loaded["pending_job"]["job_id"], "job-two")

    def test_main_start_existing_manager_does_not_enter_dashboard_loop(self) -> None:
        result = {
            "manager_id": "manager-one",
            "started": True,
            "workspace": "/tmp/workspace",
            "state_dir": "/tmp/workspace/.codex/tmux-skills",
            "start_process_mode": "existing",
        }
        argv = ["tmux_control.py", "manager", "start", "--job-id", "job-two", "--command", "echo next", "--notify", "none"]

        with mock.patch.object(tmux_control.sys, "argv", argv):
            with mock.patch.object(tmux_control, "manager_start", return_value=result):
                with mock.patch.object(tmux_control.tmux_manager, "dashboard_loop") as loop:
                    with mock.patch.object(tmux_control.sys, "stdout", io.StringIO()):
                        tmux_control.main()

        loop.assert_not_called()

    def test_bridge_register_dispatches_to_tmux_bridge_without_delivery(self) -> None:
        result = {"bridge_id": "bridge-thr-test", "registered": True}
        argv = [
            "tmux_control.py",
            "bridge",
            "register",
            "--thread-id",
            "thr_test",
            "--endpoint",
            "unix:///tmp/app.sock",
            "--workspace",
            "/tmp/workspace",
        ]
        with mock.patch.object(tmux_control.sys, "argv", argv):
            with mock.patch.object(tmux_bridge, "register_bridge", return_value=result) as register:
                with mock.patch.object(tmux_control.sys, "stdout", io.StringIO()) as stdout:
                    tmux_control.main()

        register.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue())["bridge_id"], "bridge-thr-test")

    def test_idle_shell_check_returns_structured_failure_when_capture_fails(self) -> None:
        pane_info = {
            "pane_id": "%1",
            "current_command": "bash",
            "pane_pid": "123",
        }
        with mock.patch.object(tmux_control, "current_info", return_value=pane_info):
            with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
                with mock.patch.object(tmux_control, "capture_text", side_effect=RuntimeError("pane disappeared")):
                    result = tmux_control.idle_shell_check("%1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["pane_id"], "%1")
        self.assertIn("could not capture pane output", result["reason"])

    def test_idle_shell_check_returns_structured_failure_when_capture_exits(self) -> None:
        pane_info = {
            "pane_id": "%1",
            "current_command": "bash",
            "pane_pid": "123",
        }
        with mock.patch.object(tmux_control, "current_info", return_value=pane_info):
            with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
                with mock.patch.object(tmux_control, "capture_text", side_effect=SystemExit(1)):
                    result = tmux_control.idle_shell_check("%1")

        self.assertFalse(result["ok"])
        self.assertIn("could not capture pane output", result["reason"])
        self.assertIn("SystemExit(1)", result["reason"])

    def test_spawn_parser_rejects_invalid_percent(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["spawn", "--percent", "0"],
            ["spawn", "--percent", "-1"],
            ["spawn", "--percent", "100"],
            ["spawn", "--percent", "abc"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

        args = parser.parse_args(["spawn", "--percent", "50"])
        self.assertEqual(args.percent, 50)

    def test_run_writes_command_file_and_sends_wrapper_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent: dict[str, str] = {}

            def fake_send(args: argparse.Namespace) -> dict[str, object]:
                sent["text"] = args.command_text
                return {"sent_to_pane": True}

            args = argparse.Namespace(
                pane="%1",
                command_text="printf 'quoted value'\necho 한글",
                command_file=None,
                job_id="job-one",
                name="test",
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send", side_effect=fake_send):
                result = tmux_control.run_job(args)

            command_file = Path(result["command_path"])
            self.assertEqual(command_file.read_text(encoding="utf-8"), "printf 'quoted value'\necho 한글\n")
            self.assertIn("tmux_job.py", sent["text"])
            self.assertNotIn("quoted value", sent["text"])
            self.assertEqual(oct(command_file.stat().st_mode & 0o777), "0o600")

            status, error = tmux_state.read_json(Path(result["status_path"]))
            self.assertIsNone(error)
            self.assertEqual(status["status"], "pending")

    def test_run_send_failure_finalizes_status_and_cancels_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%0",
                command_text="echo should-not-run",
                command_file=None,
                job_id="send failure",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                next_instruction="inspect the failed job",
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": False, "reason": "pane is busy"}):
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "send-failure"))
            state = tmux_state.load_task_state(paths)

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 1)
        self.assertIn("pane is busy", status["last_output"])
        self.assertEqual(len(state["tasks"]), 1)
        task = state["tasks"][0]
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["blocked_reason"], "job command was not sent to pane")
        self.assertNotEqual(task["effective_status"], "ready")

    def test_run_send_failure_keeps_failed_follow_up_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%0",
                command_text="echo should-not-run",
                command_file=None,
                job_id="send failure follow up",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                next_instruction="inspect the failed send",
                next_instruction_file=None,
                next_on="failed",
            )
            with mock.patch.object(tmux_control, "send", return_value={"sent_to_pane": False, "reason": "pane is busy"}):
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "send-failure-follow-up"))
            state = tmux_state.load_task_state(paths)

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(len(state["tasks"]), 1)
        task = state["tasks"][0]
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["effective_status"], "ready")
        self.assertEqual(task["matched_status"]["id"], "send-failure-follow-up")

    def test_run_send_exception_finalizes_status_and_cancels_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%999999",
                command_text="echo should-not-run",
                command_file=None,
                job_id="send exception",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                next_instruction="inspect the failed job",
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send", side_effect=SystemExit(1)):
                with self.assertRaises(SystemExit):
                    tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "send-exception"))
            state = tmux_state.load_task_state(paths)

        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 1)
        self.assertIn("command was not sent to pane", status["last_output"])
        self.assertEqual(len(state["tasks"]), 1)
        task = state["tasks"][0]
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["blocked_reason"], "job command was not sent to pane")
        self.assertNotEqual(task["effective_status"], "ready")

    def test_run_missing_command_file_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text=None,
                command_file=str(Path(tmp) / "missing.sh"),
                job_id="missing run command",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "missing-run-command"))

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not read command file", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["event_id"])

    def test_run_rejects_blank_job_id_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id=" ",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, _error = tmux_state.read_json(tmux_state.status_path(paths, "job"))

        send.assert_not_called()
        self.assertIn("run requires nonblank --job-id when provided", stderr.getvalue())
        self.assertIsNone(status)

    def test_run_rejects_blank_command_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text=" \n\t ",
                command_file=None,
                job_id="blank run command",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "blank-run-command"))

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "command is blank")
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "command is blank")

    def test_run_rejects_blank_command_file_path_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text=None,
                command_file="",
                job_id="blank command file path",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "blank-command-file-path"))

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "command file path is blank")
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "command file path is blank")

    def test_run_command_file_write_failure_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id="unwritable run command",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                with mock.patch.object(tmux_control, "write_command_file", side_effect=OSError("disk full")):
                    result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "unwritable-run-command"))

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not write command file", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not write command file", status["last_output"])

    def test_run_missing_next_instruction_file_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id="missing next instruction",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=str(Path(tmp) / "missing-next.txt"),
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "missing-next-instruction"))
            tasks, task_errors = tmux_state.load_tasks(paths["root"])

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not read next instruction", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["event_id"])
        self.assertEqual(tasks, [])
        self.assertEqual(task_errors, [])

    def test_run_rejects_multiple_next_instruction_sources_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instruction_file = Path(tmp) / "next.txt"
            instruction_file.write_text("Inspect from file\n", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id="duplicate next instruction",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction="Inspect inline",
                next_instruction_file=str(instruction_file),
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "duplicate-next-instruction"))
            tasks, task_errors = tmux_state.load_tasks(paths["root"])

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not read next instruction", result["reason"])
        self.assertIn("provide only one", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(tasks, [])
        self.assertEqual(task_errors, [])

    def test_run_rejects_blank_next_instruction_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instruction_file = Path(tmp) / "next.txt"
            instruction_file.write_text(" \n\t ", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id="blank next instruction",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file=str(instruction_file),
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "blank-next-instruction"))
            tasks, task_errors = tmux_state.load_tasks(paths["root"])

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("next instruction is blank", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(tasks, [])
        self.assertEqual(task_errors, [])

    def test_run_rejects_blank_next_instruction_file_path_before_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                command_text="echo should-not-run",
                command_file=None,
                job_id="blank next instruction file path",
                name=None,
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction=None,
                next_instruction_file="",
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send") as send:
                result = tmux_control.run_job(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "blank-next-instruction-file-path"))
            tasks, task_errors = tmux_state.load_tasks(paths["root"])

        send.assert_not_called()
        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("could not read next instruction", result["reason"])
        self.assertIn("file path is blank", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(tasks, [])
        self.assertEqual(task_errors, [])

    def test_monitor_rejects_no_condition_in_main_parser_contract(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(["monitor", "--pane", "%1"])
        self.assertIsNone(args.match_regex)
        self.assertFalse(args.idle_shell)
        self.assertIsNone(args.timeout_seconds)
        with mock.patch.object(tmux_control.sys, "argv", ["tmux_control.py", "monitor", "--pane", "%1"]):
            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    tmux_control.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("monitor requires --match-regex, --idle-shell, or --timeout-seconds", stderr.getvalue())

    def test_watch_status_and_cancel_require_job_id_in_main(self) -> None:
        for action in ("status", "cancel"):
            with self.subTest(action=action):
                argv = ["tmux_control.py", "watch", action]
                with mock.patch.object(tmux_control.sys, "argv", argv):
                    with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            tmux_control.main()

                self.assertEqual(raised.exception.code, 1)
                self.assertIn(f"watch {action} requires --job-id", stderr.getvalue())

    def test_watch_start_requires_job_id_and_pane_in_main(self) -> None:
        invalid_argvs = [
            ["tmux_control.py", "watch", "--pane", "%1"],
            ["tmux_control.py", "watch", "--job-id", "watch"],
        ]
        for argv in invalid_argvs:
            with self.subTest(argv=argv):
                with mock.patch.object(tmux_control.sys, "argv", argv):
                    with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            tmux_control.main()

                self.assertEqual(raised.exception.code, 1)
                self.assertIn("watch start requires --job-id and --pane", stderr.getvalue())

    def test_watch_start_rejects_blank_status_file_before_worker_start(self) -> None:
        argv = ["tmux_control.py", "watch", "--job-id", "watch", "--pane", "%1", "--status-file", " \n\t "]
        with mock.patch.object(tmux_control.sys, "argv", argv):
            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with mock.patch.object(tmux_control, "start_managed_worker") as start_worker:
                    with self.assertRaises(SystemExit) as raised:
                        tmux_control.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("watch requires nonblank --status-file when provided", stderr.getvalue())
        start_worker.assert_not_called()

    def test_watch_low_token_requires_status_file(self) -> None:
        argv = ["tmux_control.py", "watch", "--job-id", "watch", "--pane", "%1", "--low-token"]
        with mock.patch.object(tmux_control.sys, "argv", argv):
            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with mock.patch.object(tmux_control, "start_managed_worker") as start_worker:
                    with self.assertRaises(SystemExit) as raised:
                        tmux_control.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("watch --low-token requires --status-file", stderr.getvalue())
        start_worker.assert_not_called()

    def test_low_token_watch_does_not_capture_pane_when_status_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="low-token-watch",
                pane="%1",
                workspace=tmp,
                state_dir=None,
                name=None,
                interval=1.0,
                capture_lines=80,
                status_lines=10,
                status_max_chars=1200,
                status_file="missing.status",
                timeout_seconds=0,
                low_token=True,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text", side_effect=AssertionError("capture should not run")):
                exit_code = tmux_queue.run_watch(args)
            paths = tmux_state.state_paths(tmp)
            status = tmux_state.read_json(tmux_state.status_path(paths, "low-token-watch"))[0]

        self.assertEqual(exit_code, 1)
        assert status is not None
        self.assertIn("status file not found", status["last_output"])
        self.assertTrue(status["low_token"])

    def test_queue_after_status_requires_require_row_in_main(self) -> None:
        argv = [
            "tmux_control.py",
            "queue-after-status",
            "--job-id",
            "queue",
            "--pane",
            "%1",
            "--command",
            "echo ok",
            "--status-file",
            "status.tsv",
        ]
        with mock.patch.object(tmux_control.sys, "argv", argv):
            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    tmux_control.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("queue-after-status requires at least one --require-row", stderr.getvalue())

    def test_job_gc_requires_stale_in_main(self) -> None:
        with mock.patch.object(tmux_control.sys, "argv", ["tmux_control.py", "job", "gc"]):
            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    tmux_control.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("job gc requires --stale", stderr.getvalue())

    def test_workflow_reference_commands_match_public_parser(self) -> None:
        parser = tmux_control.build_parser()
        command_lines = [
            line
            for line in WORKFLOWS_DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("python scripts/tmux_control.py ")
        ]

        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(line=line):
                argv = shlex.split(line)[2:]
                parser.parse_args(argv)

    def test_workflow_reference_matches_ready_task_order_contract(self) -> None:
        text = WORKFLOWS_DOC.read_text(encoding="utf-8")

        self.assertIn("next ready task", text)
        self.assertNotIn("newest ready task", text)

    def test_workflows_feature_doc_commands_match_public_parser(self) -> None:
        parser = tmux_control.build_parser()
        command_lines = control_commands_from_bash_blocks(WORKFLOWS_FEATURES_DOC)

        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(line=line):
                argv = shlex.split(line)[2:]
                parser.parse_args(argv)

    def test_skill_quick_reference_lists_public_control_commands(self) -> None:
        parser = tmux_control.build_parser()
        command_lines = control_commands_from_bash_blocks(SKILL_DOC)
        command_names = {shlex.split(line)[2] for line in command_lines}

        self.assertTrue(command_lines)
        self.assertEqual(command_names, top_level_command_names(parser))

    def test_managed_worker_doc_commands_match_public_parser(self) -> None:
        parser = tmux_control.build_parser()
        command_lines = [
            line
            for line in MANAGED_WORKERS_DOC.read_text(encoding="utf-8").splitlines()
            if line.startswith("python3 scripts/tmux_control.py ")
        ]

        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(line=line):
                argv = shlex.split(line)[2:]
                parser.parse_args(argv)

    def test_send_parser_requires_exactly_one_enter_mode(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["send", "--pane", "%1", "--command", "echo ok"],
            ["send", "--pane", "%1", "--command", "echo ok", "--enter", "--no-enter"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

        enter = parser.parse_args(["send", "--pane", "%1", "--command", "echo ok", "--enter"])
        staged = parser.parse_args(["send", "--pane", "%1", "--command", "echo ok", "--no-enter"])
        self.assertTrue(enter.enter)
        self.assertFalse(enter.no_enter)
        self.assertFalse(staged.enter)
        self.assertTrue(staged.no_enter)

    def test_run_parser_requires_exactly_one_command_source(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["run", "--pane", "%1"],
            ["run", "--pane", "%1", "--command", "echo ok", "--command-file", "cmd.sh"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

        inline = parser.parse_args(["run", "--pane", "%1", "--command", "echo ok"])
        from_file = parser.parse_args(["run", "--pane", "%1", "--command-file", "cmd.sh"])
        self.assertEqual(inline.command_text, "echo ok")
        self.assertIsNone(inline.command_file)
        self.assertIsNone(from_file.command_text)
        self.assertEqual(from_file.command_file, "cmd.sh")

    def test_run_parser_rejects_multiple_next_instruction_sources(self) -> None:
        parser = tmux_control.build_parser()
        with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "run",
                        "--pane",
                        "%1",
                        "--command",
                        "echo ok",
                        "--next-instruction",
                        "Inspect inline",
                        "--next-instruction-file",
                        "next.txt",
                    ]
                )

    def test_queue_parser_requires_exactly_one_command_source(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["queue-after-idle", "--job-id", "queue", "--pane", "%1"],
            [
                "queue-after-idle",
                "--job-id",
                "queue",
                "--pane",
                "%1",
                "--command",
                "echo ok",
                "--command-file",
                "cmd.sh",
            ],
            [
                "queue-after-status",
                "--job-id",
                "queue",
                "--pane",
                "%1",
                "--status-file",
                "status.tsv",
                "--require-row",
                "state=done",
            ],
            [
                "queue-after-status",
                "--job-id",
                "queue",
                "--pane",
                "%1",
                "--command",
                "echo ok",
                "--command-file",
                "cmd.sh",
                "--status-file",
                "status.tsv",
                "--require-row",
                "state=done",
            ],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_public_parser_rejects_nonpositive_polling_intervals(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["monitor", "--pane", "%1", "--timeout-seconds", "1", "--poll-seconds", "0"],
            ["monitor", "--pane", "%1", "--timeout-seconds", "nan"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--interval", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--interval", "inf"],
            ["queue-after-idle", "--job-id", "queue", "--pane", "%1", "--command", "echo ok", "--poll-seconds", "0"],
            [
                "queue-after-status",
                "--job-id",
                "queue",
                "--pane",
                "%1",
                "--command",
                "echo ok",
                "--status-file",
                "status.tsv",
                "--require-row",
                "state=done",
                "--interval",
                "-1",
            ],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_public_parser_rejects_nonpositive_line_counts(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["capture", "--pane", "%1", "--lines", "0"],
            ["capture", "--pane", "%1", "--lines", "-1"],
            ["monitor", "--pane", "%1", "--timeout-seconds", "1", "--lines", "0"],
            ["monitor", "--pane", "%1", "--timeout-seconds", "1", "--status-lines", "0"],
            ["monitor", "--pane", "%1", "--timeout-seconds", "1", "--status-max-chars", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--capture-lines", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--status-lines", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--status-max-chars", "0"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_resolve_parser_rejects_invalid_pane_numbers(self) -> None:
        parser = tmux_control.build_parser()
        invalid_commands = [
            ["resolve", "--pane-index", "-1"],
            ["resolve", "--ordinal", "0"],
            ["resolve", "--ordinal", "-1"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

        self.assertEqual(parser.parse_args(["resolve", "--pane-index", "0"]).pane_index, 0)
        self.assertEqual(parser.parse_args(["resolve", "--ordinal", "1"]).ordinal, 1)

    def test_resolve_current_window_requires_tmux_before_tmux_call(self) -> None:
        args = argparse.Namespace(target=None, current_window=True, pane_index=None, ordinal=None)
        with mock.patch.object(tmux_control, "inside_tmux", return_value=False):
            with mock.patch.object(tmux_control, "current_window_target") as current_window_target:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit) as raised:
                        tmux_control.resolve(args)

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("--current-window requires running inside tmux", stderr.getvalue())
        current_window_target.assert_not_called()

    def test_monitor_spawns_worker_with_tmux_env(self) -> None:
        class Proc:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                poll_seconds=1.0,
                lines=50,
                status_lines=3,
                status_max_chars=500,
                match_regex=None,
                idle_shell=False,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_control, "inside_tmux", return_value=False):
                with mock.patch.dict(tmux_control.os.environ, {}, clear=True):
                    with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()) as popen:
                        result = tmux_control.monitor(args)

        self.assertEqual(result["pid"], 12345)
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        self.assertIn("--status-lines", argv)
        self.assertIn("3", argv)
        self.assertIn("--status-max-chars", argv)
        self.assertIn("500", argv)
        env = popen.call_args.kwargs.get("env")
        self.assertIsNotNone(env)
        assert env is not None
        self.assertNotIn("TMUX_TMPDIR", env)

    def test_tmux_env_does_not_create_hidden_tmpdir_by_default(self) -> None:
        with mock.patch.dict(tmux_control.os.environ, {}, clear=True):
            env = tmux_control.tmux_env()

        self.assertNotIn("TMUX_TMPDIR", env)
        self.assertNotIn("TMUX_SKILLS_SOCKET", env)

    def test_tmux_env_redirects_internal_codex_socket_to_default_socket(self) -> None:
        hidden = "/var/folders/tmp/codex-tmux-control/tmux-501/default"
        default_socket = "/tmp/tmux-501/default"
        with mock.patch.dict(tmux_control.os.environ, {"TMUX": f"{hidden},123,0"}, clear=True):
            with mock.patch.object(tmux_control.os, "getuid", return_value=501):
                with mock.patch.object(tmux_control, "socket_exists", return_value=True):
                    env = tmux_control.tmux_env()
                    prefix = tmux_control.tmux_command_prefix()

        self.assertEqual(env["TMUX_SKILLS_SOCKET"], default_socket)
        self.assertEqual(prefix, ["tmux", "-S", default_socket])

    def test_monitor_rejects_blank_pane_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane=" \n\t ",
                poll_seconds=1.0,
                lines=50,
                match_regex=None,
                idle_shell=False,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.monitor(args)
            paths = tmux_state.state_paths(tmp)
            status_files = list(paths["status"].glob("*.json")) if paths["status"].exists() else []

        popen.assert_not_called()
        self.assertIn("monitor requires nonblank --pane", stderr.getvalue())
        self.assertEqual(status_files, [])

    def test_monitor_start_failure_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                pane="%1",
                poll_seconds=1.0,
                lines=50,
                match_regex="ERROR",
                idle_shell=False,
                timeout_seconds=5.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", side_effect=OSError("no monitor")):
                result = tmux_control.monitor(args)

            status, error = tmux_state.read_json(Path(result["status_path"]))

        self.assertFalse(result["started"])
        self.assertIn("monitor worker failed to start", result["reason"])
        self.assertIsNone(error)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["kind"], "monitor")
        self.assertTrue(status["event_id"])

    def test_send_strict_preflight_refuses_non_executable_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("echo ok\n", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="./script.sh",
                enter=True,
                require_idle_shell=False,
                strict_preflight=True,
                bash_if_not_executable=False,
                cwd=tmp,
            )

            result = tmux_control.send(args)

        self.assertFalse(result["sent_to_pane"])
        self.assertIn("not executable", result["reason"])

    def test_send_preflight_uses_target_pane_cwd_when_cwd_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("echo ok\n", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="./script.sh",
                enter=True,
                require_idle_shell=False,
                strict_preflight=True,
                bash_if_not_executable=False,
            )
            with mock.patch.object(tmux_control, "current_info", return_value={"current_path": tmp}):
                with mock.patch.object(tmux_control, "run_tmux") as run_tmux:
                    result = tmux_control.send(args)

        self.assertFalse(result["sent_to_pane"])
        self.assertIn("not executable", result["reason"])
        run_tmux.assert_not_called()

    def test_send_can_rewrite_non_executable_script_to_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("echo ok\n", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="./script.sh --flag",
                enter=True,
                require_idle_shell=False,
                strict_preflight=False,
                bash_if_not_executable=True,
                cwd=tmp,
            )
            with mock.patch.object(tmux_control, "run_tmux") as run_tmux:
                result = tmux_control.send(args)

        self.assertTrue(result["sent_to_pane"])
        self.assertEqual(result["command_text"], "bash ./script.sh --flag")
        self.assertEqual(run_tmux.call_args_list[0].args[0], ["send-keys", "-t", "%1", "-l", "bash ./script.sh --flag"])

    def test_script_preflight_bash_rewrite_expands_home_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("echo ok\n", encoding="utf-8")
            with mock.patch.dict(tmux_control.os.environ, {"HOME": tmp}):
                result = tmux_control.script_preflight("~/script.sh --flag")

        self.assertFalse(result["ok"])
        self.assertEqual(result["script_path"], str(script))
        self.assertEqual(result["bash_command"], f"bash {script} --flag")
        self.assertNotIn("'~/script.sh'", result["bash_command"])

    def test_send_warns_but_sends_non_executable_script_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.sh"
            script.write_text("echo ok\n", encoding="utf-8")
            args = argparse.Namespace(
                pane="%1",
                command_text="./script.sh",
                enter=True,
                require_idle_shell=False,
                strict_preflight=False,
                bash_if_not_executable=False,
                cwd=tmp,
            )
            with mock.patch.object(tmux_control, "run_tmux"):
                result = tmux_control.send(args)

        self.assertTrue(result["sent_to_pane"])
        self.assertEqual(result["preflight"]["action"], "warn-only")

    def test_send_no_enter_stages_command_without_enter(self) -> None:
        args = argparse.Namespace(
            pane="%1",
            command_text="echo staged",
            enter=False,
            require_idle_shell=False,
            strict_preflight=False,
            bash_if_not_executable=False,
        )
        with mock.patch.object(tmux_control, "run_tmux") as run_tmux:
            result = tmux_control.send(args)

        self.assertTrue(result["sent_to_pane"])
        self.assertFalse(result["entered"])
        self.assertEqual(run_tmux.call_args_list, [mock.call(["send-keys", "-t", "%1", "-l", "echo staged"])])

    def test_send_enter_submits_command_with_enter(self) -> None:
        args = argparse.Namespace(
            pane="%1",
            command_text="echo run",
            enter=True,
            require_idle_shell=False,
            strict_preflight=False,
            bash_if_not_executable=False,
        )
        with mock.patch.object(tmux_control, "run_tmux") as run_tmux:
            result = tmux_control.send(args)

        self.assertTrue(result["sent_to_pane"])
        self.assertTrue(result["entered"])
        self.assertEqual(
            run_tmux.call_args_list,
            [
                mock.call(["send-keys", "-t", "%1", "-l", "echo run"]),
                mock.call(["send-keys", "-t", "%1", "Enter"]),
            ],
        )

    def test_queue_after_idle_starts_managed_worker_and_writes_record(self) -> None:
        class Proc:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue one",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()) as popen:
                result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, error = tmux_state.read_json(tmux_state.job_path(paths, "queue-one"))
            command = tmux_state.command_path(paths, "queue-one").read_text(encoding="utf-8")

        self.assertIsNone(error)
        self.assertTrue(result["started"])
        self.assertEqual(record["kind"], "queue-after-idle")
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["status"], "waiting")
        self.assertIn("dedupe_key", record)
        self.assertEqual(command, "echo ok\n")
        self.assertIn("tmux_queue.py", " ".join(popen.call_args.args[0]))

    def test_watch_forwards_status_tail_options_to_worker(self) -> None:
        class Proc:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                watch_action="start",
                job_id="watch",
                pane="%1",
                interval=1.0,
                capture_lines=80,
                status_lines=3,
                status_max_chars=500,
                status_file=None,
                timeout_seconds=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()) as popen:
                result = tmux_control.watch(args)

            paths = tmux_state.state_paths(tmp)
            record, error = tmux_state.read_json(tmux_state.job_path(paths, "watch"))

        self.assertIsNone(error)
        self.assertTrue(result["started"])
        argv = popen.call_args.args[0]
        self.assertIn("--status-lines", argv)
        self.assertIn("3", argv)
        self.assertIn("--status-max-chars", argv)
        self.assertIn("500", argv)
        assert record is not None
        self.assertNotIn("status_lines", record["dedupe_payload"])
        self.assertNotIn("status_max_chars", record["dedupe_payload"])

    def test_write_managed_job_record_normalizes_kind_and_status_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)

            record = tmux_control.write_managed_job_record(
                paths,
                job_id="watch",
                kind=" Watch ",
                pid=12345,
                pane_id="%1",
                status=" Running ",
                extra={"kind": "wrong", "status": "failed", "pid_matches": True, "pid_running": True, "stale": False},
            )
            stored = tmux_state.read_json(tmux_state.job_path(paths, "watch"))[0]

        self.assertEqual(record["kind"], "watch")
        self.assertEqual(record["status"], "running")
        self.assertNotIn("pid_matches", record)
        self.assertNotIn("pid_running", record)
        self.assertNotIn("stale", record)
        assert stored is not None
        self.assertEqual(stored["kind"], "watch")
        self.assertEqual(stored["status"], "running")
        self.assertNotIn("pid_matches", stored)
        self.assertNotIn("pid_running", stored)
        self.assertNotIn("stale", stored)

    def test_write_managed_start_failure_preserves_failure_fields_after_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)

            record = tmux_control.write_managed_start_failure(
                paths,
                job_id="start-failed",
                kind=" Queue-After-Idle ",
                pane_id="%1",
                name=None,
                reason="worker failed to start",
                extra={
                    "kind": "wrong",
                    "status": "running",
                    "exit_code": 0,
                    "last_output": "wrong output",
                    "error": "wrong error",
                },
            )
            status = tmux_state.read_json(tmux_state.status_path(paths, "start-failed"))[0]

        self.assertEqual(record["kind"], "queue-after-idle")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "worker failed to start")
        assert status is not None
        self.assertEqual(status["kind"], "queue-after-idle")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["last_output"], "worker failed to start")
        self.assertEqual(status["error"], "worker failed to start")

    def test_queue_start_popen_failure_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue failed",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", side_effect=OSError("no spawn")):
                result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "queue-failed"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "queue-failed"))

        self.assertFalse(result["started"])
        self.assertIn("managed worker failed to start", result["reason"])
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["pid"], 0)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["event_id"])

    def test_queue_start_returns_failure_when_starting_record_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue start unwritable",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control, "write_managed_job_record", side_effect=OSError("disk full")):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record_path = tmux_state.job_path(paths, "queue-start-unwritable")

        self.assertFalse(result["started"])
        self.assertIn("state update failed before start", result["reason"])
        self.assertIsNone(result["record"])
        popen.assert_not_called()
        self.assertFalse(record_path.exists())

    def test_queue_start_command_file_write_failure_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue command unwritable",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control, "write_command_file", side_effect=OSError("disk full")):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "queue-command-unwritable"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "queue-command-unwritable"))

        self.assertFalse(result["started"])
        self.assertIn("could not write managed worker command file", result["reason"])
        popen.assert_not_called()
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], result["reason"])
        self.assertEqual(result["record"], record)

    def test_queue_start_removes_starting_record_when_popen_failure_record_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue spawn unrecoverable",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            real_write = tmux_control.write_managed_job_record
            calls: list[str] = []

            def flaky_write(*args: object, **kwargs: object) -> dict[str, object]:
                calls.append(str(kwargs.get("status")))
                if len(calls) > 1:
                    raise OSError("disk full")
                return real_write(*args, **kwargs)

            with mock.patch.object(tmux_control.subprocess, "Popen", side_effect=OSError("no spawn")):
                with mock.patch.object(tmux_control, "write_managed_job_record", side_effect=flaky_write):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record_path = tmux_state.job_path(paths, "queue-spawn-unrecoverable")

        self.assertFalse(result["started"])
        self.assertIn("managed worker failed to start", result["reason"])
        self.assertIsNone(result["record"])
        self.assertEqual(calls, ["starting", "failed"])
        self.assertFalse(record_path.exists())

    def test_queue_start_stops_worker_when_final_record_update_fails(self) -> None:
        class Proc:
            pid = 12345

            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return 0 if self.terminated else None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                self.terminated = True
                return 0

            def kill(self) -> None:
                self.killed = True
                self.terminated = True

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue update failed",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            proc = Proc()
            real_write = tmux_control.write_managed_job_record
            calls: list[str] = []

            def flaky_write(*args: object, **kwargs: object) -> dict[str, object]:
                calls.append(str(kwargs.get("status")))
                if len(calls) == 2:
                    raise OSError("disk full")
                return real_write(*args, **kwargs)

            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=proc):
                with mock.patch.object(tmux_control, "write_managed_job_record", side_effect=flaky_write):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "queue-update-failed"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "queue-update-failed"))

        self.assertFalse(result["started"])
        self.assertIn("state update failed after start", result["reason"])
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)
        self.assertEqual(calls, ["starting", "waiting", "failed"])
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["pid"], 0)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], result["reason"])
        self.assertEqual(result["record"], record)

    def test_queue_start_removes_starting_record_when_failure_record_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue unrecoverable update",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            proc = mock.Mock()
            proc.pid = 12345
            proc.poll.return_value = None
            real_write = tmux_control.write_managed_job_record
            calls: list[str] = []

            def flaky_write(*args: object, **kwargs: object) -> dict[str, object]:
                calls.append(str(kwargs.get("status")))
                if len(calls) > 1:
                    raise OSError("disk full")
                return real_write(*args, **kwargs)

            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=proc):
                with mock.patch.object(tmux_control, "write_managed_job_record", side_effect=flaky_write):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record_path = tmux_state.job_path(paths, "queue-unrecoverable-update")

        self.assertFalse(result["started"])
        self.assertIn("state update failed after start", result["reason"])
        self.assertIsNone(result["record"])
        self.assertEqual(calls, ["starting", "waiting", "failed"])
        proc.terminate.assert_called_once()
        self.assertFalse(record_path.exists())

    def test_queue_start_missing_command_file_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="missing command",
                pane="%1",
                command_text=None,
                command_file=str(Path(tmp) / "missing.sh"),
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(
                    tmux_control.tmux_state,
                    "process_command_line",
                    return_value="python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id active-job --pane %1",
                ):
                    result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "missing-command"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "missing-command"))

        self.assertFalse(result["started"])
        self.assertIn("could not read managed worker command file", result["reason"])
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["event_id"])

    def test_queue_start_rejects_blank_job_id_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id=" ",
                pane="%1",
                command_text="echo ok",
                command_file=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "job"))

        popen.assert_not_called()
        self.assertIn("managed worker requires nonblank --job-id", stderr.getvalue())
        self.assertIsNone(record)

    def test_queue_start_rejects_blank_pane_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="blank pane",
                pane=" ",
                command_text="echo ok",
                command_file=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "blank-pane"))

        popen.assert_not_called()
        self.assertIn("managed worker requires nonblank --pane", stderr.getvalue())
        self.assertIsNone(record)

    def test_start_managed_worker_rejects_blank_identity_before_state_write(self) -> None:
        cases = [
            ({"job_id": "", "pane": "%1"}, "managed worker requires nonblank --job-id"),
            ({"job_id": "queue", "pane": "\t"}, "managed worker requires nonblank --pane"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as tmp:
                    base = {
                        "job_id": "queue",
                        "pane": "%1",
                        "command_text": "echo ok",
                        "command_file": None,
                        "workspace": tmp,
                        "state_dir": None,
                        "name": None,
                        "poll_seconds": 1.0,
                        "timeout_seconds": None,
                        "strict_preflight": False,
                        "bash_if_not_executable": False,
                        "replace": False,
                        "allow_duplicate": False,
                        "owner": None,
                    }
                    base.update(overrides)
                    args = argparse.Namespace(**base)
                    with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                        with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                            with self.assertRaises(SystemExit):
                                tmux_control.start_managed_worker(args, "queue-after-idle", "queue-after-idle")
                    paths = tmux_state.state_paths(tmp)
                    job_files = list(paths["jobs"].glob("*.json")) if paths["jobs"].exists() else []
                    status_files = list(paths["status"].glob("*.json")) if paths["status"].exists() else []

                popen.assert_not_called()
                self.assertIn(message, stderr.getvalue())
                self.assertEqual(job_files, [])
                self.assertEqual(status_files, [])

    def test_queue_start_rejects_blank_command_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="blank queued command",
                pane="%1",
                command_text=" \n\t ",
                command_file=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "blank-queued-command"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "blank-queued-command"))

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "command is blank")
        popen.assert_not_called()
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "command is blank")

    def test_queue_start_rejects_blank_command_file_path_before_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="blank queued command file",
                pane="%1",
                command_text=None,
                command_file="",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "blank-queued-command-file"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "blank-queued-command-file"))

        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "command file path is blank")
        popen.assert_not_called()
        self.assertIsNone(record_error)
        self.assertIsNone(status_error)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "command file path is blank")

    def test_queue_after_idle_accepts_command_file_from_cli_parser(self) -> None:
        class Proc:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "queued.sh"
            source.write_text("echo from-file\n", encoding="utf-8")
            parser = tmux_control.build_parser()
            args = parser.parse_args(
                [
                    "queue-after-idle",
                    "--job-id",
                    "file job",
                    "--pane",
                    "%1",
                    "--command-file",
                    str(source),
                    "--workspace",
                    tmp,
                ]
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()) as popen:
                result = tmux_control.queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            copied = tmux_state.command_path(paths, "file-job").read_text(encoding="utf-8")

        self.assertTrue(result["started"])
        self.assertEqual(copied, "echo from-file\n")
        argv = popen.call_args.args[0]
        self.assertIn("--command-file", argv)
        self.assertIn(str(tmux_state.command_path(paths, "file-job")), argv)

    def test_missing_command_file_does_not_overwrite_active_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            existing = {
                "job_id": "active-job",
                "kind": "queue-after-idle",
                "status": "waiting_pane_idle",
                "pid": 12345,
                "pane_id": "%1",
                "dedupe_key": "existing-key",
                "heartbeat_at": tmux_state.utc_now(),
                "updated_at": tmux_state.utc_now(),
                "check_interval_seconds": 1,
            }
            tmux_state.atomic_write_json(tmux_state.job_path(paths, "active-job"), existing)
            args = argparse.Namespace(
                job_id="active job",
                pane="%1",
                command_text=None,
                command_file=str(Path(tmp) / "missing.sh"),
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(
                    tmux_control.tmux_state,
                    "process_command_line",
                    return_value="python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id active-job --pane %1",
                ):
                    result = tmux_control.queue_after_idle(args)

            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "active-job"))
            status, status_error = tmux_state.read_json(tmux_state.status_path(paths, "active-job"))

        self.assertFalse(result["started"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["existing_job_id"], "active-job")
        self.assertIsNone(record_error)
        assert record is not None
        self.assertEqual(record["status"], "waiting_pane_idle")
        self.assertEqual(record["dedupe_key"], "existing-key")
        self.assertIsNone(status)
        self.assertIsNone(status_error)

    def test_replace_sanitizes_legacy_existing_job_id_before_pid_match(self) -> None:
        class Proc:
            pid = 54321

        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pid_matches": True,
                    "pid_running": True,
                    "pane_id": "%1",
                    "stale": False,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            args = argparse.Namespace(
                job_id="job with space",
                pane="%1",
                command_text="echo replacement",
                command_file=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=True,
                allow_duplicate=False,
                owner=None,
            )
            command = "python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id job-with-space --pane %1"
            with mock.patch.object(tmux_control, "pid_is_running", side_effect=[True, True, False, False, False]):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_control.tmux_state, "process_command_line", return_value=command):
                        with mock.patch.object(tmux_control.os, "kill") as kill:
                            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()):
                                result = tmux_control.queue_after_idle(args)

        self.assertTrue(result["started"])
        self.assertEqual(result["job_id"], "job-with-space")
        kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_replace_rejects_foreign_live_pid_before_stale_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "foreign"),
                {
                    "job_id": "foreign",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            args = argparse.Namespace(
                job_id="foreign",
                pane="%1",
                command_text="echo replacement",
                command_file=None,
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=True,
                allow_duplicate=False,
                owner=None,
            )

            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control.tmux_state, "process_command_line", return_value="python other.py"):
                    with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                        result = tmux_control.queue_after_idle(args)

        self.assertFalse(result["started"])
        self.assertIn("no longer looks like this tmux-skills worker", result["reason"])
        popen.assert_not_called()

    def test_queue_after_status_requires_required_row_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="status no rows",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                status_file="status.tsv",
                require_row=[],
                fail_row=[],
                poll_seconds=1.0,
                timeout_seconds=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "status-no-rows"))

        popen.assert_not_called()
        self.assertIsNone(record)

    def test_queue_after_status_rejects_blank_status_file_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="status blank file",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                status_file="",
                require_row=["status=done"],
                fail_row=[],
                poll_seconds=1.0,
                timeout_seconds=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "status-blank-file"))

        popen.assert_not_called()
        self.assertIn("queue-after-status requires nonblank --status-file", stderr.getvalue())
        self.assertIsNone(record)

    def test_queue_after_status_rejects_blank_row_spec_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="status blank row",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                status_file="status.tsv",
                require_row=[" "],
                fail_row=[],
                poll_seconds=1.0,
                timeout_seconds=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "status-blank-row"))

        popen.assert_not_called()
        self.assertIn("--require-row is blank", stderr.getvalue())
        self.assertIsNone(record)

    def test_queue_after_status_rejects_blank_fail_row_before_starting_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="status blank fail row",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                status_file="status.tsv",
                require_row=["status=done"],
                fail_row=[""],
                poll_seconds=1.0,
                timeout_seconds=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                    with self.assertRaises(SystemExit):
                        tmux_control.queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            record, _error = tmux_state.read_json(tmux_state.job_path(paths, "status-blank-fail-row"))

        popen.assert_not_called()
        self.assertIn("--fail-row is blank", stderr.getvalue())
        self.assertIsNone(record)

    def test_dedupe_key_ignores_owner_name_timeout_and_row_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            args_one = argparse.Namespace(
                pane="%1",
                command_text="echo next\n",
                status_file="status.tsv",
                require_row=["b=2", "a=1"],
                fail_row=["status=failed", "status=error"],
                require_idle_shell=True,
                owner="codex-a",
                name="first",
                timeout_seconds=10,
                poll_seconds=1,
            )
            args_two = argparse.Namespace(
                pane="%1",
                command_text="echo next\n",
                status_file="status.tsv",
                require_row=["a=1", "b=2"],
                fail_row=["status=error", "status=failed"],
                require_idle_shell=True,
                owner="codex-b",
                name="second",
                timeout_seconds=99,
                poll_seconds=30,
            )
            key_one = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args_one, kind="queue-after-status", command_text=args_one.command_text)
            )
            key_two = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args_two, kind="queue-after-status", command_text=args_two.command_text)
            )

        self.assertEqual(key_one, key_two)

    def test_queue_rejects_active_duplicate_dedupe_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="second",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            payload = tmux_control.managed_dedupe_payload(paths, args, kind="queue-after-idle", command_text=args.command_text)
            dedupe_key = tmux_control.managed_dedupe_key(payload)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "first"),
                {
                    "job_id": "first",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(
                    tmux_control.tmux_state,
                    "process_command_line",
                    return_value="python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id first --pane %1",
                ):
                    result = tmux_control.queue_after_idle(args)

        self.assertFalse(result["started"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["existing_job_id"], "first")
        self.assertEqual(result["dedupe_key"], dedupe_key)

    def test_concurrent_same_dedupe_creation_starts_only_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counter = {"pid": 20000}
            counter_lock = threading.Lock()
            pid_to_job: dict[int, str] = {}

            def fake_popen(argv: list[str], *_args: object, **_kwargs: object) -> object:
                with counter_lock:
                    counter["pid"] += 1
                    pid = counter["pid"]
                    job_id = argv[argv.index("--job-id") + 1]
                    pid_to_job[pid] = job_id

                class Proc:
                    pass

                proc = Proc()
                proc.pid = pid
                return proc

            def start(job_id: str) -> dict[str, object]:
                args = argparse.Namespace(
                    job_id=job_id,
                    pane="%1",
                    command_text="echo ok",
                    workspace=tmp,
                    state_dir=None,
                    name=None,
                    poll_seconds=1.0,
                    timeout_seconds=None,
                    strict_preflight=False,
                    bash_if_not_executable=False,
                    replace=False,
                    allow_duplicate=False,
                    owner=None,
                )
                return tmux_control.queue_after_idle(args)

            def command_line(pid: int | None) -> str:
                job_id = pid_to_job.get(int(pid or 0), "first")
                return f"python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id {job_id} --pane %1"

            with mock.patch.object(tmux_control.subprocess, "Popen", side_effect=fake_popen):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_control.tmux_state, "process_command_line", side_effect=command_line):
                        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                            results = list(pool.map(start, ["first", "second"]))

        self.assertEqual(sum(1 for result in results if result.get("started")), 1)
        self.assertEqual(sum(1 for result in results if result.get("duplicate")), 1)

    def test_directory_lock_cleans_up_when_owner_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "lock"
            with mock.patch.object(tmux_control.tmux_state, "atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    with tmux_control.directory_lock(lock_dir):
                        pass

        self.assertFalse(lock_dir.exists())

    def test_queue_allow_duplicate_records_duplicate_group(self) -> None:
        class Proc:
            pid = 12346

        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="second",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=True,
                owner=None,
            )
            dedupe_key = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args, kind="queue-after-idle", command_text=args.command_text)
            )
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "first"),
                {
                    "job_id": "first",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(
                        tmux_control.tmux_state,
                        "process_command_line",
                        return_value="python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id first --pane %1",
                    ):
                        result = tmux_control.queue_after_idle(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "second"))[0]

        self.assertTrue(result["started"])
        self.assertTrue(record["duplicate_allowed"])
        self.assertEqual(record["duplicate_of"], "first")

    def test_queue_reclaims_dead_same_dedupe_record_before_start(self) -> None:
        class Proc:
            pid = 12346

        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="second",
                pane="%1",
                command_text="echo ok",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=False,
                allow_duplicate=False,
                owner=None,
            )
            dedupe_key = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args, kind="queue-after-idle", command_text=args.command_text)
            )
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "first"),
                {
                    "job_id": "first",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=False):
                    result = tmux_control.queue_after_idle(args)

            first = tmux_state.read_json(tmux_state.job_path(paths, "first"))[0]

        self.assertTrue(result["started"])
        self.assertEqual(result["reclaimed"][0]["job_id"], "first")
        self.assertEqual(first["status"], "stale")
        self.assertEqual(first["replaced_by"], "second")

    def test_job_list_compact_omits_verbose_fields_and_truncates_strings(self) -> None:
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
                    "pane_id": "%1",
                    "dedupe_key": "sha256:" + ("a" * 64),
                    "dedupe_payload": {"status_file": "very large payload"},
                    "argv": ["python", "tmux_queue.py", "watch"],
                    "observed_status_tail": "large tail",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            args = argparse.Namespace(workspace=tmp, state_dir=None, compact=True, no_observed_tail=True, max_chars=12)
            result = tmux_control.job_list(args)

        job = result["jobs"][0]
        self.assertNotIn("argv", job)
        self.assertNotIn("dedupe_payload", job)
        self.assertNotIn("observed_status_tail", job)
        self.assertEqual(job["dedupe_key"], "sha256:aa...")

    def test_replace_tolerates_existing_pid_disappearing_before_signal(self) -> None:
        class Proc:
            pid = 54321

        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="replace-me",
                pane="%1",
                command_text="echo replacement",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=True,
                allow_duplicate=False,
                owner=None,
            )
            dedupe_key = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args, kind="queue-after-idle", command_text=args.command_text)
            )
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "replace-me"),
                {
                    "job_id": "replace-me",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_control.tmux_state, "managed_worker_pid_matches", return_value=True):
                        with mock.patch.object(tmux_control.os, "kill", side_effect=ProcessLookupError):
                            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()):
                                result = tmux_control.queue_after_idle(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "replace-me"))[0]

        self.assertTrue(result["started"])
        self.assertEqual(result["pid"], 54321)
        self.assertEqual(record["pid"], 54321)
        self.assertEqual(record["status"], "waiting")

    def test_replace_rejects_when_existing_worker_cannot_be_signaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="replace-me",
                pane="%1",
                command_text="echo replacement",
                workspace=tmp,
                state_dir=None,
                name=None,
                poll_seconds=1.0,
                timeout_seconds=None,
                strict_preflight=False,
                bash_if_not_executable=False,
                replace=True,
                allow_duplicate=False,
                owner=None,
            )
            dedupe_key = tmux_control.managed_dedupe_key(
                tmux_control.managed_dedupe_payload(paths, args, kind="queue-after-idle", command_text=args.command_text)
            )
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "replace-me"),
                {
                    "job_id": "replace-me",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_control.tmux_state, "managed_worker_pid_matches", return_value=True):
                        with mock.patch.object(tmux_control.os, "kill", side_effect=PermissionError("denied")):
                            with mock.patch.object(tmux_control.subprocess, "Popen") as popen:
                                result = tmux_control.queue_after_idle(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "replace-me"))[0]

        self.assertFalse(result["started"])
        self.assertIn("could not signal existing managed job", result["reason"])
        self.assertEqual(record["pid"], 12345)
        self.assertEqual(record["status"], "waiting_pane_idle")
        popen.assert_not_called()

    def test_job_gc_dry_run_and_mark_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "stale-job"),
                {
                    "job_id": "stale-job",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 0,
                    "pid_matches": False,
                    "pid_running": False,
                    "pane_id": "%1",
                    "stale": True,
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": old,
                    "updated_at": old,
                    "check_interval_seconds": 1,
                },
            )
            dry = tmux_control.job_gc(argparse.Namespace(workspace=tmp, state_dir=None, stale=True, dry_run=True))
            marked = tmux_control.job_gc(argparse.Namespace(workspace=tmp, state_dir=None, stale=True, dry_run=False))
            record = tmux_state.read_json(tmux_state.job_path(paths, "stale-job"))[0]

        self.assertEqual(dry["stale_jobs"][0]["job_id"], "stale-job")
        self.assertEqual(marked["marked"][0]["job_id"], "stale-job")
        self.assertEqual(record["status"], "stale")
        self.assertIn("heartbeat older", record["stale_reason"])
        self.assertNotIn("pid_matches", record)
        self.assertNotIn("pid_running", record)
        self.assertNotIn("stale", record)

    def test_job_gc_sanitizes_legacy_managed_job_and_status_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 0,
                    "pane_id": "%1",
                    "job_path": "/stale/job.json",
                    "status_path": "/stale/status.json",
                    "log_path": "/stale/log.log",
                    "heartbeat_at": old,
                    "updated_at": old,
                    "check_interval_seconds": 1,
                },
            )
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "updated_at": old,
                },
            )

            marked = tmux_control.job_gc(argparse.Namespace(workspace=tmp, state_dir=None, stale=True, dry_run=False))
            record = tmux_state.read_json(tmux_state.job_path(paths, "job-with-space"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "job-with-space"))[0]

        self.assertEqual(marked["marked"][0]["job_id"], "job-with-space")
        assert record is not None
        assert status is not None
        self.assertEqual(record["job_id"], "job-with-space")
        self.assertEqual(record["job_path"], str(tmux_state.job_path(paths, "job-with-space")))
        self.assertEqual(record["status_path"], str(tmux_state.status_path(paths, "job-with-space")))
        self.assertEqual(record["log_path"], str(tmux_state.log_path(paths, "job-with-space")))
        self.assertEqual(status["id"], "job-with-space")
        self.assertEqual(status["status"], "stale")

    def test_job_list_sanitizes_legacy_managed_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "watch",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            result = tmux_control.job_list(argparse.Namespace(workspace=tmp, state_dir=None))

        self.assertEqual(result["jobs"][0]["job_id"], "job-with-space")
        self.assertEqual(result["jobs"][0]["id"], "job-with-space")

    def test_job_list_kind_filter_uses_normalized_legacy_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": " Watch ",
                    "status": "starting",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            result = tmux_control.job_list(argparse.Namespace(workspace=tmp, state_dir=None), kind="watch")

        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["job_id"], "watch")
        self.assertEqual(result["jobs"][0]["kind"], "watch")
        self.assertNotIn("stale_reason", result["jobs"][0])

    def test_job_status_sanitizes_legacy_managed_job_id_for_pid_match(self) -> None:
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
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": old_time,
                    "updated_at": old_time,
                    "check_interval_seconds": 1,
                },
            )
            command = "python3 /repo/scripts/tmux_queue.py watch --job-id job-with-space --pane %1"
            with mock.patch.object(tmux_control, "pid_is_running", return_value=False):
                with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                    with mock.patch.object(tmux_control.tmux_state, "process_command_line", return_value=command):
                        result = tmux_control.job_status(argparse.Namespace(job_id="job with space", workspace=tmp, state_dir=None))

        assert result["record"] is not None
        self.assertEqual(result["record"]["job_id"], "job-with-space")
        self.assertTrue(result["record"]["pid_running"])
        self.assertTrue(result["pid_running"])
        self.assertTrue(result["record"]["pid_matches"])
        self.assertFalse(result["record"]["stale"])
        self.assertNotIn("stale_reason", result["record"])

    def test_job_status_rejects_blank_job_id_without_reading_default_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job"),
                {
                    "job_id": "job",
                    "kind": "watch",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    tmux_control.job_status(argparse.Namespace(job_id=" ", workspace=tmp, state_dir=None))

        self.assertIn("job command requires nonblank --job-id", stderr.getvalue())

    def test_job_status_kind_filter_uses_normalized_legacy_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": " Watch ",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            result = tmux_control.job_status(argparse.Namespace(job_id="watch", workspace=tmp, state_dir=None), kind="watch")

        self.assertTrue(result["found"])
        assert result["record"] is not None
        self.assertEqual(result["record"]["kind"], "watch")

    def test_job_status_reports_dead_pid_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "dead-watch"),
                {
                    "job_id": "dead-watch",
                    "kind": "watch",
                    "status": "running",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 180,
                },
            )
            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=False):
                result = tmux_control.job_status(argparse.Namespace(job_id="dead-watch", workspace=tmp, state_dir=None))

        assert result["record"] is not None
        self.assertEqual(result["record"]["status"], "running")
        self.assertEqual(result["record"]["effective_status"], "dead")
        self.assertEqual(result["record"]["process_state"], "dead_pid")
        self.assertTrue(result["record"]["stale"])

    def test_job_status_reports_orphaned_foreign_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "foreign-watch"),
                {
                    "job_id": "foreign-watch",
                    "kind": "watch",
                    "status": "running",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 180,
                },
            )
            with mock.patch.object(tmux_control.tmux_state, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control.tmux_state, "process_command_line", return_value="python3 other.py"):
                    result = tmux_control.job_status(argparse.Namespace(job_id="foreign-watch", workspace=tmp, state_dir=None))

        assert result["record"] is not None
        self.assertEqual(result["record"]["effective_status"], "orphaned")
        self.assertEqual(result["record"]["process_state"], "foreign_pid")
        self.assertFalse(result["record"]["pid_matches"])

    def test_job_status_kind_filter_checks_status_when_record_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = tmux_state.build_status(
                kind="job",
                item_id="plain",
                attempt=1,
                name=None,
                status="succeeded",
                pane_id="%1",
                command_preview_text="echo ok",
                cwd=tmp,
                status_file=tmux_state.status_path(paths, "plain"),
                log_file=tmux_state.log_path(paths, "plain"),
                exit_code=0,
                last_output="ok",
            )
            tmux_state.write_status(tmux_state.status_path(paths, "plain"), status)

            watch_result = tmux_control.job_status(argparse.Namespace(job_id="plain", workspace=tmp, state_dir=None), kind="watch")
            generic_result = tmux_control.job_status(argparse.Namespace(job_id="plain", workspace=tmp, state_dir=None))

        self.assertFalse(watch_result["found"])
        self.assertEqual(watch_result["reason"], "status is not a watch job")
        self.assertTrue(generic_result["found"])
        assert generic_result["status"] is not None
        self.assertEqual(generic_result["status"]["kind"], "job")

    def test_job_cancel_kind_filter_uses_normalized_legacy_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": " Watch ",
                    "status": "cancelled",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            result = tmux_control.job_cancel(argparse.Namespace(job_id="watch", workspace=tmp, state_dir=None), kind="watch")

        self.assertFalse(result["cancelled"])
        self.assertEqual(result["reason"], "job already cancelled")

    def test_job_cancel_rejects_blank_job_id_without_cancelling_default_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job"),
                {
                    "job_id": "job",
                    "kind": "watch",
                    "status": "running",
                    "pid": 0,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    tmux_control.job_cancel(argparse.Namespace(job_id="", workspace=tmp, state_dir=None))

            record = tmux_state.read_json(tmux_state.job_path(paths, "job"))[0]

        self.assertIn("job command requires nonblank --job-id", stderr.getvalue())
        assert record is not None
        self.assertEqual(record["status"], "running")

    def test_job_cancel_terminal_record_ignores_reused_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "old-job"),
                {
                    "job_id": "old-job",
                    "kind": "queue-after-idle",
                    "status": " Cancelled ",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            with mock.patch.object(tmux_control, "pid_is_running", return_value=True):
                with mock.patch.object(tmux_control.tmux_state, "managed_worker_pid_matches") as matches:
                    with mock.patch.object(tmux_control.os, "kill") as kill:
                        result = tmux_control.job_cancel(argparse.Namespace(job_id="old-job", workspace=tmp, state_dir=None))

        self.assertFalse(result["cancelled"])
        self.assertEqual(result["reason"], "job already cancelled")
        matches.assert_not_called()
        kill.assert_not_called()

    def test_job_cancel_sanitizes_legacy_managed_job_id_before_pid_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job-with-space"),
                {"job_id": "job with space", "status": "waiting_pane_idle", "updated_at": tmux_state.utc_now()},
            )
            command = "python3 /repo/scripts/tmux_queue.py queue-after-idle --job-id job-with-space --pane %1"
            args = argparse.Namespace(job_id="job with space", workspace=tmp, state_dir=None)
            with mock.patch.object(tmux_control, "pid_is_running", side_effect=[True, False]):
                with mock.patch.object(tmux_control.tmux_state, "process_command_line", return_value=command):
                    with mock.patch.object(tmux_control.os, "kill", side_effect=ProcessLookupError):
                        result = tmux_control.job_cancel(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "job-with-space"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "job-with-space"))[0]

        self.assertTrue(result["cancelled"])
        assert record is not None
        assert status is not None
        self.assertEqual(record["job_id"], "job-with-space")
        self.assertEqual(record["status"], "cancelled")
        self.assertNotIn("pid_matches", record)
        self.assertNotIn("pid_running", record)
        self.assertNotIn("stale", record)
        self.assertNotIn("stale_reason", record)
        self.assertEqual(status["id"], "job-with-space")
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(result["status"], status)

    def test_job_cancel_tolerates_pid_disappearing_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "race-job"),
                {
                    "job_id": "race-job",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            args = argparse.Namespace(job_id="race-job", workspace=tmp, state_dir=None)
            with mock.patch.object(tmux_control, "pid_is_running", side_effect=[True, False]):
                with mock.patch.object(tmux_control.tmux_state, "managed_worker_pid_matches", return_value=True):
                    with mock.patch.object(tmux_control.os, "kill", side_effect=ProcessLookupError):
                        result = tmux_control.job_cancel(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "race-job"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "race-job"))[0]

        self.assertTrue(result["cancelled"])
        self.assertFalse(result["signal_sent"])
        self.assertEqual(record["status"], "cancelled")
        self.assertNotIn("pid_running", record)
        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["event_id"])
        self.assertEqual(result["status"], status)

    def test_queue_after_idle_aliases_parse_to_existing_dests(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "queue-after-idle",
                "--job-id",
                "job",
                "--then-pane",
                "%1",
                "--then-command",
                "echo ok",
                "--interval",
                "30",
                "--then-require-idle-shell",
                "--allow-duplicate",
            ]
        )
        self.assertEqual(args.pane, "%1")
        self.assertEqual(args.command_text, "echo ok")
        self.assertEqual(args.poll_seconds, 30)
        self.assertTrue(args.require_idle_shell)
        self.assertTrue(args.allow_duplicate)

    def test_queue_after_status_aliases_parse_to_existing_dests(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "queue-after-status",
                "--job-id",
                "job",
                "--then-pane",
                "%1",
                "--then-command",
                "echo ok",
                "--status-file",
                "status.tsv",
                "--require-row",
                "run_cfg=a,status=done",
                "--interval",
                "30",
                "--then-require-idle-shell",
                "--allow-duplicate",
            ]
        )
        self.assertEqual(args.pane, "%1")
        self.assertEqual(args.command_text, "echo ok")
        self.assertEqual(args.poll_seconds, 30)
        self.assertTrue(args.require_idle_shell)
        self.assertTrue(args.allow_duplicate)

    def test_queue_after_status_command_file_parses(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(
            [
                "queue-after-status",
                "--job-id",
                "job",
                "--pane",
                "%1",
                "--command-file",
                "queued.sh",
                "--status-file",
                "status.tsv",
                "--require-row",
                "run_cfg=a,status=done",
            ]
        )
        self.assertIsNone(args.command_text)
        self.assertEqual(args.command_file, "queued.sh")


if __name__ == "__main__":
    unittest.main()
