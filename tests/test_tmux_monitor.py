from __future__ import annotations

import argparse
import io
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_monitor
import tmux_state


class TmuxMonitorTests(unittest.TestCase):
    def args(self, tmp: str, **overrides: object) -> argparse.Namespace:
        base = {
            "monitor_id": "mon",
            "name": None,
            "pane": "%1",
            "match_regex": None,
            "idle_shell": False,
            "timeout_seconds": 0.01,
            "poll_seconds": 0.001,
            "lines": 20,
            "workspace": tmp,
            "state_dir": None,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_regex_match_records_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_monitor, "capture_pane", return_value="hello ERROR"):
                code = tmux_monitor.run_monitor(self.args(tmp, match_regex="ERROR", timeout_seconds=5))

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))
        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "matched")

    def test_regex_uses_full_capture_but_status_tail_is_shortened(self) -> None:
        output = "\n".join(["ERROR first", *[f"line {index}" for index in range(1, 15)]])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_monitor, "capture_pane", return_value=output):
                code = tmux_monitor.run_monitor(
                    self.args(tmp, match_regex="ERROR", timeout_seconds=5, status_lines=2, status_max_chars=1200)
                )

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))
            log_text = tmux_state.log_path(paths, "mon").read_text(encoding="utf-8")

        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "matched")
        self.assertEqual(status["last_output"], "line 13\nline 14")
        self.assertEqual(status["status_lines"], 2)
        self.assertEqual(status["status_max_chars"], 1200)
        self.assertIn("ERROR first", log_text)

    def test_timeout_records_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_monitor, "capture_pane", return_value="still running"):
                code = tmux_monitor.run_monitor(self.args(tmp))

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))
        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")

    def test_idle_shell_records_matched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tmux_monitor, "capture_pane", return_value="~/repo\n$"):
                code = tmux_monitor.run_monitor(self.args(tmp, idle_shell=True, timeout_seconds=5))
        self.assertEqual(code, 0)

    def test_rejects_blank_identity_before_status_write(self) -> None:
        cases = [
            ({"monitor_id": ""}, "monitor requires nonblank --monitor-id"),
            ({"pane": " "}, "monitor requires nonblank --pane"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as tmp:
                    with mock.patch.object(tmux_monitor.sys, "stderr", io.StringIO()) as stderr:
                        with mock.patch.object(tmux_monitor, "capture_pane") as capture:
                            code = tmux_monitor.run_monitor(self.args(tmp, **overrides))
                    paths = tmux_state.state_paths(tmp)

                self.assertEqual(code, 2)
                self.assertIn(message, stderr.getvalue())
                capture.assert_not_called()
                self.assertFalse(tmux_state.status_path(paths, "job").exists())
                self.assertFalse(tmux_state.status_path(paths, "mon").exists())

    def test_invalid_regex_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = tmux_monitor.run_monitor(self.args(tmp, match_regex="[", timeout_seconds=5))

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("unterminated", status["last_output"])

    def test_log_write_failure_records_clear_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.log_path(paths, "mon").mkdir()

            with mock.patch.object(tmux_monitor, "capture_pane", return_value="hello"):
                code = tmux_monitor.run_monitor(self.args(tmp, match_regex="hello", timeout_seconds=5))

            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not write monitor log file", status["last_output"])

    def test_sigterm_records_stopped_status_and_restores_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous_sigterm = signal.getsignal(signal.SIGTERM)

            def terminate_monitor(_pane: str, _lines: int) -> str:
                os.kill(os.getpid(), signal.SIGTERM)
                return "should not return"

            with mock.patch.object(tmux_monitor, "capture_pane", side_effect=terminate_monitor):
                code = tmux_monitor.run_monitor(self.args(tmp, match_regex="never", timeout_seconds=5))

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "mon"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["exit_code"], 1)
        self.assertTrue(status["event_id"])
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_sigterm)

    def test_parser_rejects_nonpositive_polling_values(self) -> None:
        parser = tmux_monitor.build_parser()
        invalid_commands = [
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--poll-seconds", "0"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "-1"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "nan"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--poll-seconds", "inf"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_monitor.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_parser_rejects_nonpositive_line_counts(self) -> None:
        parser = tmux_monitor.build_parser()
        invalid_commands = [
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--lines", "0"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--lines", "-1"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--status-lines", "0"],
            ["--monitor-id", "mon", "--pane", "%1", "--timeout-seconds", "1", "--status-max-chars", "0"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_monitor.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_main_requires_at_least_one_trigger_condition(self) -> None:
        argv = ["tmux_monitor.py", "--monitor-id", "mon", "--pane", "%1"]
        with mock.patch.object(tmux_monitor.sys, "argv", argv):
            with mock.patch.object(tmux_monitor.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    tmux_monitor.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("monitor requires --match-regex, --idle-shell, or --timeout-seconds", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
