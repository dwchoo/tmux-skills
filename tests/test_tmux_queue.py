from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_queue
import tmux_state


class TmuxQueueTests(unittest.TestCase):
    def status_args(self, tmp: str, status_file: Path, **overrides: object) -> argparse.Namespace:
        base = {
            "job_id": "queue",
            "name": None,
            "pane": "%1",
            "command_text": "echo next",
            "command_file": None,
            "status_file": str(status_file),
            "require_row": ["configs/msec.toml:done"],
            "fail_row": [],
            "poll_seconds": 0.001,
            "timeout_seconds": 1.0,
            "workspace": tmp,
            "state_dir": None,
            "require_idle_shell": True,
            "strict_preflight": False,
            "bash_if_not_executable": False,
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_queue_after_status_submits_when_rows_match_and_pane_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file)
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": True}), mock.patch.object(
                tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True}
            ):
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "submitted")
        self.assertEqual(status["matched_required_rows"], ["configs/msec.toml:done"])

    def test_queue_after_status_matches_header_tsv_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, require_row=["run_cfg=configs/msec.toml,status=done"])
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": True}), mock.patch.object(
                tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True}
            ):
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(status["matched_required_rows"], ["run_cfg=configs/msec.toml,status=done"])

    def test_queue_after_status_fails_on_fail_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tfailed\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, require_row=["configs/msec.toml:done"], fail_row=["configs/msec.toml:failed"])
            code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["matched_fail_rows"], ["configs/msec.toml:failed"])

    def test_queue_after_status_records_waiting_pane_idle_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, poll_seconds=0.001, timeout_seconds=0.003)
            original_write = tmux_queue.write_worker_record
            statuses: list[str] = []

            def record_status(*call_args: object, **kwargs: object) -> dict[str, object]:
                statuses.append(str(kwargs["status"]))
                return original_write(*call_args, **kwargs)

            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": False, "reason": "busy"}), mock.patch.object(
                tmux_queue, "write_worker_record", side_effect=record_status
            ):
                code = tmux_queue.run_queue_after_status(args)

        self.assertEqual(code, 1)
        self.assertIn("waiting_status", statuses)
        self.assertIn("waiting_pane_idle", statuses)

    def test_queue_after_status_records_waiting_pane_idle_before_timeout_when_already_timed_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, poll_seconds=0.001, timeout_seconds=0.0)
            original_write = tmux_queue.write_worker_record
            statuses: list[str] = []

            def record_status(*call_args: object, **kwargs: object) -> dict[str, object]:
                statuses.append(str(kwargs["status"]))
                return original_write(*call_args, **kwargs)

            guard = {"ok": False, "reason": "busy"}
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value=guard), mock.patch.object(
                tmux_queue, "write_worker_record", side_effect=record_status
            ):
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(statuses[-2:], ["waiting_pane_idle", "timeout"])
        self.assertEqual(status["status"], "timeout")
        self.assertEqual(status["idle_shell_check"], guard)
        self.assertEqual(status["matched_required_rows"], ["configs/msec.toml:done"])

    def test_watch_records_timeout_after_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="watch",
                name=None,
                pane="%1",
                interval=0.001,
                capture_lines=10,
                status_file=None,
                timeout_seconds=0.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text", return_value="latest output"):
                code = tmux_queue.run_watch(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "watch"))
            log_text = tmux_state.log_path(paths, "watch").read_text(encoding="utf-8")

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")
        self.assertEqual(log_text, "latest output")

    def test_queue_after_idle_fails_when_pane_lookup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%missing",
                command_text="echo next",
                command_file=None,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
            )
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", side_effect=RuntimeError("can't find pane")):
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("can't find pane", status["last_output"])

    def test_queue_after_idle_fails_when_pane_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%missing",
                command_text="echo next",
                command_file=None,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
            )
            with mock.patch.object(
                tmux_queue.tmux_control,
                "idle_shell_check",
                return_value={"ok": False, "reason": "pane could not be resolved", "pane_id": "%missing"},
            ):
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["idle_shell_check"]["reason"], "pane could not be resolved")


if __name__ == "__main__":
    unittest.main()
