from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts" / "tmux_control.py"


@unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
class TmuxIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.tmux_tmp = Path(self.tmp.name) / "tmux"
        self.tmux_tmp.mkdir()
        self.env = os.environ.copy()
        self.env.pop("TMUX", None)
        self.env["TMUX_TMPDIR"] = str(self.tmux_tmp)
        self.session = f"codex-test-{os.getpid()}"

    def tearDown(self) -> None:
        subprocess.run(["tmux", "kill-session", "-t", self.session], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["tmux", "kill-session", "-t", f"codex-{self.workspace.name}"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.tmp.cleanup()

    def run_control(self, args: list[str]) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(CONTROL), *args],
            cwd=str(ROOT),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def test_spawn_run_and_capture(self) -> None:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session, "-c", str(self.workspace)],
            env=self.env,
            check=True,
        )
        pane = self.run_control(["spawn", "--target", f"{self.session}:1", "--cwd", str(self.workspace)])["pane_id"]
        run = self.run_control(
            [
                "run",
                "--pane",
                str(pane),
                "--command",
                "printf integration-ok",
                "--job-id",
                "integration",
                "--workspace",
                str(self.workspace),
            ]
        )
        self.assertTrue(run["sent"])

        status_path = Path(str(run["status_path"]))
        for _ in range(50):
            if status_path.exists():
                data = json.loads(status_path.read_text(encoding="utf-8"))
                if data.get("status") == "succeeded":
                    break
            subprocess.run(["sleep", "0.1"], check=True)
        else:
            self.fail("job did not finish")

        capture = self.run_control(["capture", "--pane", str(pane), "--lines", "50", "--strip-ansi", "--max-chars", "2000"])
        self.assertIn("integration-ok", capture["output"])

    def test_new_window_outside_tmux_uses_requested_cwd(self) -> None:
        result = self.run_control(["new-window", "--cwd", str(self.workspace), "--workspace", str(self.workspace)])
        self.assertEqual(result["session_name"], f"codex-{self.workspace.name}")
        pane = str(result["pane_id"])
        current = self.run_control(["current", "--target", pane])
        self.assertEqual(Path(current["current"]["current_path"]).resolve(), self.workspace.resolve())


if __name__ == "__main__":
    unittest.main()
