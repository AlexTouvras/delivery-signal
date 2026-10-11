"""Map Jira, Azure DevOps, and the canonical CSV onto one work-item shape.

Jira Cloud does not promise a stable set of CSV headers: "current fields"
follows the columns on screen, and "all fields" can drop an empty custom
field. Azure DevOps exports the columns chosen on the query. The aliases
below are the headers those products actually use. A folder can override
any header in columns.json when a team renamed a field.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class InputError(Exception):
    pass


# First alias is the header written when we re-encode the sample.
FIELDS: dict[str, dict[str, list[str]]] = {
    "key": {
        "canonical": ["key"],
        "jira": ["Issue key"],
        "azure_devops": ["ID"],
    },
    "project": {
        "canonical": ["project"],
        "jira": ["Project key"],
        "azure_devops": ["Team Project"],
    },
    "project_name": {
        "canonical": ["project_name"],
        "jira": ["Project name"],
        "azure_devops": ["Area Path"],
    },
    "type": {
        "canonical": ["type"],
        "jira": ["Issue Type"],
        "azure_devops": ["Work Item Type"],
    },
    "summary": {
        "canonical": ["summary"],
        "jira": ["Summary"],
        "azure_devops": ["Title"],
    },
    "status": {
        "canonical": ["status"],
        "jira": ["Status"],
        "azure_devops": ["State"],
    },
    "priority": {
        "canonical": ["priority"],
        "jira": ["Priority"],
        "azure_devops": ["Priority"],
    },
    "assignee": {
        "canonical": ["assignee"],
        "jira": ["Assignee"],
        "azure_devops": ["Assigned To"],
    },
    "story_points": {
        "canonical": ["story_points"],
        "jira": ["Story Points", "Custom field (Story Points)", "Custom field (Story point estimate)"],
        "azure_devops": ["Story Points", "Effort"],
    },
    "percent_complete": {
        "canonical": ["percent_complete"],
        "jira": ["Custom field (Percent complete)", "Percent complete"],
        "azure_devops": ["Percent Complete"],
    },
    "created": {
        "canonical": ["created"],
        "jira": ["Created"],
        "azure_devops": ["Created Date"],
    },
    "updated": {
        "canonical": ["updated"],
        "jira": ["Updated"],
        "azure_devops": ["Changed Date"],
    },
    "due": {
        "canonical": ["due"],
        "jira": ["Due date", "Due", "Custom field (Target end)", "Target end"],
        "azure_devops": ["Due Date", "Target Date"],
    },
    "parent": {
        "canonical": ["parent"],
        "jira": ["Parent", "Custom field (Epic Link)", "Epic Link"],
        "azure_devops": ["Parent"],
    },
    "blocked_by": {
        "canonical": ["blocked_by"],
        "jira": ["Blocked By"],
        "azure_devops": ["Blocked By"],
    },
    "comment": {
        "canonical": ["comment"],
        "jira": ["Comment"],
        "azure_devops": ["Description"],
    },
}

REQUIRED = ("key", "summary", "status")
PROFILES = ("canonical", "jira", "azure_devops")

STATUS = {
    "done": "Done",
    "closed": "Done",
    "resolved": "Done",
    "completed": "Done",
    "complete": "Done",
    "removed": "Done",
    "in progress": "In Progress",
    "active": "In Progress",
    "doing": "In Progress",
    "committed": "In Progress",
    "to do": "To Do",
    "todo": "To Do",
    "new": "To Do",
    "open": "To Do",
    "approved": "To Do",
    "blocked": "Blocked",
    "impeded": "Blocked",
    "waiting": "Waiting",
    "pending": "Waiting",
    "on hold": "Waiting",
}

PRIORITY = {
    "highest": "Highest",
    "critical": "Highest",
    "blocker": "Highest",
    "1": "Highest",
    "high": "High",
    "major": "High",
    "2": "High",
    "medium": "Medium",
    "3": "Medium",
    "low": "Low",
    "lowest": "Low",
    "minor": "Low",
    "trivial": "Low",
    "4": "Low",
}

ADO_STATUS = {
    "In Progress": "Active",
    "To Do": "New",
    "Done": "Closed",
    "Blocked": "Blocked",
    "Waiting": "Pending",
}
ADO_PRIORITY = {"Highest": "1", "High": "2", "Medium": "3", "Low": "4"}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%b/%y %I:%M %p",
    "%d/%b/%Y %I:%M %p",
    "%d/%b/%y",
    "%d/%b/%Y",
    "%d.%m.%Y",
    "%d.%m.%Y %H:%M",
)
ADO_DATE_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
)

ASSIGNEE_EMAIL = re.compile(r"^(.*?)\s*<[^>]+>\s*$")


@dataclass
class MappedItem:
    key: str
    project: str
    project_name: str
    type: str
    summary: str
    status: str
    priority: str
    assignee: str
    story_points: float | None
    percent_complete: float | None
    created: date | None
    updated: date | None
    due: date | None
    parent: str
    blocked_by: str
    comment: str
    row: int


def parse_date(value: str, profile: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    formats = DATE_FORMATS + (ADO_DATE_FORMATS if profile == "azure_devops" else ())
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _norm_header(value: str) -> str:
    return value.strip().lstrip("\ufeff").casefold()


def detect_profile(headers: list[str]) -> str:
    names = {_norm_header(header) for header in headers if header.strip()}
    if "issue key" in names:
        return "jira"
    if "work item type" in names and "title" in names:
        return "azure_devops"
    if "key" in names and "summary" in names:
        return "canonical"
    raise InputError(
        "This CSV does not match a known export. "
        "Recognized shapes: canonical (key, summary), Jira (Issue key, Summary), "
        "Azure DevOps (ID, Title, Work Item Type). "
        "Add columns.json when the headers were renamed. Saw: " + ", ".join(headers[:12])
    )


def load_column_map(directory: Path) -> tuple[str | None, dict[str, str]]:
    path = directory / "columns.json"
    if not path.is_file():
        return None, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = payload.get("profile")
    if profile is not None and profile not in PROFILES:
        raise InputError(f"columns.json profile must be one of: {', '.join(PROFILES)}")
    columns = payload.get("columns") or {}
    unknown = set(columns) - set(FIELDS)
    if unknown:
        raise InputError("columns.json has unknown fields: " + ", ".join(sorted(unknown)))
    return profile, {key: str(value) for key, value in columns.items()}


def _aliases(field_name: str, profile: str, overrides: dict[str, str]) -> list[str]:
    if field_name in overrides:
        return [overrides[field_name]]
    return FIELDS[field_name][profile]


def _header_index(headers: list[str], aliases: list[str]) -> list[int]:
    wanted = {_norm_header(alias) for alias in aliases}
    found = []
    for index, header in enumerate(headers):
        name = _norm_header(header)
        if name in wanted or any(name.startswith(alias + ".") or name.startswith(alias + " ") for alias in wanted):
            found.append(index)
    return found


def _cell(row: list[str], indexes: list[int], join: bool = False) -> str:
    parts = []
    for index in indexes:
        if index < len(row):
            text = row[index].strip()
            if text:
                parts.append(text)
    if join:
        return " ".join(parts)
    return parts[0] if parts else ""


def _number(value: str) -> float | None:
    text = value.strip().rstrip("%").strip()
    if not text:
        return None
    return float(text)


def _status(value: str) -> str:
    text = value.strip()
    return STATUS.get(text.casefold(), text)


def _priority(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    token = text.casefold()
    if token[:1].isdigit():
        token = token[:1]
    return PRIORITY.get(token, text)


def _assignee(value: str) -> str:
    text = value.strip()
    match = ASSIGNEE_EMAIL.match(text)
    return (match.group(1) if match else text).strip()


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            headers = next(reader)
        except StopIteration as exc:
            raise InputError(f"{path.name} is empty") from exc
        return headers, [row for row in reader if any(cell.strip() for cell in row)]


def map_work_items(
    path: Path,
    profile: str,
    overrides: dict[str, str],
    issues: list[str],
    default_project: str = "",
    default_project_name: str = "",
) -> list[MappedItem]:
    headers, table = read_table(path)
    if not table:
        raise InputError(f"{path.name} has no rows")
    indexes = {field: _header_index(headers, _aliases(field, profile, overrides)) for field in FIELDS}
    missing = [field for field in REQUIRED if not indexes[field]]
    if missing:
        raise InputError(
            f"{path.name} is missing {', '.join(missing)} for the {profile} export. "
            f"Headers: {', '.join(headers[:12])}"
        )
    items: list[MappedItem] = []
    seen: dict[str, int] = {}
    for offset, row in enumerate(table, start=2):
        key = _cell(row, indexes["key"])
        if not key:
            issues.append(f"{path.name} row {offset} has no key")
            continue
        if key in seen:
            issues.append(f"duplicate work item {key} on rows {seen[key]} and {offset}")
            continue
        seen[key] = offset
        project = _cell(row, indexes["project"]) or default_project or "Portfolio"
        if not _cell(row, indexes["project"]) and not default_project:
            issues.append(f"{key} has no project column; grouped under Portfolio")
        points = _optional_number(row, indexes, "story_points", key, issues)
        percent = _optional_number(row, indexes, "percent_complete", key, issues)
        created = _dated(row, indexes, "created", profile, key, issues)
        updated = _dated(row, indexes, "updated", profile, key, issues)
        due = _dated(row, indexes, "due", profile, key, issues)
        raw_status = _cell(row, indexes["status"])
        status = _status(raw_status)
        if raw_status and status == raw_status and raw_status.casefold() not in STATUS:
            issues.append(f"{key} status {raw_status!r} is not a recognized workflow state")
        raw_priority = _cell(row, indexes["priority"])
        priority = _priority(raw_priority)
        if raw_priority and priority == raw_priority and raw_priority.casefold() not in PRIORITY:
            issues.append(f"{key} priority {raw_priority!r} is not a recognized priority")
        items.append(
            MappedItem(
                key=key,
                project=project,
                project_name=_cell(row, indexes["project_name"]) or default_project_name or project,
                type=_cell(row, indexes["type"]),
                summary=_cell(row, indexes["summary"]),
                status=status,
                priority=priority,
                assignee=_assignee(_cell(row, indexes["assignee"])),
                story_points=points,
                percent_complete=percent,
                created=created,
                updated=updated,
                due=due,
                parent=_cell(row, indexes["parent"]),
                blocked_by=_cell(row, indexes["blocked_by"]),
                comment=_cell(row, indexes["comment"], join=True),
                row=offset,
            )
        )
    return items


def _optional_number(row: list[str], indexes: dict[str, list[int]], field_name: str, key: str, issues: list[str]) -> float | None:
    raw = _cell(row, indexes[field_name])
    if not raw:
        return None
    try:
        return _number(raw)
    except ValueError:
        issues.append(f"{key} {field_name} is not a number: {raw}")
        return None


def _dated(row: list[str], indexes: dict[str, list[int]], field_name: str, profile: str, key: str, issues: list[str]) -> date | None:
    raw = _cell(row, indexes[field_name])
    if not raw:
        return None
    parsed = parse_date(raw, profile)
    if parsed is None:
        issues.append(f"{key} {field_name} is not a recognized date: {raw}")
    return parsed


def _format_date(value: str, profile: str, with_time: bool) -> str:
    if not value:
        return ""
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if profile == "jira":
        stamp = parsed.strftime("%d/%b/%y")
        return f"{stamp} 4:12 PM" if with_time else stamp
    if profile == "azure_devops":
        stamp = parsed.strftime("%m/%d/%Y")
        return f"{stamp} 4:12:00 PM" if with_time else stamp
    return value


def write_profile_copy(source: Path, dest: Path, profile: str) -> None:
    """Re-encode the canonical sample in a product's export headers."""
    if profile not in ("jira", "azure_devops"):
        raise InputError("write_profile_copy supports jira and azure_devops")
    dest.mkdir(parents=True, exist_ok=True)
    meta = json.loads((source / "meta.json").read_text(encoding="utf-8"))
    meta["description"] = (
        f"Same synthetic portfolio as northline, saved in the {profile} column layout. "
        "Not a customer delivery."
    )
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    for name in ("milestones.csv", "dependencies.csv", "notes.csv"):
        shutil.copy(source / name, dest / name)
    for source_name, dest_name in (
        ("work_items.csv", "work_items.csv"),
        ("previous_work_items.csv", "previous_work_items.csv"),
    ):
        _rewrite_items(source / source_name, dest / dest_name, profile)


def _rewrite_items(source: Path, dest: Path, profile: str) -> None:
    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    headers = [FIELDS[field][profile][0] for field in FIELDS]
    with dest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_export_cell(field, row, profile) for field in FIELDS])


def _export_cell(field_name: str, row: dict[str, str], profile: str) -> str:
    value = (row.get(field_name) or "").strip()
    if profile == "azure_devops" and field_name == "status":
        return ADO_STATUS.get(value, value)
    if profile == "azure_devops" and field_name == "priority":
        return ADO_PRIORITY.get(value, value)
    if profile == "azure_devops" and field_name == "assignee" and value:
        local = re.sub(r"[^a-z0-9]+", ".", value.casefold()).strip(".")
        return f"{value} <{local}@northline.example>"
    if field_name in {"created", "updated", "due"}:
        return _format_date(value, profile, with_time=field_name == "updated")
    return value
