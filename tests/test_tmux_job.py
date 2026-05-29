from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_state


JOB = Path(__file__).resolve().parents[1] / "scripts" / "tmux_job.py"


class TmuxJobTests(unittest.TestCase):
    def run_job(self, command: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            command_file = workspace / "command.sh"
            command_file.write_text(command, encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(JOB),
                    "exec",
                    "--job-id",
                    "job",
                    "--attempt",
                    "1",
                    "--pane",
                    "%1",
                    "--command-file",
                    str(command_file),
                    "--workspace",
                    str(workspace),
                    "--cwd",
                    str(workspace),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            paths = tmux_state.state_paths(str(workspace))
            status, error = tmux_state.read_json(tmux_state.status_path(paths, "job"))
            self.assertIsNone(error)
            return result, status

    def test_success_captures_stdout_stderr_and_non_ascii(self) -> None:
        result, status = self.run_job("printf 'hello 한글\\n'; printf 'warn\\n' >&2")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(status["status"], "succeeded")
        self.assertIn("hello 한글", status["last_output"])
        self.assertIn("warn", status["last_output"])

    def test_failure_preserves_exit_code(self) -> None:
        result, status = self.run_job("printf fail; exit 7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 7)

    def test_newline_command_runs_as_shell_script(self) -> None:
        result, status = self.run_job("value='quoted value'\nprintf \"$value\\n\"")
        self.assertEqual(result.returncode, 0)
        self.assertIn("quoted value", status["last_output"])


if __name__ == "__main__":
    unittest.main()
