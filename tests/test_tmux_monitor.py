from __future__ import annotations

import argparse
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


if __name__ == "__main__":
    unittest.main()
