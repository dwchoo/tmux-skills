from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "run_managed_job.sh"


class RunManagedJobTests(unittest.TestCase):
    def run_wrapper(
        self,
        job_dir: Path,
        *command: str,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(WRAPPER), str(job_dir), *command],
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_success_writes_final_artifacts_and_combined_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            result = self.run_wrapper(
                job_dir,
                "bash",
                "-c",
                "printf 'out\\n'; printf 'err\\n' >&2",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((job_dir / "exitcode").read_text(encoding="utf-8").strip(), "0")
            status = self.read_json(job_dir / "status.json")
            self.assertEqual(status["state"], "succeeded")
            self.assertEqual(status["exitcode"], 0)
            self.assertIn("out\n", (job_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertIn("err\n", (job_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertFalse((job_dir / "stderr.log").exists())
            self.assertTrue((job_dir / "command.sh").read_text(encoding="utf-8").endswith("\n"))
            self.assertRegex((job_dir / "pid").read_text(encoding="utf-8").strip(), r"^[0-9]+$")
            self.assertRegex((job_dir / "started_at").read_text(encoding="utf-8").strip(), r"Z$")
            self.assertRegex((job_dir / "finished_at").read_text(encoding="utf-8").strip(), r"Z$")

    def test_failure_preserves_child_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            result = self.run_wrapper(job_dir, "bash", "-c", "printf fail; exit 7")

            self.assertEqual(result.returncode, 7)
            self.assertEqual((job_dir / "exitcode").read_text(encoding="utf-8").strip(), "7")
            status = self.read_json(job_dir / "status.json")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exitcode"], 7)

    def test_running_state_is_observable_before_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            process = subprocess.Popen(
                [
                    "bash",
                    str(WRAPPER),
                    str(job_dir),
                    "bash",
                    "-c",
                    "sleep 0.6",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.time() + 3
                status = None
                while time.time() < deadline:
                    if (job_dir / "status.json").exists():
                        status = self.read_json(job_dir / "status.json")
                        if status.get("state") == "running":
                            break
                    time.sleep(0.02)
                self.assertIsNotNone(status)
                assert status is not None
                self.assertEqual(status["state"], "running")
                self.assertIsNone(process.poll())
            finally:
                stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            self.assertEqual(self.read_json(job_dir / "status.json")["state"], "succeeded")

    def test_pid_is_direct_child_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job"
            pid_probe = root / "child-pid"
            result = self.run_wrapper(
                job_dir,
                "bash",
                "-c",
                'echo $$ > "$1"',
                "_",
                str(pid_probe),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (job_dir / "pid").read_text(encoding="utf-8").strip(),
                pid_probe.read_text(encoding="utf-8").strip(),
            )

    def test_command_file_reruns_quoted_argv_from_same_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir()
            job_dir = root / "job"
            command = [
                "bash",
                "-c",
                'printf "%s\\n" "$@" > rerun.out',
                "_",
                "space value",
                "single'quote",
                'double"quote',
            ]

            result = self.run_wrapper(job_dir, *command, cwd=cwd)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = "space value\nsingle'quote\ndouble\"quote\n"
            self.assertEqual((cwd / "rerun.out").read_text(encoding="utf-8"), expected)

            (cwd / "rerun.out").unlink()
            rerun = subprocess.run(
                ["bash", str(job_dir / "command.sh")],
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertEqual((cwd / "rerun.out").read_text(encoding="utf-8"), expected)

    def test_missing_args_exit_2_without_artifacts(self) -> None:
        missing_all = subprocess.run(
            ["bash", str(WRAPPER)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(missing_all.returncode, 2)

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            missing_command = subprocess.run(
                ["bash", str(WRAPPER), str(job_dir)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(missing_command.returncode, 2)
            self.assertFalse(job_dir.exists())
            for name in ("status.json", "stdout.log", "command.sh", "pid"):
                self.assertFalse((job_dir / name).exists())

    def test_mkdir_failure_exits_125_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_file = root / "not-a-directory"
            parent_file.write_text("x", encoding="utf-8")
            job_dir = parent_file / "job"
            result = self.run_wrapper(job_dir, "bash", "-c", "true")

            self.assertEqual(result.returncode, 125)
            self.assertFalse((job_dir / "status.json").exists())

    def test_pre_command_command_file_failure_exits_125_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "command.sh").mkdir()
            side_effect = root / "side-effect"

            result = self.run_wrapper(job_dir, "bash", "-c", f"touch {side_effect!s}")

            self.assertEqual(result.returncode, 125)
            self.assertFalse(side_effect.exists())

    def test_stdout_setup_failure_exits_125_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "stdout.log").mkdir()
            side_effect = root / "side-effect"

            result = self.run_wrapper(job_dir, "bash", "-c", f"touch {side_effect!s}")

            self.assertEqual(result.returncode, 125)
            self.assertFalse(side_effect.exists())

    def test_final_artifact_failure_exits_125_after_child_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_dir = root / "job"
            side_effect = root / "side-effect"
            result = self.run_wrapper(
                job_dir,
                "bash",
                "-c",
                'mkdir "$1/exitcode"; touch "$2"',
                "_",
                str(job_dir),
                str(side_effect),
            )

            self.assertEqual(result.returncode, 125)
            self.assertTrue(side_effect.exists())
            status = self.read_json(job_dir / "status.json")
            self.assertNotEqual(status.get("state"), "succeeded")

    def test_no_stderr_log_is_created_for_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            result = self.run_wrapper(job_dir, "bash", "-c", "printf err >&2; exit 4")

            self.assertEqual(result.returncode, 4)
            self.assertIn("err", (job_dir / "stdout.log").read_text(encoding="utf-8"))
            self.assertFalse((job_dir / "stderr.log").exists())


if __name__ == "__main__":
    unittest.main()
