#!/usr/bin/env python3
"""The local connect page accepts an export the way a browser form or curl would."""

import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import connect  # noqa: E402


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
    body = b"".join(chunks)
    return body, f"multipart/form-data; boundary={boundary}"


CSV = b"""key,project,project_name,type,summary,status,priority,assignee,story_points,percent_complete,created,updated,due,parent,blocked_by,comment
T-1,DEMO,Demo project,Task,Rotate keys,In Progress,Highest,,1,,2026-09-01,2026-10-09,2026-10-01,,,,
"""


class ConnectPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = connect.ThreadingHTTPServer(("127.0.0.1", 0), connect.make_handler(0))
        cls.port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass = connect.make_handler(cls.port)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_page_offers_the_sample_the_form_and_the_cli(self) -> None:
        with urllib.request.urlopen(self.url("/")) as response:
            page = response.read().decode()
        self.assertIn("Connect a project", page)
        self.assertIn('href="/sample"', page)
        self.assertIn('action="/run"', page)
        self.assertIn("python3 delivery-signal/pipeline.py", page)
        self.assertIn(f"http://127.0.0.1:{self.port}/run", page)
        self.assertIn("this computer only", page.lower())

    def test_sample_opens_the_known_brief(self) -> None:
        with urllib.request.urlopen(self.url("/sample")) as response:
            page = response.read().decode()
        self.assertIn("Northline Advisory", page)
        self.assertIn("FOR-12", page)
        self.assertIn("result ", page)

    def test_form_post_builds_a_brief_from_the_csv(self) -> None:
        body, content_type = _multipart(
            [
                ("name", "", None, "Demo project"),
                ("as_of", "", None, "2026-10-10"),
                ("profile", "", None, ""),
                ("export", "issues.csv", CSV, ""),
            ]
        )
        request = urllib.request.Request(
            self.url("/run"),
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            page = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("Demo project", page)
        self.assertIn("T-1", page)
        self.assertIn("no assignee", page.lower())

    def test_missing_export_is_rejected(self) -> None:
        body, content_type = _multipart(
            [
                ("name", "", None, "Demo project"),
                ("as_of", "", None, "2026-10-10"),
            ]
        )
        request = urllib.request.Request(
            self.url("/run"),
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("CSV", caught.exception.read().decode())


if __name__ == "__main__":
    unittest.main()
