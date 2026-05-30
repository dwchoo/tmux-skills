from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_state


HOOK = Path(__file__).resolve().parents[1] / "scripts" / "codex_tmux_hook.py"


class CodexTmuxHookTests(unittest.TestCase):
    def run_hook(self, args: list[str], stdin: dict[str, object] | None = None) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(HOOK), *args],
            input=json.dumps(stdin or {}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def write_terminal_status(self, workspace: str) -> dict[str, object]:
        paths = tmux_state.state_paths(workspace)
        tmux_state.ensure_state_dirs(paths)
        status_file = tmux_state.status_path(paths, "job")
        status = tmux_state.build_status(
            kind="job",
            item_id="job",
            attempt=1,
            name="training",
            status="failed",
            pane_id="%1",
            command_preview_text="python train.py",
            cwd=workspace,
            status_file=status_file,
            log_file=tmux_state.log_path(paths, "job"),
            exit_code=2,
            last_output="Traceback",
        )
        return tmux_state.write_status(status_file, status)

    def test_context_outputs_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp])
        self.assertIn("hookSpecificOutput", output)
        self.assertIn("training: failed", output["hookSpecificOutput"]["additionalContext"])

    def test_stop_blocks_once_and_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            first = self.run_hook(["stop", "--workspace", tmp], {})
            second = self.run_hook(["stop", "--workspace", tmp], {})
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second, {})

    def test_stop_hook_active_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.write_terminal_status(tmp)
            output = self.run_hook(["stop", "--workspace", tmp], {"stop_hook_active": True})
        self.assertEqual(output, {})

    def test_context_and_stop_include_ready_task_without_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            self.write_terminal_status(tmp)
            task = tmux_state.build_task(
                task_id="follow-up",
                instruction="Inspect the failed training log",
                summary="inspect failure",
                intent=None,
                after_job_id="job",
                after_event_id=None,
                trigger_on="failed",
            )
            tmux_state.write_task(paths, task)

            context = self.run_hook(["context", "--event", "SessionStart", "--workspace", tmp], {})
            stop = self.run_hook(["stop", "--workspace", tmp], {})
            stored = tmux_state.read_json(tmux_state.task_path(paths, "follow-up"))[0]

        self.assertIn("ready task follow-up", context["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(stop["decision"], "block")
        self.assertIn("Inspect the failed training log", stop["reason"])
        self.assertEqual(stored["status"], "waiting")

    def test_context_includes_active_managed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            tmux_state.atomic_write_json(
                tmux_state.job_path(paths, "watch"),
                {
                    "job_id": "watch",
                    "kind": "watch",
                    "status": "running",
                    "pane_id": "%1",
                    "heartbeat_at": tmux_state.utc_now(),
                    "updated_at": tmux_state.utc_now(),
                },
            )

            output = self.run_hook(["context", "--event", "UserPromptSubmit", "--workspace", tmp], {})

        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("managed job watch: running kind=watch pane=%1 heartbeat=", context)


if __name__ == "__main__":
    unittest.main()
