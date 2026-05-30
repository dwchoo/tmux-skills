from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_control
import tmux_state


CONTROL = Path(__file__).resolve().parents[1] / "scripts" / "tmux_control.py"


class TmuxTaskTests(unittest.TestCase):
    def write_status(self, paths: dict[str, Path], job_id: str, status: str = "succeeded") -> dict[str, object]:
        status_file = tmux_state.status_path(paths, job_id)
        data = tmux_state.build_status(
            kind="job",
            item_id=job_id,
            attempt=1,
            name="job",
            status=status,
            pane_id="%1",
            command_preview_text="echo ok",
            cwd=str(paths["workspace"]),
            status_file=status_file,
            log_file=tmux_state.log_path(paths, job_id),
            exit_code=0 if status == "succeeded" else 1,
            last_output="ok",
        )
        return tmux_state.write_status(status_file, data)

    def cli(self, args: list[str], workspace: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTROL), *args, "--workspace", workspace],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_task_add_next_claim_done_blocked_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")

            add = self.cli(
                [
                    "task",
                    "add",
                    "--after-job",
                    "job",
                    "--trigger-on",
                    "succeeded",
                    "--instruction",
                    "Inspect the result",
                    "--summary",
                    "inspect",
                ],
                tmp,
            )
            task = json.loads(add.stdout)
            task_id = task["task_id"]

            next_result = self.cli(["task", "next", "--json"], tmp)
            self.assertEqual(json.loads(next_result.stdout)["task_id"], task_id)

            claimed = json.loads(self.cli(["task", "claim", "--task-id", task_id], tmp).stdout)
            self.assertEqual(claimed["status"], "in_progress")

            done = json.loads(self.cli(["task", "done", "--task-id", task_id, "--note", "finished"], tmp).stdout)
            self.assertEqual(done["status"], "done")

            blocked_add = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Blocked task",
                    ],
                    tmp,
                ).stdout
            )
            blocked = json.loads(
                self.cli(["task", "blocked", "--task-id", blocked_add["task_id"], "--note", "needs input"], tmp).stdout
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["blocked_reason"], "needs input")

            cancelled_add = json.loads(
                self.cli(
                    [
                        "task",
                        "add",
                        "--after-job",
                        "job",
                        "--trigger-on",
                        "succeeded",
                        "--instruction",
                        "Cancel task",
                    ],
                    tmp,
                ).stdout
            )
            cancelled = json.loads(self.cli(["task", "cancel", "--task-id", cancelled_add["task_id"]], tmp).stdout)
            self.assertEqual(cancelled["status"], "cancelled")

    def test_task_load_is_read_only_and_for_skill_has_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_status(paths, "job")
            task = tmux_state.build_task(
                task_id="task",
                instruction="Continue analysis",
                summary="continue",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="succeeded",
            )
            tmux_state.write_task(paths, task)
            before = tmux_state.read_json(tmux_state.task_path(paths, "task"))[0]

            output = self.cli(["task", "load", "--for-skill"], tmp).stdout
            after = tmux_state.read_json(tmux_state.task_path(paths, "task"))[0]

            self.assertEqual(before, after)
            for heading in (
                "What happened",
                "Current state",
                "Next actionable instruction",
                "Evidence files",
                "Safe commands to inspect",
                "Do not auto-run",
            ):
                self.assertIn(heading, output)
            self.assertIn("Continue analysis", output)

    def test_json_load_handles_old_status_and_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            (paths["status"] / "old.json").write_text(json.dumps({"id": "old", "status": "succeeded"}), encoding="utf-8")
            (paths["status"] / "bad.json").write_text("{", encoding="utf-8")

            result = self.cli(["task", "load", "--json"], tmp)
            data = json.loads(result.stdout)
            self.assertEqual(data["recent_jobs"][0]["id"], "old")
            self.assertEqual(len(data["errors"]), 1)

    def test_load_tasks_tolerates_corrupt_task_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            good = tmux_state.build_task(
                task_id="good",
                instruction="Keep loading",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            tmux_state.write_task(paths, good)
            (paths["tasks"] / "bad.json").write_text(
                json.dumps({"task_id": "bad", "version": "not-an-int", "instruction": "x"}),
                encoding="utf-8",
            )

            tasks, errors = tmux_state.load_tasks(paths["root"])
            by_id = {task["task_id"]: task for task in tasks}
            self.assertEqual(errors, [])
            self.assertIn("good", by_id)
            self.assertEqual(by_id["bad"]["version"], tmux_state.TASK_VERSION)

            state = tmux_state.load_task_state(paths)
            state_by_id = {task["task_id"]: task for task in state["tasks"]}
            self.assertEqual(state["errors"], [])
            self.assertIn("good", state_by_id)
            self.assertEqual(state_by_id["bad"]["version"], tmux_state.TASK_VERSION)

    def test_stale_in_progress_and_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds").replace("+00:00", "Z")
            task = tmux_state.build_task(
                task_id="stale",
                instruction="Resume stale",
                summary=None,
                intent=None,
                after_job_id=None,
                after_event_id=None,
                trigger_on="terminal",
            )
            task["status"] = "in_progress"
            task["claimed_at"] = old
            tmux_state.write_task(paths, task)

            load = json.loads(self.cli(["task", "load", "--json"], tmp).stdout)
            self.assertEqual(load["blocked"][0]["task_id"], "stale")
            claimed = json.loads(self.cli(["task", "claim", "--task-id", "stale", "--reclaim-stale"], tmp).stdout)
            self.assertEqual(claimed["status"], "in_progress")

    def test_run_next_instruction_creates_waiting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sent: dict[str, str] = {}

            def fake_send(args: argparse.Namespace) -> dict[str, object]:
                sent["text"] = args.command_text
                return {"sent_to_pane": True}

            args = argparse.Namespace(
                pane="%1",
                command_text="printf ok",
                command_file=None,
                job_id="job-next",
                name="job",
                cwd=tmp,
                workspace=tmp,
                state_dir=None,
                require_idle_shell=False,
                next_instruction="Summarize the result",
                next_instruction_file=None,
                next_on="succeeded",
            )
            with mock.patch.object(tmux_control, "send", side_effect=fake_send):
                result = tmux_control.run_job(args)

            self.assertTrue(result["next_task"])
            task_id = result["next_task"]["task_id"]
            paths = tmux_state.state_paths(tmp)
            task = tmux_state.read_json(tmux_state.task_path(paths, task_id))[0]
            self.assertEqual(task["status"], "waiting")
            self.assertEqual(task["after_job_id"], "job-next")
            self.assertEqual(task["instruction"], "Summarize the result")


if __name__ == "__main__":
    unittest.main()
