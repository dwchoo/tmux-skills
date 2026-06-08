from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import codex_tmux_hook  # noqa: E402
import tmux_state  # noqa: E402


class CodexTmuxHookTests(unittest.TestCase):
    def write_terminal_status(self, paths: dict[str, Path], job_id: str, *, ended_at: str) -> dict[str, object]:
        status_file = tmux_state.status_path(paths, job_id)
        status = tmux_state.build_status(
            kind="job",
            item_id=job_id,
            attempt=1,
            name=None,
            status="succeeded",
            pane_id="%2",
            command_preview_text="echo ok",
            cwd=str(paths["workspace"]),
            status_file=status_file,
            log_file=tmux_state.log_path(paths, job_id),
            exit_code=0,
            last_output="ok",
        )
        status["ended_at"] = ended_at
        status["updated_at"] = ended_at
        return tmux_state.write_status(status_file, status)

    def write_manager_record(self, paths: dict[str, Path], status: dict[str, object]) -> None:
        managers_dir = paths["root"] / "managers"
        managers_dir.mkdir(parents=True, exist_ok=True)
        job_id = str(status["id"])
        event_id = str(status["event_id"])
        tmux_state.atomic_write_json(
            managers_dir / "manager-one.json",
            {
                "manager_id": "manager-one",
                "jobs": {
                    job_id: {
                        "job_id": job_id,
                        "status_path": status["status_path"],
                        "terminal_event_id": event_id,
                    }
                },
                "events": {
                    event_id: {
                        "event_id": event_id,
                        "job_id": job_id,
                        "status_path": status["status_path"],
                        "status": "succeeded",
                    }
                },
                "notifications": [],
            },
        )

    def test_context_omits_manager_owned_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            self.write_manager_record(paths, status)

            result = codex_tmux_hook.context(SimpleNamespace(workspace=tmp, state_dir=None))

        self.assertEqual(result, {})

    def test_stop_acknowledges_manager_owned_terminal_status_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            self.write_manager_record(paths, status)

            result = codex_tmux_hook.stop(SimpleNamespace(workspace=tmp, state_dir=None), {})

            ack_path = tmux_state.ack_path(paths, str(status["event_id"]))
            self.assertTrue(ack_path.exists())

        self.assertEqual(result, {})

    def test_stop_acknowledges_manager_marked_terminal_status_without_manager_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            status["manager_owned"] = True
            status["manager_id"] = "manager-one"
            status["manager_sequence"] = 1
            status = tmux_state.write_status(Path(str(status["status_path"])), status)

            result = codex_tmux_hook.stop(SimpleNamespace(workspace=tmp, state_dir=None), {})

            ack_path = tmux_state.ack_path(paths, str(status["event_id"]))
            self.assertTrue(ack_path.exists())

        self.assertEqual(result, {})

    def test_stop_blocks_non_manager_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "raw-job", ended_at="2026-06-06T00:00:01Z")

            result = codex_tmux_hook.stop(SimpleNamespace(workspace=tmp, state_dir=None), {})

            ack_path = tmux_state.ack_path(paths, str(status["event_id"]))
            self.assertTrue(ack_path.exists())

        self.assertEqual(result["decision"], "block")
        self.assertIn("tmux-skills observed a terminal event: raw-job", result["reason"])

    def test_pre_tool_use_blocks_manager_owned_capture_without_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            self.write_manager_record(paths, status)
            manager_path = paths["managers"] / "manager-one.json"
            record = tmux_state.read_json(manager_path)[0]
            record["status"] = "running"
            record["active_job_ids"] = ["manager-job"]
            record["worker_pane_ids"] = ["%2"]
            tmux_state.atomic_write_json(manager_path, record)

            result = codex_tmux_hook.pre_tool_use(
                SimpleNamespace(workspace=tmp, state_dir=None),
                {"tool_input": {"command": "python scripts/tmux_control.py capture --pane %2 --lines 20"}},
            )

        self.assertEqual(result["decision"], "block")
        self.assertIn("must not be polled", result["reason"])

    def test_pre_tool_use_allows_manual_override_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            self.write_manager_record(paths, status)
            manager_path = paths["managers"] / "manager-one.json"
            record = tmux_state.read_json(manager_path)[0]
            record["status"] = "running"
            record["active_job_ids"] = ["manager-job"]
            record["worker_pane_ids"] = ["%2"]
            tmux_state.atomic_write_json(manager_path, record)

            result = codex_tmux_hook.pre_tool_use(
                SimpleNamespace(workspace=tmp, state_dir=None),
                {"tool_input": {"command": "python scripts/tmux_control.py capture --pane %2 --manual-override --reason inspect"}},
            )

        self.assertEqual(result, {})

    def test_context_injects_no_polling_policy_for_active_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = tmux_state.state_paths(tmp)
            tmux_state.ensure_state_dirs(paths)
            status = self.write_terminal_status(paths, "manager-job", ended_at="2026-06-06T00:00:01Z")
            self.write_manager_record(paths, status)
            manager_path = paths["managers"] / "manager-one.json"
            record = tmux_state.read_json(manager_path)[0]
            record["status"] = "running"
            record["active_job_ids"] = ["manager-job"]
            record["worker_pane_ids"] = ["%2"]
            tmux_state.atomic_write_json(manager_path, record)

            result = codex_tmux_hook.context(SimpleNamespace(workspace=tmp, state_dir=None))

        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("active manager-owned work", context)
        self.assertIn("do not poll manager status", context)


if __name__ == "__main__":
    unittest.main()
