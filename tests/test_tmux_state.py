from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_state


class TmuxStateTests(unittest.TestCase):
    def test_state_dir_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            paths = tmux_state.state_paths(str(workspace), "state")
            self.assertEqual(paths["root"], (workspace / "state").resolve())

            absolute = Path(tmp) / "absolute"
            paths = tmux_state.state_paths(str(workspace), str(absolute))
            self.assertEqual(paths["root"], absolute.resolve())

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

    def test_corrupt_json_is_soft_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "bad.json").write_text("{", encoding="utf-8")

            statuses, errors = tmux_state.load_statuses(paths["root"])
            self.assertEqual(statuses, [])
            self.assertEqual(len(errors), 1)

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


if __name__ == "__main__":
    unittest.main()
