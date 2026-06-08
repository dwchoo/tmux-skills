from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_mcp_server  # noqa: E402
import tmux_manager  # noqa: E402
import tmux_state  # noqa: E402


class TmuxMcpServerTests(unittest.TestCase):
    def test_tools_list_exposes_required_manager_tools(self) -> None:
        response = tmux_mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response is not None
        tool_names = {tool["name"] for tool in response["result"]["tools"]}

        self.assertTrue(
            {
                "manager.start",
                "manager.submit",
                "manager.status",
                "manager.observe",
                "manager.ack",
                "manager.run_next",
                "manager.cancel",
            }.issubset(tool_names)
        )

    def test_manager_status_tool_returns_redacted_structured_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            workspace = Path(tmp_name) / "workspace"
            workspace.mkdir()
            paths = tmux_manager.manager_paths(str(workspace))
            record = tmux_manager.build_manager_record(
                manager_id="manager-one",
                manager_pane_id="%3",
                worker_pane_id="%2",
                pending_job=None,
                notify={"mode": "none"},
                workspace=str(workspace),
                state_dir=str(paths["root"]),
            )
            record["current_job_id"] = "job-one"
            status = tmux_state.build_status(
                kind="job",
                item_id="job-one",
                attempt=1,
                name=None,
                status="succeeded",
                pane_id="%2",
                command_preview_text="echo ok",
                cwd=str(workspace),
                status_file=tmux_state.status_path(paths, "job-one"),
                log_file=tmux_state.log_path(paths, "job-one"),
                exit_code=0,
                last_output="SECRET OUTPUT",
                manager_id="manager-one",
                manager_sequence=1,
            )
            status = tmux_state.write_status(tmux_state.status_path(paths, "job-one"), status)
            record = tmux_manager.transition_terminal(record, paths=paths, status=status)
            tmux_manager.write_manager_record(paths, record)

            response = tmux_mcp_server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "manager.status",
                        "arguments": {"manager_id": "manager-one", "workspace": str(workspace)},
                    },
                }
            )

        assert response is not None
        content = response["result"]["structuredContent"]
        serialized = response["result"]["content"][0]["text"]
        self.assertTrue(content["redacted"])
        self.assertNotIn("%2", serialized)
        self.assertNotIn("SECRET OUTPUT", serialized)
        self.assertNotIn("status/job-one.json", serialized)


if __name__ == "__main__":
    unittest.main()
