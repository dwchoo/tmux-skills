from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_control
import tmux_state


class TmuxControlTests(unittest.TestCase):
    def test_capture_max_chars_after_strip(self) -> None:
        args = argparse.Namespace(pane="%1", lines=10, strip_ansi=True, max_chars=4)
        with mock.patch.object(tmux_control, "capture_text", return_value="abcdef"):
            result = tmux_control.capture(args)
        self.assertEqual(result["output"], "cdef")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["omitted_chars"], 2)

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

    def test_monitor_rejects_no_condition_in_main_parser_contract(self) -> None:
        parser = tmux_control.build_parser()
        args = parser.parse_args(["monitor", "--pane", "%1"])
        self.assertIsNone(args.match_regex)
        self.assertFalse(args.idle_shell)
        self.assertIsNone(args.timeout_seconds)


if __name__ == "__main__":
    unittest.main()
