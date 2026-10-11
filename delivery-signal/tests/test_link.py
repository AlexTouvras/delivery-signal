#!/usr/bin/env python3
"""Connect a tracker or import a CSV, then write the report. Tokens stay out of the project file."""

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import link  # noqa: E402
import pipeline  # noqa: E402


CSV = "key,summary,status,priority,assignee,due\nT-1,Rotate keys,To Do,Highest,,2026-10-01\n"


def _multipart(fields: list[tuple[str, str, bytes | None, str]]) -> tuple[bytes, str]:
    boundary = "deliverytestboundary"
    chunks: list[bytes] = []
    for name, filename, payload, text in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        if filename:
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            chunks.append(b"Content-Type: text/csv\r\n\r\n")
            chunks.append(payload or b"")
        else:
            chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            chunks.append(text.encode())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class LinkTests(unittest.TestCase):
    def test_three_steps_import_then_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "issues.csv"
            source.write_text(CSV, encoding="utf-8")
            self.assertEqual(
                link.main(["--home", str(home), "connect", "other", "--project", "WEB", "--name", "Board"]),
                0,
            )
            saved = json.loads((home / ".delivery-signal" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["tracker"], "other")
            self.assertNotIn("token", json.dumps(saved).lower())
            self.assertEqual(link.main(["--home", str(home), "import", str(source)]), 0)
            self.assertEqual(link.main(["--home", str(home), "report", "--as-of", "2026-10-10"]), 0)
            brief = json.loads((home / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["portfolio"], "Board")
            self.assertEqual(brief["top_risks"][0]["id"], "unassigned:T-1")

    def test_jira_pull_uses_the_connected_site_and_skips_the_token_file(self) -> None:
        calls = []

        def transport(method, url, headers, body):
            if method == "GET" and url.endswith("/rest/api/3/field"):
                return []
            calls.append((method, url, headers["Authorization"], json.loads(body.decode())))
            return {
                "issues": [
                    {
                        "key": "FOR-12",
                        "fields": {
                            "summary": "Carrier cutover",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Story"},
                            "priority": {"name": "Highest"},
                            "assignee": None,
                            "project": {"key": "FORGE", "name": "Forge"},
                            "created": "2026-08-01T10:00:00.000+0000",
                            "updated": "2026-08-02T10:00:00.000+0000",
                            "duedate": "2026-10-01",
                            "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Waiting on the carrier."}]}]},
                        },
                    }
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.dict(os.environ, {"JIRA_EMAIL": "ada@example.com", "JIRA_API_TOKEN": "secret-token"}):
                self.assertEqual(
                    link.main(
                        [
                            "--home",
                            str(home),
                            "connect",
                            "jira",
                            "--site",
                            "https://acme.atlassian.net",
                            "--project",
                            "FORGE",
                            "--name",
                            "Forge",
                        ]
                    ),
                    0,
                )
                project_text = (home / ".delivery-signal" / "project.json").read_text(encoding="utf-8")
                self.assertNotIn("secret-token", project_text)
                self.assertNotIn("ada@example.com", project_text)
                with mock.patch.object(link, "default_transport", transport):
                    self.assertEqual(link.main(["--home", str(home), "import"]), 0)
            self.assertEqual(calls[0][0], "POST")
            self.assertEqual(calls[0][1], "https://acme.atlassian.net/rest/api/3/search/jql")
            self.assertNotIn("secret-token", calls[0][1])
            self.assertEqual(link.main(["--home", str(home), "report", "--as-of", "2026-10-10"]), 0)
            brief = json.loads((home / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["source_profile"], "jira")
            self.assertEqual(brief["top_risks"][0]["id"], "unassigned:FOR-12")

    def test_ado_pull_reads_the_connected_project(self) -> None:
        bodies = []

        def transport(method, url, headers, body):
            if body:
                bodies.append(json.loads(body.decode()))
            if url.endswith("/wiql?api-version=7.1"):
                return {"workItems": [{"id": 20}]}
            return {
                "value": [
                    {
                        "id": 20,
                        "fields": {
                            "System.TeamProject": "Fabrikam",
                            "System.Title": "Cutover",
                            "System.State": "Active",
                            "System.WorkItemType": "User Story",
                            "Microsoft.VSTS.Common.Priority": 1,
                            "System.AssignedTo": {"displayName": ""},
                            "System.ChangedDate": "2026-08-01T00:00:00Z",
                            "Microsoft.VSTS.Scheduling.DueDate": "2026-10-01T00:00:00Z",
                        },
                    }
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.dict(os.environ, {"ADO_PAT": "ado-secret"}, clear=False):
                self.assertEqual(
                    link.main(
                        [
                            "--home",
                            str(home),
                            "connect",
                            "ado",
                            "--org",
                            "https://dev.azure.com/contoso",
                            "--project",
                            "Fabrikam",
                        ]
                    ),
                    0,
                )
                self.assertNotIn("ado-secret", (home / ".delivery-signal" / "project.json").read_text(encoding="utf-8"))
                with mock.patch.object(link, "default_transport", transport):
                    self.assertEqual(link.main(["--home", str(home), "import"]), 0)
            self.assertEqual(link.main(["--home", str(home), "report", "--as-of", "2026-10-10"]), 0)
            brief = json.loads((home / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["source_profile"], "azure_devops")
            self.assertEqual(brief["top_risks"][0]["id"], "unassigned:20")
            requested = bodies[-1]["fields"]
            self.assertIn("Microsoft.VSTS.Scheduling.StoryPoints", requested)
            self.assertIn("Microsoft.VSTS.Scheduling.Effort", requested)
            self.assertIn("Microsoft.VSTS.Scheduling.TargetDate", requested)

    def test_http_site_and_missing_token_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code = link.main(
                ["--home", str(home), "connect", "jira", "--site", "http://acme.atlassian.net", "--project", "FORGE"]
            )
            self.assertEqual(code, 1)
            with mock.patch.dict(os.environ, {"JIRA_EMAIL": "", "JIRA_API_TOKEN": ""}, clear=False):
                os.environ.pop("JIRA_EMAIL", None)
                os.environ.pop("JIRA_API_TOKEN", None)
                self.assertEqual(
                    link.main(
                        ["--home", str(home), "connect", "jira", "--site", "https://acme.atlassian.net", "--project", "FORGE"]
                    ),
                    0,
                )
                code = link.main(["--home", str(home), "import"])
            self.assertEqual(code, 1)

    def test_help_prints_the_three_steps(self) -> None:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = pipeline.main(["help"])
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("configuration page", text)
        self.assertIn("run connect jira", text)
        self.assertIn("run import issues.csv", text)
        self.assertIn("run report", text)

    def test_bare_run_opens_the_configuration_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(link, "open_config", return_value=0) as opened:
                code = pipeline.main([])
                self.assertEqual(link.main(["--home", tmp, "connect"]), 0)
            self.assertEqual(code, 0)
            self.assertEqual(opened.call_count, 2)

    def test_configuration_page_saves_the_project_and_builds_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            httpd = link.start_config(home)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    page = response.read().decode()
                self.assertIn("Configure the project", page)
                self.assertIn("this computer only", page.lower())
                self.assertIn('action="/configure"', page)
                body, content_type = _multipart(
                    [
                        ("tracker", "", None, "other"),
                        ("project", "", None, "WEB"),
                        ("name", "", None, "Board"),
                        ("token", "", None, "page-secret"),
                        ("email", "", None, "ada@example.com"),
                        ("as_of", "", None, "2026-10-10"),
                        ("export", "issues.csv", CSV.encode(), ""),
                    ]
                )
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/configure",
                    data=body,
                    headers={"Content-Type": content_type},
                )
                with urllib.request.urlopen(request) as response:
                    brief_html = response.read().decode()
                self.assertIn("Board", brief_html)
                self.assertIn("T-1", brief_html)
                saved = json.loads((home / ".delivery-signal" / "project.json").read_text(encoding="utf-8"))
                stored = json.dumps(saved)
                self.assertEqual(saved["tracker"], "other")
                self.assertEqual(saved["name"], "Board")
                self.assertNotIn("page-secret", stored)
                self.assertNotIn("ada@example.com", stored)
                self.assertNotIn("token", stored.lower())
                disk = (home / "out" / "brief.json").read_text(encoding="utf-8")
                self.assertNotIn("page-secret", disk)
                brief = json.loads(disk)
                self.assertEqual(brief["top_risks"][0]["id"], "unassigned:T-1")
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_configuration_page_reads_jira_without_storing_the_token(self) -> None:
        def transport(method, url, headers, body):
            self.assertNotIn("page-secret", url)
            return {
                "issues": [
                    {
                        "key": "FOR-12",
                        "fields": {
                            "summary": "Carrier cutover",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Story"},
                            "priority": {"name": "Highest"},
                            "assignee": None,
                            "project": {"key": "FORGE", "name": "Forge"},
                            "created": "2026-08-01",
                            "updated": "2026-08-02",
                            "duedate": "2026-10-01",
                        },
                    }
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            httpd = link.start_config(home)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                body, content_type = _multipart(
                    [
                        ("tracker", "", None, "jira"),
                        ("project", "", None, "FORGE"),
                        ("name", "", None, "Forge"),
                        ("site", "", None, "https://acme.atlassian.net"),
                        ("email", "", None, "ada@example.com"),
                        ("token", "", None, "page-secret"),
                        ("as_of", "", None, "2026-10-10"),
                    ]
                )
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/configure",
                    data=body,
                    headers={"Content-Type": content_type},
                )
                with mock.patch.object(link, "default_transport", transport):
                    with urllib.request.urlopen(request) as response:
                        brief_html = response.read().decode()
                self.assertIn("FOR-12", brief_html)
                stored = (home / ".delivery-signal" / "project.json").read_text(encoding="utf-8")
                self.assertNotIn("page-secret", stored)
                self.assertNotIn("ada@example.com", stored)
                self.assertIn("https://acme.atlassian.net", stored)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_pipeline_connect_writes_the_project_file_in_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.getcwd()
            os.chdir(tmp)
            try:
                code = pipeline.main(["connect", "other", "--project", "WEB", "--name", "Board"])
            finally:
                os.chdir(previous)
            self.assertEqual(code, 0)
            saved = json.loads((Path(tmp) / ".delivery-signal" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["name"], "Board")
            self.assertNotIn("token", json.dumps(saved).lower())

    def test_connected_jira_ignores_a_csv_and_reads_story_points(self) -> None:
        calls = []

        def transport(method, url, headers, body):
            if method == "GET" and url.endswith("/rest/api/3/field"):
                return [
                    {"id": "customfield_10016", "name": "Story point estimate"},
                    {"id": "customfield_10028", "name": "Story Points"},
                    {"id": "customfield_10015", "name": "Target end"},
                    {"id": "customfield_10014", "name": "Epic Link"},
                ]
            payload = json.loads(body.decode())
            calls.append(payload["fields"])
            return {
                "issues": [
                    {
                        "key": "FOR-12",
                        "fields": {
                            "summary": "Carrier cutover",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Story"},
                            "priority": {"name": "Highest"},
                            "assignee": None,
                            "project": {"key": "FORGE", "name": "Forge"},
                            "updated": "2026-08-02",
                            "duedate": None,
                            "parent": None,
                            "customfield_10028": 5,
                            "customfield_10015": "2026-10-01",
                            "customfield_10014": "FOR-1",
                        },
                    }
                ]
            }

        decoy = "key,summary,status,priority,assignee,due\nT-9,Ignore this row,To Do,Highest,,2026-10-01\n"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / "issues.csv"
            source.write_text(decoy, encoding="utf-8")
            with mock.patch.dict(os.environ, {"JIRA_EMAIL": "ada@example.com", "JIRA_API_TOKEN": "secret-token"}):
                self.assertEqual(
                    link.main(
                        [
                            "--home",
                            str(home),
                            "connect",
                            "jira",
                            "--site",
                            "https://acme.atlassian.net",
                            "--project",
                            "FORGE",
                        ]
                    ),
                    0,
                )
                stdout = io.StringIO()
                with mock.patch.object(link, "default_transport", transport), mock.patch("sys.stdout", stdout):
                    self.assertEqual(link.main(["--home", str(home), "import", str(source)]), 0)
            self.assertIn("read from the tracker", stdout.getvalue())
            self.assertIn("customfield_10028", calls[0])
            self.assertNotIn("customfield_10016", calls[0])
            self.assertIn("customfield_10015", calls[0])
            self.assertIn("customfield_10014", calls[0])
            table = (home / ".delivery-signal" / "work_items.csv").read_text(encoding="utf-8")
            self.assertIn("FOR-12", table)
            self.assertNotIn("T-9", table)
            self.assertIn("2026-10-01", table)
            self.assertIn("FOR-1", table)
            self.assertIn("5", table)
            self.assertEqual(link.main(["--home", str(home), "report", "--as-of", "2026-10-10"]), 0)
            brief = json.loads((home / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["top_risks"][0]["id"], "unassigned:FOR-12")

    def test_configuration_page_ignores_a_csv_when_jira_is_connected(self) -> None:
        def transport(method, url, headers, body):
            if method == "GET":
                return []
            return {
                "issues": [
                    {
                        "key": "FOR-12",
                        "fields": {
                            "summary": "Carrier cutover",
                            "status": {"name": "In Progress"},
                            "issuetype": {"name": "Story"},
                            "priority": {"name": "Highest"},
                            "assignee": None,
                            "project": {"key": "FORGE", "name": "Forge"},
                            "updated": "2026-08-02",
                            "duedate": "2026-10-01",
                        },
                    }
                ]
            }

        decoy = b"key,summary,status,priority,assignee,due\nT-9,Ignore this row,To Do,Highest,,2026-10-01\n"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            httpd = link.start_config(home)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                port = httpd.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                    page = response.read().decode()
                self.assertIn('id="csv-field"', page)
                self.assertIn('csv-field").hidden = tracker !== "other"', page)
                body, content_type = _multipart(
                    [
                        ("tracker", "", None, "jira"),
                        ("project", "", None, "FORGE"),
                        ("name", "", None, "Forge"),
                        ("site", "", None, "https://acme.atlassian.net"),
                        ("email", "", None, "ada@example.com"),
                        ("token", "", None, "page-secret"),
                        ("as_of", "", None, "2026-10-10"),
                        ("export", "issues.csv", decoy, ""),
                    ]
                )
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/configure",
                    data=body,
                    headers={"Content-Type": content_type},
                )
                with mock.patch.object(link, "default_transport", transport):
                    with urllib.request.urlopen(request) as response:
                        brief_html = response.read().decode()
                self.assertIn("FOR-12", brief_html)
                self.assertNotIn("T-9", brief_html)
                stored = (home / ".delivery-signal" / "project.json").read_text(encoding="utf-8")
                self.assertNotIn("page-secret", stored)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_plain_csv_command_still_writes_a_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            csv_path = home / "Northwind.csv"
            csv_path.write_text("key,summary,status\nT-1,Rotate keys,To Do\n", encoding="utf-8")
            code = pipeline.main([str(csv_path), "--as-of", "2026-10-10", "--out", str(home / "out")])
            self.assertEqual(code, 0)
            self.assertTrue((home / "out" / "brief.html").is_file())


if __name__ == "__main__":
    unittest.main()
