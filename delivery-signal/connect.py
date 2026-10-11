#!/usr/bin/env python3
"""Local page for trying a project export, plus the HTTP call a script can make.

The page listens on this computer only. The export is written to a temp folder,
the brief is built, and the folder is removed. Nothing is sent to a hosted service.
"""

from __future__ import annotations

import argparse
import html
import shutil
import sys
import tempfile
from datetime import date
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline

MAX_BYTES = 8 * 1024 * 1024
SAMPLE = Path(__file__).resolve().parent / "samples" / "northline"


def parse_form(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    if "multipart/form-data" not in content_type.lower():
        raise pipeline.InputError("Submit the form as multipart data.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    fields: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        raise pipeline.InputError("The form did not include any fields.")
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        fields[name] = (filename, payload if isinstance(payload, bytes) else b"")
    return fields


def _text(fields: dict[str, tuple[str, bytes]], name: str) -> str:
    found = fields.get(name)
    if not found:
        return ""
    return found[1].decode("utf-8-sig").strip()


def connect_page(port: int) -> str:
    curl = (
        f'curl -F name="Northwind" -F as_of="2026-10-10" -F export=@issues.csv \\\n'
        f"  http://127.0.0.1:{port}/run -o brief.html"
    )
    cli = 'python3 delivery-signal/pipeline.py issues.csv --name "Northwind" --as-of 2026-10-10 --previous issues-last-week.csv'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect a project</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; background: #f4f1ea; color: #1c1917; font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: 52rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    header {{ border-top: 6px solid #1c1917; padding-top: 0.8rem; }}
    .stamp {{ margin: 0; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 650; font-size: 0.72rem; color: #9a3412; }}
    h1 {{ font-size: 1.8rem; line-height: 1.1; margin: 0.35rem 0; }}
    h2 {{ font-size: 1.05rem; margin: 1.4rem 0 0.45rem; }}
    .meta {{ color: #57534e; }}
    form, .card {{ background: #fffdf8; border: 1px solid #e7e0d4; padding: 0.9rem 1rem; }}
    label {{ display: block; font-weight: 650; margin: 0.75rem 0 0.25rem; }}
    input, select {{ width: 100%; max-width: 28rem; font: inherit; padding: 0.4rem 0.5rem; box-sizing: border-box; }}
    button, .button {{ font: inherit; font-weight: 650; background: #1c1917; color: #fff; border: 0; padding: 0.5rem 0.8rem; }}
    a.button {{ display: inline-block; text-decoration: none; }}
    pre {{ background: #1c1917; color: #f4f1ea; padding: 0.8rem; overflow-x: auto; white-space: pre-wrap; font: 0.82rem/1.45 ui-monospace, monospace; }}
    .row {{ display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }}
    iframe {{ width: 100%; height: 80vh; border: 1px solid #e7e0d4; background: #fff; }}
    ul {{ padding-left: 1.1rem; }}
  </style>
</head>
<body>
  <main>
    <header>
      <p class="stamp">Local test · this computer only</p>
      <h1>Connect a project</h1>
      <p class="meta">Drop this week's export. The brief is built here and the file is deleted. Nothing is uploaded to a hosted service.</p>
      <p class="meta">On a phone, or on another computer, open <a href="https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html">the page from this repo</a> and choose the CSV there. That file stays in that browser.</p>
    </header>

    <h2>Try it before you export anything</h2>
    <p class="row"><a class="button" href="/sample" target="result">Open the Northline sample</a></p>

    <h2>Connect your export</h2>
    <form action="/run" method="post" enctype="multipart/form-data" target="result">
      <label for="name">Project or portfolio name</label>
      <input id="name" name="name" required placeholder="Northwind" autocomplete="organization">
      <label for="as_of">Report date</label>
      <input id="as_of" name="as_of" required type="date" value="{date.today().isoformat()}">
      <label for="profile">Export</label>
      <select id="profile" name="profile">
        <option value="">Detect from the header row</option>
        <option value="jira">Jira CSV</option>
        <option value="azure_devops">Azure DevOps CSV</option>
        <option value="canonical">Canonical columns</option>
      </select>
      <label for="export">This week's CSV</label>
      <input id="export" name="export" required type="file" accept=".csv,text/csv">
      <label for="previous">Last week's CSV, optional</label>
      <input id="previous" name="previous" type="file" accept=".csv,text/csv">
      <label for="columns">columns.json, optional</label>
      <input id="columns" name="columns" type="file" accept=".json,application/json">
      <p class="row"><button type="submit">Build the brief</button></p>
    </form>

    <h2>How to export</h2>
    <ul>
      <li>Jira: search work items, choose Export, then CSV (all fields). Keep Issue key, Summary, Status, Updated, and Due date.</li>
      <li>Azure DevOps: open the query, add columns, then Export to CSV. Keep ID, Title, Work Item Type, State, and Changed Date.</li>
      <li>A renamed header goes in columns.json. The example is <code>delivery-signal/templates/columns.example.json</code>.</li>
    </ul>

    <h2>Run it from a terminal or an IDE</h2>
    <p>In this repo, start the page:</p>
    <pre>python3 delivery-signal/connect.py</pre>
    <p>Or skip the page and write the brief next to the export:</p>
    <pre>{html.escape(cli)}</pre>
    <p>While this page is running, a script in another project can send a file to it:</p>
    <pre>{html.escape(curl)}</pre>

    <h2>Brief</h2>
    <iframe name="result" title="Delivery brief" src="/waiting"></iframe>
  </main>
</body>
</html>
"""


def waiting_page() -> str:
    return """<!DOCTYPE html>
<html lang="en"><body style="font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; color: #57534e; margin: 1rem;">
<p>The brief shows here.</p>
</body></html>
"""


def error_page(message: str, status: int) -> tuple[bytes, int]:
    body = f"""<!DOCTYPE html>
<html lang="en"><body style="font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 1rem;">
<p>{html.escape(message)}</p>
<p>Change the form and build again.</p>
</body></html>
"""
    return body.encode(), status


def run_brief(fields: dict[str, tuple[str, bytes]]) -> tuple[bytes, int]:
    name = _text(fields, "name")
    as_of = _text(fields, "as_of")
    profile = _text(fields, "profile") or None
    if profile is not None and profile not in pipeline.extract.PROFILES:
        return error_page("Choose a known export, or leave detection on.", 400)
    if not name or not as_of:
        return error_page("Add a project name and a report date.", 400)
    export = fields.get("export")
    if export is None or not export[1].strip():
        return error_page("Choose this week's CSV.", 400)
    staging = Path(tempfile.mkdtemp(prefix="delivery-connect-"))
    try:
        export_path = staging / "export.csv"
        export_path.write_bytes(export[1])
        previous_path = None
        previous = fields.get("previous")
        if previous and previous[1].strip():
            previous_path = staging / "previous.csv"
            previous_path.write_bytes(previous[1])
        columns_path = None
        columns = fields.get("columns")
        if columns and columns[1].strip():
            columns_path = staging / "columns.json"
            columns_path.write_bytes(columns[1])
        brief = pipeline.brief_from_csv(export_path, name, as_of, previous_path, profile, columns_path)
    except pipeline.InputError as exc:
        return error_page(str(exc), 400)
    except (OSError, UnicodeError, ValueError) as exc:
        return error_page(str(exc), 400)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not brief["quality"]["traceable"]:
        return error_page("The brief had a finding without evidence.", 400)
    return pipeline.render_html(brief).encode(), 200


def make_handler(port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", ""):
                self._send(200, connect_page(port).encode())
            elif path == "/waiting":
                self._send(200, waiting_page().encode())
            elif path == "/sample":
                try:
                    brief = pipeline.build(SAMPLE)
                    self._send(200, pipeline.render_html(brief).encode())
                except pipeline.InputError as exc:
                    body, status = error_page(str(exc), 400)
                    self._send(status, body)
            else:
                body, status = error_page("That page is not part of the test.", 404)
                self._send(status, body)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/run":
                body, status = error_page("Send the export to /run.", 404)
                self._send(status, body)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > MAX_BYTES:
                body, status = error_page("That export is larger than 8 MB.", 413)
                self._send(status, body)
                return
            raw = self.rfile.read(length)
            try:
                fields = parse_form(self.headers.get("Content-Type", ""), raw)
                body, status = run_brief(fields)
            except pipeline.InputError as exc:
                body, status = error_page(str(exc), 400)
            self._send(status, body)

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8766) -> None:
    if host not in ("127.0.0.1", "localhost"):
        print("Refusing to listen beyond this computer.", file=sys.stderr)
        raise SystemExit(1)
    httpd = ThreadingHTTPServer((host, port), make_handler(port))
    shown = "127.0.0.1" if host == "localhost" else host
    print(f"Connect a project at http://{shown}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open a local page to try a project export.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
