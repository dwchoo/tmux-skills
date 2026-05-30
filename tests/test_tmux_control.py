from __future__ import annotations

import argparse
import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_control
import tmux_state


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
            "/dev/ttys001",
        ]
        with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
            parsed = tmux_control.parse_current_line(tmux_control.FIELD_SEP.join(parts))

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["window_name"], "window\tname")
        self.assertEqual(parsed["current_path"], "/tmp/path\twith-tab")
        self.assertEqual(parsed["title"], "title\twith-tab")

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
            "/dev/ttys001",
        ]
        with mock.patch.object(tmux_control, "descendant_processes", return_value=(0, [], 0)):
            parsed = tmux_control.parse_pane_line(tmux_control.FIELD_SEP.join(parts), current_pane_id="%5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["window_name"], "window\tname")
        self.assertEqual(parsed["current_path"], "/tmp/path\twith-tab")
        self.assertEqual(parsed["title"], "title\twith-tab")
        self.assertTrue(parsed["current"])

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
                    "pid": 0,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )

            result = tmux_control.queue_after_idle(args)

        self.assertFalse(result["started"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["existing_job_id"], "first")
        self.assertEqual(result["dedupe_key"], dedupe_key)

    def test_concurrent_same_dedupe_creation_starts_only_one_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counter = {"pid": 20000}
            counter_lock = threading.Lock()

            def fake_popen(*_args: object, **_kwargs: object) -> object:
                with counter_lock:
                    counter["pid"] += 1
                    pid = counter["pid"]

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

            with mock.patch.object(tmux_control.subprocess, "Popen", side_effect=fake_popen):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(start, ["first", "second"]))

        self.assertEqual(sum(1 for result in results if result.get("started")), 1)
        self.assertEqual(sum(1 for result in results if result.get("duplicate")), 1)

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
                    "pid": 0,
                    "pane_id": "%1",
                    "dedupe_key": dedupe_key,
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                    "check_interval_seconds": 1,
                },
            )
            with mock.patch.object(tmux_control.subprocess, "Popen", return_value=Proc()):
                result = tmux_control.queue_after_idle(args)

            record = tmux_state.read_json(tmux_state.job_path(paths, "second"))[0]

        self.assertTrue(result["started"])
        self.assertTrue(record["duplicate_allowed"])
        self.assertEqual(record["duplicate_of"], "first")

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
                    "pane_id": "%1",
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

    def test_queue_aliases_parse_to_existing_dests(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
