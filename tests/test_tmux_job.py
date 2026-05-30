from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import tmux_state


JOB = Path(__file__).resolve().parents[1] / "scripts" / "tmux_job.py"


class TmuxJobTests(unittest.TestCase):
    def process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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

    def test_interrupt_kills_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pid_file = workspace / "grandchild.pid"
            command_file = workspace / "command.sh"
            command_file.write_text(
                shlex.join(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,time,sys; "
                            "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
                            "time.sleep(60)"
                        ),
                        str(pid_file),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(JOB),
                    "exec",
                    "--job-id",
                    "interrupt",
                    "--attempt",
                    "1",
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
                start_new_session=True,
            )
            grandchild_pid: int | None = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_file.exists():
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists(), "grandchild did not record its pid")
                grandchild_pid = int(pid_file.read_text(encoding="utf-8"))

                proc.send_signal(signal.SIGINT)
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 130, msg=f"stdout={stdout!r} stderr={stderr!r}")

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self.process_alive(grandchild_pid):
                    time.sleep(0.05)
                self.assertFalse(self.process_alive(grandchild_pid))
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            proc.kill()
                        except (OSError, ProcessLookupError):
                            pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                if grandchild_pid is not None and self.process_alive(grandchild_pid):
                    try:
                        os.kill(grandchild_pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass


if __name__ == "__main__":
    unittest.main()
