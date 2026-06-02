from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
        subprocess.run(["tmux", "kill-server"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for attempt in range(5):
            try:
                self.tmp.cleanup()
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.1)

    def tmux(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def run_control_process(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONTROL), *args],
            cwd=str(ROOT),
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def run_control(self, args: list[str]) -> dict[str, object]:
        result = self.run_control_process(args)
        if result.returncode != 0:
            self.fail(f"tmux_control failed with {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}")
        return json.loads(result.stdout)

    def wait_for_pane_cwd(self, pane: str, expected: Path) -> dict[str, object]:
        expected_path = expected.resolve()
        last: dict[str, object] | None = None
        for _ in range(50):
            last = self.run_control(["current", "--target", pane])
            current_path = Path(str(last["current"]["current_path"])).resolve()
            if current_path == expected_path:
                return last
            subprocess.run(["sleep", "0.1"], check=True)
        self.fail(f"pane cwd did not become {expected_path}: last={last!r}")

    def start_session(self) -> str:
        self.tmux(["new-session", "-d", "-s", self.session, "-c", str(self.workspace)])
        return self.tmux(["display-message", "-p", "-t", self.session, "#{pane_id}"]).stdout.strip()

    def test_spawn_run_and_capture(self) -> None:
        self.start_session()
        target = self.tmux(["display-message", "-p", "-t", self.session, "#{session_name}:#{window_index}"]).stdout.strip()
        pane = self.run_control(["spawn", "--target", target, "--cwd", str(self.workspace)])["pane_id"]
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
        current = self.wait_for_pane_cwd(pane, self.workspace)
        self.assertEqual(Path(current["current"]["current_path"]).resolve(), self.workspace.resolve())

    def test_new_window_outside_tmux_first_window_uses_requested_name(self) -> None:
        result = self.run_control(
            ["new-window", "--cwd", str(self.workspace), "--workspace", str(self.workspace), "--name", "requested-name"]
        )
        window_id = str(result["window_id"])
        name = self.tmux(["display-message", "-p", "-t", window_id, "#{window_name}"]).stdout.strip()

        self.assertEqual(name, "requested-name")

    def test_send_require_idle_shell_rejects_busy_pane(self) -> None:
        pane = self.start_session()
        output = self.workspace / "busy.out"
        self.tmux(["send-keys", "-t", pane, "-l", "sleep 5"])
        self.tmux(["send-keys", "-t", pane, "Enter"])
        subprocess.run(["sleep", "0.2"], check=True)

        result = self.run_control_process(
            [
                "send",
                "--pane",
                pane,
                "--command",
                "printf busy > busy.out",
                "--require-idle-shell",
                "--enter",
            ]
        )
        data = json.loads(result.stdout)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(data["sent_to_pane"])
        self.assertIn("reason", data)
        self.assertFalse(output.exists())

    def test_send_require_idle_shell_rejects_killed_pane(self) -> None:
        pane = self.start_session()
        self.tmux(["kill-pane", "-t", pane])

        result = self.run_control_process(
            [
                "send",
                "--pane",
                pane,
                "--command",
                "printf missing > missing.out",
                "--require-idle-shell",
                "--enter",
            ]
        )
        data = json.loads(result.stdout)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(data["sent_to_pane"])
        self.assertEqual(data["reason"], "pane could not be resolved")
        self.assertFalse((self.workspace / "missing.out").exists())


if __name__ == "__main__":
    unittest.main()
