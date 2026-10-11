"""Connect a tracker, import its work items, and write the brief.

The token stays in the environment. The project file records the site and the
project key only.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from base64 import b64encode
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import extract
import pipeline


class LinkError(Exception):
    pass


TRACKERS = ("jira", "ado", "other")
MAX_ITEMS = 500
MAX_BYTES = 8 * 1024 * 1024
CONFIG_PORT = 8767
JIRA_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "project",
    "created",
    "updated",
    "duedate",
    "parent",
    "description",
]
ADO_FIELDS = [
    "System.TeamProject",
    "System.AreaPath",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "Microsoft.VSTS.Common.Priority",
    "System.AssignedTo",
    "System.CreatedDate",
    "System.ChangedDate",
    "Microsoft.VSTS.Scheduling.DueDate",
    "System.Parent",
    "System.Description",
    "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Scheduling.Effort",
    "Microsoft.VSTS.Scheduling.TargetDate",
]
JIRA_FIELD_NAMES = {
    "story points": "story_points",
    "story point estimate": "story_points",
    "target end": "target_end",
    "epic link": "epic_link",
}

USAGE = """1. Connect this folder to your terminal. The command is run.
   Command Prompt: run
   PowerShell: .\\run
   bash: ./run
   That opens the configuration page on this computer.
2. On that page, choose Jira, Azure DevOps, or another tracker, and the project.
   Jira and Azure DevOps are read from the project. A CSV is for another tracker.
   run connect
   run import
   run import issues.csv
   A script can send the same connection:
   run connect jira --site https://acme.atlassian.net --project FORGE
   run connect ado --org https://dev.azure.com/contoso --project Fabrikam
   run connect other --name "Board" --project WEB
3. Get the report. It opens on that page.
   run report
"""


def state_dir(home: Path) -> Path:
    return home / ".delivery-signal"


def project_path(home: Path) -> Path:
    return state_dir(home) / "project.json"


def load_project(home: Path) -> dict:
    path = project_path(home)
    if not path.is_file():
        raise LinkError("Connect a project or import a CSV first.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_project(home: Path, project: dict) -> None:
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    project_path(home).write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")


def _https(url: str, label: str) -> str:
    text = url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise LinkError(f"{label} must be an https URL without a password.")
    return text


def connect(home: Path, tracker: str, project_key: str, name: str, site: str, org: str) -> dict:
    if tracker not in TRACKERS:
        raise LinkError("Tracker must be jira, ado, or other.")
    key = project_key.strip()
    if not key:
        raise LinkError("A project key is required.")
    record = load_project(home) if project_path(home).is_file() else {}
    record.update(
        {
            "tracker": tracker,
            "project": key,
            "name": (name or record.get("name") or key).strip(),
        }
    )
    if tracker == "jira":
        if not site:
            raise LinkError("A Jira connection needs --site https://your-site.atlassian.net.")
        record["site"] = _https(site, "The Jira site")
        record.pop("org", None)
    elif tracker == "ado":
        if not org:
            raise LinkError("An Azure DevOps connection needs --org https://dev.azure.com/your-org.")
        record["org"] = _https(org, "The Azure DevOps organization")
        record.pop("site", None)
    else:
        record.pop("site", None)
        record.pop("org", None)
    save_project(home, record)
    return record


def _day(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _person(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("displayName") or value.get("uniqueName") or "").strip()
    return "" if value is None else str(value).strip()


def _issue_ref(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("key") or "").strip()
    if value is None:
        return ""
    return str(value).strip()


def _points(value: object) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _adf(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts = []
        if node.get("text"):
            parts.append(str(node["text"]))
        for child in node.get("content") or []:
            parts.append(_adf(child))
        return " ".join(part for part in parts if part).strip()
    if isinstance(node, list):
        return " ".join(_adf(item) for item in node).strip()
    return ""


def _write_rows(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def remember_import(home: Path, source: Path, previous: Path | None, name: str | None) -> dict:
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / "work_items.csv"
    prior = directory / "previous_work_items.csv"
    if previous is not None:
        shutil.copyfile(previous, prior)
    elif dest.is_file():
        shutil.copyfile(dest, prior)
    elif prior.is_file():
        prior.unlink()
    shutil.copyfile(source, dest)
    headers, _table = extract.read_table(dest)
    try:
        profile = extract.detect_profile(headers)
    except extract.InputError:
        profile = ""
    record = load_project(home) if project_path(home).is_file() else {
        "tracker": "other",
        "project": source.stem or "Portfolio",
        "name": source.stem or "Portfolio",
    }
    if name:
        record["name"] = name
    elif not record.get("name"):
        record["name"] = source.stem or "Portfolio"
    record["profile"] = profile
    record["imported"] = dest.name
    save_project(home, record)
    return record


def default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise LinkError(f"The tracker returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise LinkError(f"The tracker could not be reached: {exc.reason}.") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LinkError("The tracker returned a response that is not JSON.") from exc


def _basic(user: str, secret: str) -> str:
    return "Basic " + b64encode(f"{user}:{secret}".encode()).decode("ascii")


def _jira_field_ids(site: str, headers: dict[str, str], transport) -> dict[str, str]:
    try:
        catalog = transport("GET", site.rstrip("/") + "/rest/api/3/field", headers, None)
    except LinkError:
        return {}
    if not isinstance(catalog, list):
        return {}
    rank = {"story points": 0, "story point estimate": 1, "target end": 0, "epic link": 0}
    found: dict[str, str] = {}
    scores: dict[str, int] = {}
    for field in catalog:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip().casefold()
        role = JIRA_FIELD_NAMES.get(name)
        field_id = str(field.get("id") or "").strip()
        if not role or not field_id:
            continue
        score = rank[name]
        if role not in found or score < scores[role]:
            found[role] = field_id
            scores[role] = score
    return found


def fetch_jira(site: str, project_key: str, email: str, token: str, transport=None) -> list[list[str]]:
    transport = default_transport if transport is None else transport
    headers = {
        "Authorization": _basic(email, token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    extras = _jira_field_ids(site, headers, transport)
    requested = list(JIRA_FIELDS)
    for field_id in extras.values():
        if field_id not in requested:
            requested.append(field_id)
    story_id = extras.get("story_points", "")
    target_id = extras.get("target_end", "")
    epic_id = extras.get("epic_link", "")
    rows: list[list[str]] = []
    page = ""
    while len(rows) < MAX_ITEMS:
        safe_key = project_key.replace("\\", "\\\\").replace('"', '\\"')
        body = {
            "jql": f'project = "{safe_key}" ORDER BY key',
            "maxResults": 100,
            "fields": requested,
        }
        if page:
            body["nextPageToken"] = page
        payload = transport(
            "POST",
            site.rstrip("/") + "/rest/api/3/search/jql",
            headers,
            json.dumps(body).encode("utf-8"),
        )
        for issue in payload.get("issues") or []:
            fields = issue.get("fields") or {}
            project = fields.get("project") or {}
            status = fields.get("status") or {}
            priority = fields.get("priority") or {}
            issue_type = fields.get("issuetype") or {}
            parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
            due = _day(fields.get("duedate"))
            if not due and target_id:
                due = _day(fields.get(target_id))
            parent_key = str(parent.get("key") or "")
            if not parent_key and epic_id:
                parent_key = _issue_ref(fields.get(epic_id))
            rows.append(
                [
                    str(issue.get("key") or ""),
                    str(project.get("key") or project_key),
                    str(project.get("name") or project_key),
                    str(issue_type.get("name") or ""),
                    str(fields.get("summary") or ""),
                    str(status.get("name") or ""),
                    str(priority.get("name") or ""),
                    _person(fields.get("assignee")),
                    _day(fields.get("created")),
                    _day(fields.get("updated")),
                    due,
                    parent_key,
                    _adf(fields.get("description"))[:500],
                    _points(fields.get(story_id)) if story_id else "",
                ]
            )
            if len(rows) >= MAX_ITEMS:
                break
        page = str(payload.get("nextPageToken") or "")
        if not page:
            break
    return rows


def fetch_ado(org: str, project_key: str, token: str, transport=None) -> list[list[str]]:
    transport = default_transport if transport is None else transport
    headers = {
        "Authorization": _basic("", token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    safe_project = project_key.replace("'", "''")
    quoted = urllib.parse.quote(project_key)
    base = org.rstrip("/") + "/" + quoted
    wiql = transport(
        "POST",
        base + "/_apis/wit/wiql?api-version=7.1",
        headers,
        json.dumps({"query": f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{safe_project}'"}).encode(),
    )
    identifiers = [int(item["id"]) for item in (wiql.get("workItems") or [])][:MAX_ITEMS]
    rows: list[list[str]] = []
    for start in range(0, len(identifiers), 200):
        chunk = identifiers[start : start + 200]
        payload = transport(
            "POST",
            base + "/_apis/wit/workitemsbatch?api-version=7.1",
            headers,
            json.dumps({"ids": chunk, "fields": ADO_FIELDS}).encode("utf-8"),
        )
        for item in payload.get("value") or []:
            fields = item.get("fields") or {}
            rows.append(
                [
                    str(item.get("id") or ""),
                    str(fields.get("System.TeamProject") or project_key),
                    str(fields.get("System.AreaPath") or ""),
                    str(fields.get("System.WorkItemType") or ""),
                    str(fields.get("System.Title") or ""),
                    str(fields.get("System.State") or ""),
                    "" if fields.get("Microsoft.VSTS.Common.Priority") is None else str(fields.get("Microsoft.VSTS.Common.Priority")),
                    _person(fields.get("System.AssignedTo")),
                    _day(fields.get("System.CreatedDate")),
                    _day(fields.get("System.ChangedDate")),
                    _day(fields.get("Microsoft.VSTS.Scheduling.DueDate")),
                    "" if fields.get("System.Parent") is None else str(fields.get("System.Parent")),
                    _adf(fields.get("System.Description"))[:500],
                    _points(fields.get("Microsoft.VSTS.Scheduling.StoryPoints")),
                    _points(fields.get("Microsoft.VSTS.Scheduling.Effort")),
                    _day(fields.get("Microsoft.VSTS.Scheduling.TargetDate")),
                ]
            )
    return rows


JIRA_HEADERS = [
    "Issue key",
    "Project key",
    "Project name",
    "Issue Type",
    "Summary",
    "Status",
    "Priority",
    "Assignee",
    "Created",
    "Updated",
    "Due date",
    "Parent",
    "Comment",
    "Custom field (Story Points)",
]
ADO_HEADERS = [
    "ID",
    "Team Project",
    "Area Path",
    "Work Item Type",
    "Title",
    "State",
    "Priority",
    "Assigned To",
    "Created Date",
    "Changed Date",
    "Due Date",
    "Parent",
    "Description",
    "Story Points",
    "Effort",
    "Target Date",
]


def pull(home: Path, transport=None, email: str | None = None, token: str | None = None) -> Path:
    transport = default_transport if transport is None else transport
    record = load_project(home)
    tracker = record.get("tracker")
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / "incoming.csv"
    if tracker == "jira":
        if email is None:
            email = os.environ.get("JIRA_EMAIL", "")
        if token is None:
            token = os.environ.get("JIRA_API_TOKEN", "")
        email = email.strip()
        token = token.strip()
        if not email or not token:
            raise LinkError("Set JIRA_EMAIL and JIRA_API_TOKEN. The token is not stored.")
        rows = fetch_jira(record["site"], record["project"], email, token, transport)
        headers = JIRA_HEADERS
    elif tracker == "ado":
        if token is None:
            token = os.environ.get("ADO_PAT") or os.environ.get("AZURE_DEVOPS_EXT_PAT") or ""
        token = token.strip()
        if not token:
            raise LinkError("Set ADO_PAT. The token is not stored.")
        rows = fetch_ado(record["org"], record["project"], token, transport)
        headers = ADO_HEADERS
    else:
        raise LinkError("This tracker has no live read. Run import with a CSV.")
    if not rows:
        raise LinkError("The project query returned no work items.")
    _write_rows(dest, headers, rows)
    return dest


def report(home: Path, as_of: str | None, output: Path | None) -> dict:
    record = load_project(home)
    source = state_dir(home) / "work_items.csv"
    if not source.is_file():
        if record.get("tracker") in {"jira", "ado"}:
            raise LinkError("Import work items before the report. Run import")
        raise LinkError("Import work items before the report. Run import issues.csv")
    previous_path = state_dir(home) / "previous_work_items.csv"
    previous = previous_path if previous_path.is_file() else None
    profile = record.get("profile") or None
    if profile not in extract.PROFILES:
        profile = None
    columns = state_dir(home) / "columns.json"
    brief = pipeline.brief_from_csv(
        source,
        record.get("name") or record.get("project") or "Portfolio",
        as_of or pipeline.date.today().isoformat(),
        previous,
        profile,
        columns if columns.is_file() else None,
    )
    pipeline.write_brief(brief, output or (home / "out"))
    return brief


def _field_text(fields: dict[str, tuple[str, bytes]], name: str) -> str:
    found = fields.get(name)
    if not found:
        return ""
    return found[1].decode("utf-8-sig").strip()


def _parse_form(content_type: str, body: bytes) -> dict[str, tuple[str, bytes]]:
    if "multipart/form-data" not in content_type.lower():
        raise LinkError("Submit the form as multipart data.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    fields: dict[str, tuple[str, bytes]] = {}
    if not message.is_multipart():
        raise LinkError("The form did not include any fields.")
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


def _attr(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def config_page(home: Path) -> str:
    record: dict = {}
    if project_path(home).is_file():
        try:
            record = load_project(home)
        except (OSError, json.JSONDecodeError):
            record = {}
    tracker = record.get("tracker") if record.get("tracker") in TRACKERS else "jira"
    options = "\n".join(
        f'<option value="{value}"{" selected" if value == tracker else ""}>{label}</option>'
        for value, label in (("jira", "Jira"), ("ado", "Azure DevOps"), ("other", "Other tracker"))
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Configure the project</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; background: #f4f1ea; color: #1c1917; font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: 52rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    header {{ border-top: 6px solid #1c1917; padding-top: 0.8rem; }}
    .stamp {{ margin: 0; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 650; font-size: 0.72rem; color: #9a3412; }}
    h1 {{ font-size: 1.8rem; line-height: 1.1; margin: 0.35rem 0; }}
    .meta {{ color: #57534e; }}
    form {{ background: #fffdf8; border: 1px solid #e7e0d4; padding: 0.9rem 1rem; }}
    label {{ display: block; font-weight: 650; margin: 0.75rem 0 0.25rem; }}
    input, select {{ width: 100%; max-width: 28rem; font: inherit; padding: 0.4rem 0.5rem; box-sizing: border-box; }}
    button {{ font: inherit; font-weight: 650; background: #1c1917; color: #fff; border: 0; padding: 0.5rem 0.8rem; }}
    .hint {{ color: #57534e; margin: 0.25rem 0 0; }}
    [hidden] {{ display: none !important; }}
    iframe {{ width: 100%; height: 80vh; border: 1px solid #e7e0d4; background: #fff; margin-top: 1rem; }}
  </style>
</head>
<body>
  <main>
    <header>
      <p class="stamp">Configuration · this computer only</p>
      <h1>Configure the project</h1>
      <p class="meta">Jira or Azure DevOps reads the project. A CSV is only for another tracker. The token is used for this read and left out of the project file.</p>
    </header>
    <form action="/configure" method="post" enctype="multipart/form-data" target="result">
      <label for="tracker">Tracker</label>
      <select id="tracker" name="tracker">{options}</select>
      <label for="project">Project key</label>
      <input id="project" name="project" required value="{_attr(record.get("project"))}" placeholder="FORGE" autocomplete="off">
      <label for="name">Name on the report</label>
      <input id="name" name="name" value="{_attr(record.get("name"))}" placeholder="Forge" autocomplete="organization">
      <div id="site-field">
        <label for="site">Jira site</label>
        <input id="site" name="site" value="{_attr(record.get("site"))}" placeholder="https://acme.atlassian.net" inputmode="url" autocomplete="off">
      </div>
      <div id="org-field">
        <label for="org">Azure DevOps organization</label>
        <input id="org" name="org" value="{_attr(record.get("org"))}" placeholder="https://dev.azure.com/contoso" inputmode="url" autocomplete="off">
      </div>
      <div id="email-field">
        <label for="email">Jira email</label>
        <input id="email" name="email" type="email" autocomplete="off" placeholder="ada@example.com">
        <p class="hint">Used for this read. The project file keeps the site and the project key.</p>
      </div>
      <div id="token-field">
        <label for="token">API token</label>
        <input id="token" name="token" type="password" autocomplete="off">
        <p class="hint">Used for this read. It is left out of the project file.</p>
      </div>
      <label for="as_of">Report date</label>
      <input id="as_of" name="as_of" required type="date" value="{pipeline.date.today().isoformat()}">
      <div id="csv-field">
        <label for="export">This week's CSV</label>
        <input id="export" name="export" type="file" accept=".csv,text/csv">
        <label for="previous">Last week's CSV, optional</label>
        <input id="previous" name="previous" type="file" accept=".csv,text/csv">
      </div>
      <p><button type="submit">Save and build the report</button></p>
    </form>
    <iframe name="result" title="Delivery brief" src="/waiting"></iframe>
  </main>
  <script>
    function syncTracker() {{
      const tracker = document.getElementById("tracker").value;
      document.getElementById("site-field").hidden = tracker !== "jira";
      document.getElementById("org-field").hidden = tracker !== "ado";
      document.getElementById("email-field").hidden = tracker !== "jira";
      document.getElementById("token-field").hidden = tracker === "other";
      document.getElementById("csv-field").hidden = tracker !== "other";
    }}
    document.getElementById("tracker").addEventListener("change", syncTracker);
    syncTracker();
  </script>
</body>
</html>
"""


def _waiting_page() -> str:
    return """<!DOCTYPE html>
<html lang="en"><body style="font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; color: #57534e; margin: 1rem;">
<p>The report opens here after you save the project.</p>
</body></html>
"""


def _error_page(message: str) -> bytes:
    body = f"""<!DOCTYPE html>
<html lang="en"><body style="font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 1rem;">
<p>{html.escape(message)}</p>
<p>Change the form and save again.</p>
</body></html>
"""
    return body.encode()


def apply_config(home: Path, fields: dict[str, tuple[str, bytes]]) -> tuple[bytes, int]:
    tracker = _field_text(fields, "tracker") or "jira"
    project = _field_text(fields, "project")
    name = _field_text(fields, "name")
    email = _field_text(fields, "email")
    token = _field_text(fields, "token")
    as_of = _field_text(fields, "as_of") or None
    try:
        connect(
            home,
            tracker,
            project,
            name,
            _field_text(fields, "site") if tracker == "jira" else "",
            _field_text(fields, "org") if tracker == "ado" else "",
        )
        export = fields.get("export")
        previous = fields.get("previous")
        saved = state_dir(home) / "work_items.csv"
        if tracker in {"jira", "ado"}:
            incoming = pull(home, email=email or None, token=token or None)
            remember_import(home, incoming, None, name or None)
        elif export and export[1].strip():
            staging = Path(tempfile.mkdtemp(prefix="delivery-config-"))
            try:
                source = staging / "issues.csv"
                source.write_bytes(export[1])
                previous_path = None
                if previous and previous[1].strip():
                    previous_path = staging / "previous.csv"
                    previous_path.write_bytes(previous[1])
                remember_import(home, source, previous_path, name or None)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        if not saved.is_file():
            raise LinkError("Add this week's CSV.")
        brief = report(home, as_of, None)
    except (LinkError, pipeline.InputError, json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        return _error_page(str(exc)), 400
    if not brief["quality"]["traceable"]:
        return _error_page("The brief had a finding without evidence."), 400
    return pipeline.render_html(brief).encode(), 200


def make_config_handler(home: Path):
    folder = home

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", ""):
                self._send(200, config_page(folder).encode())
            elif path == "/waiting":
                self._send(200, _waiting_page().encode())
            else:
                self._send(404, _error_page("That page is not part of the configuration."))

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/configure":
                self._send(404, _error_page("Send the configuration to /configure."))
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > MAX_BYTES:
                self._send(413, _error_page("That export is larger than 8 MB."))
                return
            raw = self.rfile.read(length)
            try:
                fields = _parse_form(self.headers.get("Content-Type", ""), raw)
            except LinkError as exc:
                self._send(400, _error_page(str(exc)))
                return
            body, status = apply_config(folder, fields)
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


def start_config(home: Path, port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_config_handler(home))


def open_config(home: Path, port: int = CONFIG_PORT, opener=None) -> int:
    try:
        httpd = start_config(home, port)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Configuration page: {url}")
    print("It stays on this computer. Stop it with Ctrl+C.")
    if opener is None:
        opener = webbrowser.open

    def _open() -> None:
        try:
            opener(url)
        except Exception:
            return

    threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run", description="Connect a project and write the delivery brief.")
    parser.add_argument("--home", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command")

    connect_parser = sub.add_parser("connect")
    connect_parser.add_argument("tracker", nargs="?", choices=TRACKERS)
    connect_parser.add_argument("--site", default="")
    connect_parser.add_argument("--org", default="")
    connect_parser.add_argument("--project", default="")
    connect_parser.add_argument("--name", default="")

    import_parser = sub.add_parser("import")
    import_parser.add_argument("file", nargs="?", type=Path)
    import_parser.add_argument("--previous", type=Path)
    import_parser.add_argument("--name", default="")

    report_parser = sub.add_parser("report")
    report_parser.add_argument("--as-of", default="")

    sub.add_parser("help")
    args = parser.parse_args(argv)
    home = args.home
    try:
        if args.command == "help":
            print(USAGE, end="")
            return 0
        if args.command is None or (args.command == "connect" and not args.tracker):
            return open_config(home)
        if args.command == "connect":
            if not args.project.strip():
                raise LinkError("A project key is required.")
            record = connect(home, args.tracker, args.project, args.name, args.site, args.org)
            print(f"Connected {record['tracker']} project {record['project']}.")
            if record["tracker"] == "other":
                print("Next: run import issues.csv")
            else:
                print("Next: run import")
            return 0
        if args.command == "import":
            connected = ""
            if project_path(home).is_file():
                connected = str(load_project(home).get("tracker") or "")
            if connected in {"jira", "ado"}:
                if args.file is not None:
                    print("A connected project is read from the tracker.")
                source = pull(home)
            elif args.file is None:
                source = pull(home)
            else:
                if not args.file.is_file():
                    raise LinkError(f"CSV not found: {args.file}")
                source = args.file
            record = remember_import(home, source, args.previous, args.name or None)
            print(f"Imported work items for {record['name']}.")
            print("Next: run report")
            return 0
        brief = report(home, args.as_of or None, None)
    except (LinkError, pipeline.InputError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    if not brief["quality"]["traceable"]:
        print("Brief has a finding without evidence.", file=__import__("sys").stderr)
        return 1
    print(
        f"{brief['portfolio']}: {brief['source_profile']}, {len(brief['top_risks'])} top risks, "
        f"{len(brief['set_aside'])} set aside, result {brief['result_id']}, approval {brief['approval']}"
    )
    return 0
