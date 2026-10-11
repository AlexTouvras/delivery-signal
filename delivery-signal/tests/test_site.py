#!/usr/bin/env python3
"""The published page is the same checker, opened from GitHub on a phone or a computer."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline  # noqa: E402


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
PACKAGE = Path(__file__).resolve().parents[1]
SAMPLE_FILES = (
    "meta.json",
    "work_items.csv",
    "previous_work_items.csv",
    "notes.csv",
    "milestones.csv",
    "dependencies.csv",
)


class PublishedFilesTests(unittest.TestCase):
    def test_checker_and_sample_match_the_repo(self):
        self.assertEqual((DOCS / "engine" / "pipeline.py").read_bytes(), (PACKAGE / "pipeline.py").read_bytes())
        self.assertEqual((DOCS / "engine" / "extract.py").read_bytes(), (PACKAGE / "extract.py").read_bytes())
        sample = PACKAGE / "samples" / "northline"
        for name in SAMPLE_FILES:
            self.assertEqual((DOCS / "sample" / name).read_bytes(), (sample / name).read_bytes(), name)

    def test_page_can_be_installed_and_can_receive_a_csv(self):
        page = (DOCS / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="file"', page)
        self.assertIn("Add to Home Screen", page)
        self.assertIn("Open the Northline sample", page)
        for label in ("Apps", "One-liner", "npm", "Hackable"):
            self.assertIn(label, page)
        self.assertIn("install.sh | bash", page)
        self.assertIn("install.ps1 | iex", page)
        manifest = json.loads((DOCS / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["share_target"]["action"], "./share")
        self.assertEqual(manifest["share_target"]["params"]["files"][0]["name"], "export")
        self.assertIn(".csv", manifest["file_handlers"][0]["accept"]["text/csv"])
        worker = (DOCS / "sw.js").read_text(encoding="utf-8")
        self.assertIn("shared-export", worker)
        self.assertIn("/share", worker)


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="delivery-site-"))
        shutil.copytree(DOCS, cls.root, dirs_exist_ok=True)
        for name, source in (
            ("sample-jira", PACKAGE / "samples" / "northline-jira"),
            ("sample-ado", PACKAGE / "samples" / "northline-ado"),
        ):
            destination = cls.root / name
            destination.mkdir()
            for filename in SAMPLE_FILES:
                shutil.copyfile(source / filename, destination / filename)
        handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(  # noqa: E731
            *args, directory=str(cls.root), **kwargs
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_install_options_switch_by_platform_and_method(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.fail(f"playwright is required to verify the published page: {exc}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{self.port}/", wait_until="domcontentloaded")
            page.get_by_role("tab", name="One-liner").wait_for()
            self.assertIn("install.sh | bash", page.locator("body").inner_text())
            page.get_by_role("tab", name="Windows beta").click()
            self.assertIn("install.ps1 | iex", page.locator("body").inner_text())
            page.screenshot(path="/opt/cursor/artifacts/install_oneliner_phone.png", full_page=True)
            page.get_by_role("tab", name="npm").click()
            self.assertIn("npm install -g", page.locator("body").inner_text())
            page.screenshot(path="/opt/cursor/artifacts/install_npm_phone.png", full_page=False)
            page.get_by_role("tab", name="Hackable").click()
            self.assertIn("git clone", page.locator("body").inner_text())
            self.assertIn("delivery-signal", page.locator("body").inner_text())
            page.get_by_role("tab", name="macOS & Linux").click()
            self.assertIn("./run", page.locator("body").inner_text())
            page.get_by_role("tab", name="Apps").click()
            self.assertIn("Add to Home Screen", page.locator("body").inner_text())
            page.screenshot(path="/opt/cursor/artifacts/install_apps_phone.png", full_page=False)
            page.get_by_role("tab", name="One-liner").click()
            page.get_by_role("button", name="Copy").click()
            page.get_by_role("button", name="Copied").wait_for()
            copied = page.evaluate("() => navigator.clipboard.readText()")
            self.assertIn("install.sh | bash", copied)
            browser.close()

    def test_sample_and_upload_match_python(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.fail(f"playwright is required to verify the published page: {exc}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(f"http://127.0.0.1:{self.port}/", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#status').dataset.state === 'ready'",
                timeout=180000,
            )
            page.screenshot(path="/opt/cursor/artifacts/phone_connect.png", full_page=True)
            page.get_by_role("button", name="Open the Northline sample").click()
            page.wait_for_function(
                "() => document.querySelector('#status').dataset.state === 'built'",
                timeout=120000,
            )
            self.assertIn("e53f514aa6", page.locator("#status").inner_text())
            frame = page.frame_locator("#brief")
            frame.get_by_text("FOR-12", exact=False).first.wait_for()
            body = frame.locator("body").inner_text()
            self.assertIn("FOR-12", body)
            self.assertIn("e53f514aa6", body)
            self.assertNotIn("No previous export was attached.", body)
            page.screenshot(path="/opt/cursor/artifacts/phone_brief.png", full_page=False)

            for prefix in ("sample/", "sample-jira/", "sample-ado/"):
                result_id = page.evaluate(
                    """async (prefix) => {
                        const brief = await window.DeliverySignal.loadDirectory(prefix);
                        return brief.result_id;
                    }""",
                    prefix,
                )
                self.assertEqual(result_id, "e53f514aa6", prefix)

            export = self.root / "tiny.csv"
            export.write_text(
                "key,project,project_name,type,summary,status,priority,assignee,due,updated\n"
                "T-1,Demo,Demo project,Story,Rotate keys,To Do,Highest,,2026-10-01,2026-10-01\n",
                encoding="utf-8",
            )
            expected = pipeline.brief_from_csv(export, "Demo project", "2026-10-10")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.locator("#name").fill("Demo project")
            page.locator("#as_of").fill("2026-10-10")
            page.locator("#export").set_input_files(str(export))
            page.get_by_role("button", name="Build the brief").click()
            page.wait_for_function(
                "() => document.querySelector('#status').innerText.includes('Demo project')",
                timeout=60000,
            )
            self.assertIn(expected["result_id"], page.locator("#status").inner_text())
            built = page.frame_locator("#brief").locator("body").inner_text()
            self.assertIn("Rotate keys", built)
            self.assertIn("No previous export was attached.", built)
            page.screenshot(path="/opt/cursor/artifacts/desktop_brief.png", full_page=False)
            browser.close()


if __name__ == "__main__":
    unittest.main()
