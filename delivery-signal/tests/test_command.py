#!/usr/bin/env python3
"""One shell command runs the brief. It does not ask for a second install."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = "htmlpreview.github.io"


class CommandTests(unittest.TestCase):
    def test_shells_only_forward_to_the_checker(self) -> None:
        for name in ("run", "run.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("pipeline.py", text, name)
            self.assertIn(PAGE, text, name)
            lowered = text.lower()
            self.assertNotIn("openclaw", lowered, name)
            self.assertNotIn("pip install", lowered, name)
            self.assertNotIn("npm install", lowered, name)
            self.assertNotIn("venv", lowered, name)
        cmd = (ROOT / "run.cmd").read_text(encoding="utf-8").lower()
        self.assertIn("run.ps1", cmd)
        self.assertNotIn("openclaw", cmd)
        self.assertNotIn("pip install", cmd)
        skill = (ROOT.parents[0] / "skills" / "delivery-signal" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("openclaw", skill.lower())
        self.assertNotIn("/delivery-signal", skill)
        self.assertIn("run issues.csv", skill)
        self.assertIn("Do not install Python", skill)

    def test_bash_command_writes_the_brief(self) -> None:
        script = ROOT / "run"
        mode = script.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "run is executable")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            csv_path = directory / "Northwind.csv"
            csv_path.write_text("key,summary,status\nT-1,Rotate keys,To Do\n", encoding="utf-8")
            completed = subprocess.run(
                ["bash", str(script), str(csv_path), "--as-of", "2026-10-10", "--out", str(directory / "out")],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            brief = json.loads((directory / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["portfolio"], "Northwind")
            self.assertEqual(brief["as_of"], "2026-10-10")
            self.assertTrue(brief["result_id"])

    def test_install_scripts_do_not_fetch_a_runtime(self) -> None:
        for name in ("install.sh", "install.ps1", "bin.js"):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("pip install", text, name)
            self.assertNotIn("npm install", text, name)
            self.assertNotIn("openclaw", text, name)
        page = (ROOT.parents[0] / "docs" / "index.html").read_text(encoding="utf-8")
        for label in ("Apps", "One-liner", "npm", "Hackable", "macOS", "Windows"):
            self.assertIn(label, page)
        self.assertIn("install.sh | bash", page)
        self.assertIn("install.ps1 | iex", page)

    def test_install_sh_puts_the_command_on_the_machine(self) -> None:
        script = ROOT / "install.sh"
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["DELIVERY_SIGNAL_HOME"] = str(home / "app")
            completed = subprocess.run(
                ["bash", str(script)],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            launcher = home / ".local" / "bin" / "delivery-signal"
            self.assertTrue(launcher.is_file())
            csv_path = home / "Northwind.csv"
            csv_path.write_text("key,summary,status\nT-1,Rotate keys,To Do\n", encoding="utf-8")
            ran = subprocess.run(
                ["bash", str(launcher), str(csv_path), "--as-of", "2026-10-10", "--out", str(home / "out")],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(ran.returncode, 0, ran.stderr)
            brief = json.loads((home / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["portfolio"], "Northwind")


if __name__ == "__main__":
    unittest.main()
