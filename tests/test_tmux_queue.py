from __future__ import annotations

import argparse
import io
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

    def test_worker_parser_requires_exactly_one_command_source(self) -> None:
        parser = tmux_queue.build_parser()
        base = ["queue-after-idle", "--job-id", "queue", "--pane", "%1"]
        with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(base)
        with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([*base, "--command", "echo ok", "--command-file", "cmd.sh"])

    def test_worker_parser_accepts_command_file_source(self) -> None:
        parser = tmux_queue.build_parser()
        args = parser.parse_args(["queue-after-idle", "--job-id", "queue", "--pane", "%1", "--command-file", "cmd.sh"])

        self.assertIsNone(args.command_text)
        self.assertEqual(args.command_file, "cmd.sh")

    def test_worker_main_rejects_blank_identity_before_safe_id(self) -> None:
        cases = [
            (
                [
                    "tmux_queue.py",
                    "queue-after-idle",
                    "--job-id",
                    "",
                    "--pane",
                    "%1",
                    "--command",
                    "echo ok",
                ],
                "managed worker requires nonblank --job-id",
            ),
            (
                [
                    "tmux_queue.py",
                    "queue-after-idle",
                    "--job-id",
                    "queue",
                    "--pane",
                    " ",
                    "--command",
                    "echo ok",
                ],
                "managed worker requires nonblank --pane",
            ),
        ]
        for argv, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(tmux_queue.sys, "argv", argv):
                    with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as raised:
                            tmux_queue.main()

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.getvalue())

    def test_direct_worker_entrypoints_reject_blank_identity_before_safe_id(self) -> None:
        cases = [
            (
                tmux_queue.run_queue_after_idle,
                {"job_id": "", "pane": "%1", "command_text": "echo ok", "command_file": None},
                "managed worker requires nonblank --job-id",
            ),
            (
                tmux_queue.run_queue_after_status,
                {
                    "job_id": "queue",
                    "pane": "\t",
                    "command_text": "echo ok",
                    "command_file": None,
                    "status_file": "status.tsv",
                    "require_row": ["state=done"],
                    "fail_row": [],
                },
                "managed worker requires nonblank --pane",
            ),
            (
                tmux_queue.run_watch,
                {"job_id": " ", "pane": "%1", "interval": 1.0, "capture_lines": 10, "status_file": None},
                "managed worker requires nonblank --job-id",
            ),
        ]
        for entrypoint, overrides, message in cases:
            with self.subTest(entrypoint=entrypoint.__name__, message=message):
                with tempfile.TemporaryDirectory() as tmp:
                    base = {
                        "job_id": "queue",
                        "name": None,
                        "pane": "%1",
                        "command_text": "echo ok",
                        "command_file": None,
                        "status_file": "status.tsv",
                        "require_row": ["state=done"],
                        "fail_row": [],
                        "poll_seconds": 0.001,
                        "interval": 0.001,
                        "capture_lines": 10,
                        "timeout_seconds": 1.0,
                        "workspace": tmp,
                        "state_dir": None,
                        "require_idle_shell": False,
                        "strict_preflight": False,
                        "bash_if_not_executable": False,
                    }
                    base.update(overrides)
                    args = argparse.Namespace(**base)
                    with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()) as stderr:
                        code = entrypoint(args)
                    paths = tmux_state.state_paths(tmp)
                    job_files = list(paths["jobs"].glob("*.json")) if paths["jobs"].exists() else []
                    status_files = list(paths["status"].glob("*.json")) if paths["status"].exists() else []

                self.assertEqual(code, 2)
                self.assertIn(message, stderr.getvalue())
                self.assertEqual(job_files, [])
                self.assertEqual(status_files, [])

    def test_run_worker_safely_rejects_blank_identity_without_calling_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            args = argparse.Namespace(job_id="", pane="%1")
            worker = mock.Mock(return_value=0)
            with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()) as stderr:
                code = tmux_queue.run_worker_safely(args, paths, "job", worker)
            job_files = list(paths["jobs"].glob("*.json")) if paths["jobs"].exists() else []
            status_files = list(paths["status"].glob("*.json")) if paths["status"].exists() else []

        self.assertEqual(code, 2)
        self.assertIn("managed worker requires nonblank --job-id", stderr.getvalue())
        worker.assert_not_called()
        self.assertEqual(job_files, [])
        self.assertEqual(status_files, [])

    def test_read_command_rejects_blank_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blank_file = Path(tmp) / "blank.sh"
            blank_file.write_text(" \n\t ", encoding="utf-8")
            cases = [
                (argparse.Namespace(command_text=" \n\t ", command_file=None), "command is blank"),
                (argparse.Namespace(command_text=None, command_file=""), "command file path is blank"),
                (argparse.Namespace(command_text=None, command_file=str(blank_file)), "command is blank"),
            ]
            for args, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        tmux_queue.read_command(args)

    def test_active_worker_state_normalizes_status_case(self) -> None:
        self.assertTrue(tmux_queue.active_worker_state({"status": " Running "}, None))
        self.assertFalse(tmux_queue.active_worker_state({"status": " Succeeded "}, {"status": " Running "}))
        self.assertTrue(tmux_queue.active_worker_state({}, {"status": " Waiting_Status "}))

    def test_write_worker_record_normalizes_kind_and_status_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(job_id="queue", pane="%1", command_file=None, interval=None, poll_seconds=1.0)

            record = tmux_queue.write_worker_record(
                paths,
                args,
                kind=" Queue-After-Idle ",
                status=" Waiting ",
                command_text="echo ok",
                extra={"kind": "wrong", "status": "failed", "pid_matches": True, "pid_running": True, "stale": False},
            )
            stored = tmux_state.read_json(tmux_state.job_path(paths, "queue"))[0]

        self.assertEqual(record["kind"], "queue-after-idle")
        self.assertEqual(record["status"], "waiting")
        self.assertNotIn("pid_matches", record)
        self.assertNotIn("pid_running", record)
        self.assertNotIn("stale", record)
        assert stored is not None
        self.assertEqual(stored["kind"], "queue-after-idle")
        self.assertEqual(stored["status"], "waiting")
        self.assertNotIn("pid_matches", stored)
        self.assertNotIn("pid_running", stored)
        self.assertNotIn("stale", stored)

    def test_write_worker_status_normalizes_kind_and_status_after_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(job_id="queue", name=None, pane="%1")

            status = tmux_queue.write_worker_status(
                paths,
                args,
                kind=" Queue-After-Idle ",
                status=" Failed ",
                started_at=tmux_state.utc_now(),
                command_text="echo ok",
                last_output="failed",
                exit_code=1,
                extra={"kind": "wrong", "status": "running"},
            )
            stored = tmux_state.read_json(tmux_state.status_path(paths, "queue"))[0]

        self.assertEqual(status["kind"], "queue-after-idle")
        self.assertEqual(status["status"], "failed")
        assert stored is not None
        self.assertEqual(status, stored)
        self.assertEqual(stored["kind"], "queue-after-idle")
        self.assertEqual(stored["status"], "failed")

    def test_worker_parser_rejects_nonpositive_polling_intervals(self) -> None:
        parser = tmux_queue.build_parser()
        invalid_commands = [
            ["queue-after-idle", "--job-id", "queue", "--pane", "%1", "--command", "echo ok", "--poll-seconds", "0"],
            ["queue-after-idle", "--job-id", "queue", "--pane", "%1", "--command", "echo ok", "--poll-seconds", "nan"],
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
                "--poll-seconds",
                "-1",
            ],
            ["watch", "--job-id", "watch", "--pane", "%1", "--interval", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--interval", "inf"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_worker_parser_rejects_nonpositive_capture_lines(self) -> None:
        parser = tmux_queue.build_parser()
        invalid_commands = [
            ["watch", "--job-id", "watch", "--pane", "%1", "--capture-lines", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--capture-lines", "-1"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--status-lines", "0"],
            ["watch", "--job-id", "watch", "--pane", "%1", "--status-max-chars", "0"],
        ]
        for command in invalid_commands:
            with self.subTest(command=command):
                with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(command)

    def test_main_requires_queue_after_status_require_row(self) -> None:
        argv = [
            "tmux_queue.py",
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
        with mock.patch.object(tmux_queue.sys, "argv", argv):
            with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    tmux_queue.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("queue-after-status requires at least one --require-row", stderr.getvalue())

    def test_main_rejects_blank_watch_status_file_before_worker_start(self) -> None:
        argv = ["tmux_queue.py", "watch", "--job-id", "watch", "--pane", "%1", "--status-file", " \n\t "]
        with mock.patch.object(tmux_queue.sys, "argv", argv):
            with mock.patch.object(tmux_queue.sys, "stderr", io.StringIO()) as stderr:
                with mock.patch.object(tmux_queue, "run_worker_safely") as run_worker:
                    with self.assertRaises(SystemExit) as raised:
                        tmux_queue.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("watch requires nonblank --status-file when provided", stderr.getvalue())
        run_worker.assert_not_called()

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

    def test_queue_after_status_retries_when_send_idle_recheck_gets_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file)
            send_results = [
                {"sent_to_pane": False, "reason": "busy", "idle_shell_check": {"ok": False, "reason": "busy"}},
                {"sent_to_pane": True},
            ]
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": True}):
                with mock.patch.object(tmux_queue.tmux_control, "send", side_effect=send_results) as send:
                    code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(status["status"], "submitted")

    def test_queue_after_status_colon_spec_does_not_substring_match_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tundone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, require_idle_shell=False, timeout_seconds=0.0)
            with mock.patch.object(tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True}) as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")
        self.assertEqual(status["matched_required_rows"], [])
        send.assert_not_called()

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

    def test_queue_after_status_rejects_blank_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = self.status_args(tmp, Path(tmp) / "ignored.tsv", require_idle_shell=False)
            args.status_file = ""
            with mock.patch.object(tmux_queue.tmux_control, "send") as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "status file path is blank")
        self.assertEqual(status["error"], "status file path is blank")
        send.assert_not_called()

    def test_queue_after_status_rejects_blank_required_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, require_row=[""], require_idle_shell=False)
            with mock.patch.object(tmux_queue.tmux_control, "send") as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "--require-row is blank")
        self.assertEqual(status["error"], "--require-row is blank")
        send.assert_not_called()

    def test_queue_after_status_rejects_blank_fail_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("configs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(tmp, status_file, fail_row=[""], require_idle_shell=False)
            with mock.patch.object(tmux_queue.tmux_control, "send") as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "--fail-row is blank")
        self.assertEqual(status["error"], "--fail-row is blank")
        send.assert_not_called()

    def test_specs_matching_does_not_treat_blank_spec_as_wildcard(self) -> None:
        self.assertEqual(tmux_queue.specs_matching(["configs/msec.toml\tdone"], [""]), [])

    def test_queue_after_status_records_failed_status_when_status_file_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.mkdir()
            args = self.status_args(tmp, status_file)

            code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not read status file", status["last_output"])
        self.assertEqual(status["observed_status_file"], str(status_file.resolve()))

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

    def test_queue_after_idle_timeout_blocks_ready_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_text="echo next",
                command_file=None,
                poll_seconds=0.001,
                timeout_seconds=0.0,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
            )
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": True}), mock.patch.object(
                tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True}
            ) as send:
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")
        self.assertIn("before command submission", status["last_output"])
        send.assert_not_called()

    def test_queue_after_idle_retries_when_send_idle_recheck_gets_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
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
            send_results = [
                {"sent_to_pane": False, "reason": "busy", "idle_shell_check": {"ok": False, "reason": "busy"}},
                {"sent_to_pane": True},
            ]
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", return_value={"ok": True}):
                with mock.patch.object(tmux_queue.tmux_control, "send", side_effect=send_results) as send:
                    code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(status["status"], "submitted")

    def test_queue_after_idle_missing_command_file_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sh"
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_text=None,
                command_file=str(missing),
                poll_seconds=0.001,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
            )
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check") as idle_shell_check:
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not read command file", status["last_output"])
        self.assertEqual(status["error"], status["last_output"])
        idle_shell_check.assert_not_called()

    def test_queue_after_idle_blank_command_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_text=" \n\t ",
                command_file=None,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=True,
                strict_preflight=False,
                bash_if_not_executable=False,
            )
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check") as idle_shell_check:
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "command is blank")
        self.assertEqual(status["error"], status["last_output"])
        idle_shell_check.assert_not_called()

    def test_queue_after_status_missing_command_file_records_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tdone\n", encoding="utf-8")
            missing = Path(tmp) / "missing.sh"
            args = self.status_args(
                tmp,
                status_file,
                command_text=None,
                command_file=str(missing),
                require_row=["run_cfg=configs/msec.toml,status=done"],
                require_idle_shell=False,
            )
            with mock.patch.object(tmux_queue.tmux_control, "send") as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not read command file", status["last_output"])
        self.assertEqual(status["error"], status["last_output"])
        send.assert_not_called()

    def test_queue_submit_preflight_uses_target_pane_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            pane_cwd = Path(tmp) / "pane"
            workspace.mkdir()
            pane_cwd.mkdir()
            workspace_script = workspace / "script.sh"
            pane_script = pane_cwd / "script.sh"
            workspace_script.write_text("echo workspace\n", encoding="utf-8")
            pane_script.write_text("echo pane\n", encoding="utf-8")
            workspace_script.chmod(0o700)
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_text="unused",
                command_file=None,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                workspace=str(workspace),
                state_dir=None,
                require_idle_shell=False,
                strict_preflight=True,
                bash_if_not_executable=False,
            )
            paths = tmux_state.state_paths(str(workspace))
            tmux_state.ensure_state_dirs(paths)
            with mock.patch.object(tmux_queue.tmux_control, "current_info", return_value={"current_path": str(pane_cwd)}):
                with mock.patch.object(tmux_queue.tmux_control, "run_tmux") as run_tmux:
                    code = tmux_queue.submit_command(
                        paths,
                        args,
                        started_at=tmux_state.utc_now(),
                        kind="queue-after-idle",
                        command_text="./script.sh",
                        last_output="ready",
                    )

            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 1)
        self.assertIn("not executable", status["send_result"]["reason"])
        run_tmux.assert_not_called()

    def test_submit_command_keeps_actual_send_exception_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_file=None,
                poll_seconds=0.001,
                workspace=tmp,
                require_idle_shell=False,
                strict_preflight=False,
                bash_if_not_executable=False,
            )

            with mock.patch.object(tmux_queue.tmux_control, "send", side_effect=RuntimeError("actual send failure")):
                code = tmux_queue.submit_command(
                    paths,
                    args,
                    started_at=tmux_state.utc_now(),
                    kind="queue-after-idle",
                    command_text="echo next",
                    last_output="ready",
                    extra={"error": "stale error", "diagnostic": "kept"},
                )

            record = tmux_state.read_json(tmux_state.job_path(paths, "queue"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "queue"))[0]

        self.assertEqual(code, 1)
        assert record is not None
        assert status is not None
        self.assertEqual(record["error"], "actual send failure")
        self.assertEqual(status["error"], "actual send failure")
        self.assertEqual(status["last_output"], "actual send failure")
        self.assertEqual(status["diagnostic"], "kept")

    def test_submit_command_records_send_system_exit_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_file=None,
                poll_seconds=0.001,
                workspace=tmp,
                require_idle_shell=False,
                strict_preflight=False,
                bash_if_not_executable=False,
            )

            with mock.patch.object(tmux_queue.tmux_control, "send", side_effect=SystemExit(1)):
                code = tmux_queue.submit_command(
                    paths,
                    args,
                    started_at=tmux_state.utc_now(),
                    kind="queue-after-idle",
                    command_text="echo next",
                    last_output="ready",
                )

            status = tmux_state.read_json(tmux_state.status_path(paths, "queue"))[0]

        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "SystemExit(1)")
        self.assertEqual(status["last_output"], "SystemExit(1)")

    def test_submit_command_keeps_actual_send_result_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(
                job_id="queue",
                name=None,
                pane="%1",
                command_file=None,
                poll_seconds=0.001,
                workspace=tmp,
                require_idle_shell=False,
                strict_preflight=False,
                bash_if_not_executable=False,
            )

            with mock.patch.object(tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True, "reason": "actual"}):
                code = tmux_queue.submit_command(
                    paths,
                    args,
                    started_at=tmux_state.utc_now(),
                    kind="queue-after-idle",
                    command_text="echo next",
                    last_output="ready",
                    extra={
                        "send_result": {"sent_to_pane": False, "reason": "stale"},
                        "command_hash": "stale",
                        "diagnostic": "kept",
                    },
                )

            record = tmux_state.read_json(tmux_state.job_path(paths, "queue"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "queue"))[0]

        self.assertEqual(code, 0)
        assert record is not None
        assert status is not None
        self.assertEqual(record["command_hash"], tmux_queue.command_hash("echo next"))
        self.assertEqual(status["command_hash"], tmux_queue.command_hash("echo next"))
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["send_result"], {"sent_to_pane": True, "reason": "actual"})
        self.assertEqual(status["diagnostic"], "kept")

    def test_queue_after_status_timeout_blocks_ready_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_file = Path(tmp) / "status.tsv"
            status_file.write_text("run_cfg\tstatus\nconfigs/msec.toml\tdone\n", encoding="utf-8")
            args = self.status_args(
                tmp,
                status_file,
                require_row=["run_cfg=configs/msec.toml,status=done"],
                require_idle_shell=False,
                timeout_seconds=0.0,
            )
            with mock.patch.object(tmux_queue.tmux_control, "send", return_value={"sent_to_pane": True}) as send:
                code = tmux_queue.run_queue_after_status(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")
        self.assertEqual(status["matched_required_rows"], ["run_cfg=configs/msec.toml,status=done"])
        send.assert_not_called()

    def test_watch_records_timeout_after_capture(self) -> None:
        output = "\n".join(f"line {index}" for index in range(15))
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="watch",
                name=None,
                pane="%1",
                interval=0.001,
                capture_lines=10,
                status_lines=2,
                status_max_chars=1200,
                status_file=None,
                timeout_seconds=0.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text", return_value=output):
                code = tmux_queue.run_watch(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "watch"))
            record, record_error = tmux_state.read_json(tmux_state.job_path(paths, "watch"))
            log_text = tmux_state.log_path(paths, "watch").read_text(encoding="utf-8")

        self.assertIsNone(error)
        self.assertIsNone(record_error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "timeout")
        self.assertEqual(status["last_output"], "line 13\nline 14")
        self.assertEqual(status["status_lines"], 2)
        self.assertEqual(status["status_max_chars"], 1200)
        assert record is not None
        self.assertEqual(record["status_lines"], 2)
        self.assertEqual(record["status_max_chars"], 1200)
        self.assertEqual(log_text, output)

    def test_watch_records_failed_status_for_blank_observed_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                job_id="watch",
                name=None,
                pane="%1",
                interval=0.001,
                capture_lines=10,
                status_file=" \n\t ",
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text") as capture_text:
                code = tmux_queue.run_watch(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "watch"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("watch requires nonblank --status-file when provided", status["last_output"])
        capture_text.assert_not_called()

    def test_watch_records_failed_status_when_observed_file_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observed = Path(tmp) / "observed.txt"
            observed.mkdir()
            args = argparse.Namespace(
                job_id="watch",
                name=None,
                pane="%1",
                interval=0.001,
                capture_lines=10,
                status_file=str(observed),
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text", return_value="latest output"):
                code = tmux_queue.run_watch(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "watch"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not read observed status file", status["last_output"])
        self.assertEqual(status["observed_status_file"], str(observed.resolve()))

    def test_watch_records_failed_status_when_log_file_unwritable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.log_path(paths, "watch").mkdir()
            args = argparse.Namespace(
                job_id="watch",
                name=None,
                pane="%1",
                interval=0.001,
                capture_lines=10,
                status_file=None,
                timeout_seconds=1.0,
                workspace=tmp,
                state_dir=None,
            )
            with mock.patch.object(tmux_queue.tmux_control, "capture_text", return_value="latest output"):
                code = tmux_queue.run_watch(args)

            status, error = tmux_state.read_json(tmux_state.status_path(paths, "watch"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        assert status is not None
        self.assertEqual(status["status"], "failed")
        self.assertIn("could not write watch log file", status["last_output"])
        self.assertEqual(status["error"], status["last_output"])

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

    def test_queue_after_idle_records_system_exit_from_idle_check(self) -> None:
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
            with mock.patch.object(tmux_queue.tmux_control, "idle_shell_check", side_effect=SystemExit(1)):
                code = tmux_queue.run_queue_after_idle(args)

            paths = tmux_state.state_paths(tmp)
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "queue"))

        self.assertIsNone(error)
        self.assertEqual(code, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["last_output"], "SystemExit(1)")

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

    def test_terminalize_sanitizes_legacy_status_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job-with-space"),
                {
                    "id": "job with space",
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pid_matches": True,
                    "pid_running": True,
                    "pane_id": "%1",
                    "stale": False,
                    "stale_reason": "legacy stale reason",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )
            tmux_state.atomic_write_json(
                tmux_state.status_path(paths, "job-with-space"),
                {
                    "job_id": "job with space",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "updated_at": tmux_state.utc_now(),
                },
            )

            args = argparse.Namespace(action="queue-after-idle", name=None, pane="%1")
            tmux_queue.terminalize_active_worker(
                paths,
                args,
                job_id="job-with-space",
                terminal_status="cancelled",
                last_output="cancelled before command submission",
                extra={"kind": "wrong", "status": "failed"},
            )

            record = tmux_state.read_json(tmux_state.job_path(paths, "job-with-space"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "job-with-space"))[0]

        assert record is not None
        assert status is not None
        self.assertEqual(record["id"], "job-with-space")
        self.assertEqual(record["job_id"], "job-with-space")
        self.assertEqual(record["kind"], "queue-after-idle")
        self.assertEqual(record["status"], "cancelled")
        self.assertNotIn("pid_matches", record)
        self.assertNotIn("pid_running", record)
        self.assertNotIn("stale", record)
        self.assertNotIn("stale_reason", record)
        self.assertEqual(status["id"], "job-with-space")
        self.assertEqual(status["kind"], "queue-after-idle")
        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["event_id"])

    def test_terminalize_active_record_even_with_stale_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "job"),
                {
                    "job_id": "job",
                    "kind": "queue-after-idle",
                    "status": "waiting_pane_idle",
                    "pid": 12345,
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )
            old_status = tmux_state.build_status(
                kind="queue-after-idle",
                item_id="job",
                attempt=1,
                name=None,
                status="submitted",
                pane_id="%1",
                command_preview_text="old command",
                cwd=tmp,
                status_file=tmux_state.status_path(paths, "job"),
                log_file=tmux_state.log_path(paths, "job"),
                exit_code=0,
                last_output="old terminal status",
            )
            tmux_state.atomic_write_json(tmux_state.status_path(paths, "job"), old_status)

            args = argparse.Namespace(action="queue-after-idle", name=None, pane="%1")
            tmux_queue.terminalize_active_worker(
                paths,
                args,
                job_id="job",
                terminal_status="cancelled",
                last_output="cancelled before first fresh status write",
            )

            record = tmux_state.read_json(tmux_state.job_path(paths, "job"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "job"))[0]

        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["last_output"], "cancelled before first fresh status write")
        self.assertTrue(status["event_id"])

    def test_run_worker_safely_records_keyboard_interrupt_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(action="queue-after-idle", job_id="job", name=None, pane="%1")
            started_at = tmux_state.utc_now()
            tmux_queue.write_worker_record(
                paths,
                args,
                kind="queue-after-idle",
                status="waiting_pane_idle",
                command_text="echo next",
            )
            tmux_queue.write_worker_status(
                paths,
                args,
                kind="queue-after-idle",
                status="waiting_pane_idle",
                started_at=started_at,
                command_text="echo next",
                last_output="waiting",
            )

            def interrupted(_args: argparse.Namespace) -> int:
                raise KeyboardInterrupt

            code = tmux_queue.run_worker_safely(args, paths, "job", interrupted)
            record = tmux_state.read_json(tmux_state.job_path(paths, "job"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "job"))[0]

        self.assertEqual(code, 1)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["last_output"], "cancelled before command submission")
        self.assertNotIn("error", status)

    def test_run_worker_safely_records_sigterm_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            args = argparse.Namespace(action="watch", job_id="watch", name=None, pane="%1")
            tmux_queue.write_worker_record(paths, args, kind="watch", status="running")

            def terminated(_args: argparse.Namespace) -> int:
                raise tmux_queue.WorkerTerminatedBySignal(tmux_queue.signal.SIGTERM)

            code = tmux_queue.run_worker_safely(args, paths, "watch", terminated)
            record = tmux_state.read_json(tmux_state.job_path(paths, "watch"))[0]
            status = tmux_state.read_json(tmux_state.status_path(paths, "watch"))[0]

        self.assertEqual(code, 1)
        assert record is not None
        assert status is not None
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["last_output"], "watch cancelled")
        self.assertNotIn("error", status)


if __name__ == "__main__":
    unittest.main()
