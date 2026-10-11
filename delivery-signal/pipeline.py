#!/usr/bin/env python3
"""Build an evidence-linked weekly delivery brief from a project export.

The rules are deterministic on purpose. A finding is kept only when every
material claim cites a row in the export. Narrative interpretation is marked
as an inference. The brief does not predict that a future date will be missed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import extract


RULES_VERSION = "2026-10-11"
DONE = {"done", "closed", "resolved"}
ACTIVE = {"in progress", "blocked", "waiting"}
KNOWN_STATUS = {"done", "in progress", "to do", "blocked", "waiting"}
KNOWN_PRIORITY = {"highest", "high", "medium", "low"}
HIGH = {"highest", "high"}
PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "low": 3}
GROUP_AT = 3
OPTIMISTIC = re.compile(r"\b(green|on track|no risks?|no blockers?)\b", re.I)
ONLY_OPEN = re.compile(r"\bonly open\b", re.I)


InputError = extract.InputError


@dataclass
class Evidence:
    source: str
    id: str
    field: str
    value: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Finding:
    id: str
    kind: str
    severity: int
    confidence: str
    project: str
    title: str
    summary: str
    blocks_delivery: bool
    suggested_question: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["evidence"] = [e.to_dict() for e in self.evidence]
        return data


@dataclass
class WorkItem:
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


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(value: str, label: str, issues: list[str]) -> date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        issues.append(f"{label} is not YYYY-MM-DD: {text}")
        return None


def _parse_number(value: str, label: str, issues: list[str]) -> float | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        issues.append(f"{label} is not a number: {text}")
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _fmt_date(value: date | None) -> str:
    if value is None:
        return "no date"
    return f"{value.day} {value.strftime('%b %Y')}"


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "blank"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _is_done(status: str) -> bool:
    return status.strip().lower() in DONE


def _priority(item: WorkItem) -> str:
    return item.priority.strip().lower()


def _status(item: WorkItem) -> str:
    return item.status.strip().lower()


def _overdue(item: WorkItem, as_of: date) -> bool:
    return item.due is not None and item.due < as_of and not _is_done(item.status)


def _quote(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return f'"{compact}"'


def load_portfolio(directory: Path) -> dict:
    meta_path = directory / "meta.json"
    items_path = directory / "work_items.csv"
    if not meta_path.is_file() or not items_path.is_file():
        raise InputError(f"{directory} needs meta.json and work_items.csv")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    as_of = _parse_date(str(meta.get("as_of", "")), "meta.as_of", issues)
    if as_of is None:
        raise InputError("meta.json as_of must be YYYY-MM-DD")
    if not _clean(str(meta.get("portfolio", ""))):
        raise InputError("meta.json portfolio name is required")

    forced, overrides = extract.load_column_map(directory)
    profile = forced or str(meta.get("profile") or "") or None
    if profile is not None and profile not in extract.PROFILES:
        raise InputError(f"meta.json profile must be one of: {', '.join(extract.PROFILES)}")
    headers, _table = extract.read_table(items_path)
    profile = profile or extract.detect_profile(headers)
    mapped = extract.map_work_items(
        items_path,
        profile,
        overrides,
        issues,
        default_project=_clean(str(meta.get("default_project", ""))),
        default_project_name=_clean(str(meta.get("default_project_name", ""))),
    )
    items = [WorkItem(**asdict(item)) for item in mapped]

    def optional(name: str) -> list[dict[str, str]]:
        path = directory / name
        if not path.is_file():
            return []
        return _read_csv(path)

    milestones = []
    for offset, row in enumerate(optional("milestones.csv"), start=2):
        ident = _clean(row.get("id"))
        if not ident:
            issues.append(f"milestones.csv row {offset} has no id")
            continue
        milestones.append(
            {
                "id": ident,
                "project": _clean(row.get("project")),
                "name": _clean(row.get("name")),
                "due": _parse_date(row.get("due", ""), f"{ident} due", issues),
                "status": _clean(row.get("status")),
                "row": offset,
            }
        )

    dependencies = []
    for offset, row in enumerate(optional("dependencies.csv"), start=2):
        ident = _clean(row.get("id"))
        if not ident:
            issues.append(f"dependencies.csv row {offset} has no id")
            continue
        dependencies.append(
            {
                "id": ident,
                "predecessor": _clean(row.get("predecessor")),
                "successor": _clean(row.get("successor")),
                "note": _clean(row.get("note")),
                "row": offset,
            }
        )

    notes = []
    for offset, row in enumerate(optional("notes.csv"), start=2):
        ident = _clean(row.get("id"))
        if not ident:
            issues.append(f"notes.csv row {offset} has no id")
            continue
        notes.append(
            {
                "id": ident,
                "date": _parse_date(row.get("date", ""), f"{ident} date", issues),
                "project": _clean(row.get("project")),
                "author": _clean(row.get("author")),
                "text": _clean(row.get("text")),
                "row": offset,
            }
        )

    previous = []
    previous_path = directory / "previous_work_items.csv"
    if previous_path.is_file():
        for item in extract.map_work_items(previous_path, profile, overrides, issues):
            previous.append(
                {
                    "key": item.key,
                    "project": item.project,
                    "summary": item.summary,
                    "status": item.status,
                    "assignee": item.assignee,
                    "priority": item.priority,
                    "due": item.due.isoformat() if item.due else "",
                    "blocked_by": item.blocked_by,
                    "percent_complete": "" if item.percent_complete is None else _fmt_num(item.percent_complete),
                }
            )

    by_key = {item.key: item for item in items}
    for item in items:
        if item.parent and item.parent not in by_key:
            issues.append(f"{item.key} parent {item.parent} is not in the export")
        if item.blocked_by and item.blocked_by not in by_key:
            issues.append(f"{item.key} blocked_by {item.blocked_by} is not in the export")
    known = set(by_key) | {row["id"] for row in milestones}
    for dep in dependencies:
        if dep["predecessor"] not in known:
            issues.append(f"{dep['id']} predecessor {dep['predecessor']} is not in the export")
        if dep["successor"] not in known:
            issues.append(f"{dep['id']} successor {dep['successor']} is not in the export")

    return {
        "meta": meta,
        "as_of": as_of,
        "items": items,
        "by_key": by_key,
        "milestones": milestones,
        "milestone_by_id": {row["id"]: row for row in milestones},
        "dependencies": dependencies,
        "notes": notes,
        "previous": previous,
        "issues": issues,
        "stale_days": int(meta.get("stale_days", 14)),
        "top_n": int(meta.get("top_n", 5)),
        "min_severity": int(meta.get("min_severity", 40)),
        "source_profile": profile,
    }


def _ev(source: str, ident: str, field_name: str, value: object) -> Evidence:
    return Evidence(source, ident, field_name, "" if value is None else str(value))


def _item_evidence(item: WorkItem, field_name: str) -> Evidence:
    value = getattr(item, field_name)
    if isinstance(value, float):
        value = _fmt_num(value)
    return _ev("work_items", item.key, field_name, value)


def _and(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _quoted_comment(text: str) -> str:
    quoted = _quote(text)
    if text.rstrip().endswith((".", "!", "?", "…")):
        return f". The item comment says {quoted}"
    return f". The item comment says {quoted}."


def _project_names(data: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in data["items"]:
        names.setdefault(item.project, item.project_name)
    return names


def _children(data: dict, epic_key: str) -> list[WorkItem]:
    return [item for item in data["items"] if item.parent == epic_key]


def _milestone_done(row: dict) -> bool:
    return row["status"].strip().lower() in {"done", "met", "complete", "closed"}


def _record_done(data: dict, ident: str) -> bool:
    item = data["by_key"].get(ident)
    if item is not None:
        return _is_done(item.status)
    milestone = data["milestone_by_id"].get(ident)
    if milestone is not None:
        return _milestone_done(milestone)
    return False


def _record_overdue(data: dict, ident: str, as_of: date) -> bool:
    item = data["by_key"].get(ident)
    if item is not None:
        return _overdue(item, as_of)
    milestone = data["milestone_by_id"].get(ident)
    if milestone is None or _milestone_done(milestone):
        return False
    if milestone["status"].strip().lower() == "overdue":
        return True
    return milestone["due"] is not None and milestone["due"] < as_of


def _stale(item: WorkItem, as_of: date, stale_days: int) -> bool:
    if _is_done(item.status) or _status(item) not in ACTIVE:
        return False
    if item.updated is None:
        return True
    return (as_of - item.updated).days >= stale_days


def _related_notes(data: dict, project: str, tokens: list[str]) -> list[dict]:
    found = []
    for note in data["notes"]:
        if note["project"] != project:
            continue
        text = note["text"]
        if any(token and token in text for token in tokens):
            found.append(note)
    return found


def _priority_label(finding: Finding) -> str:
    match = re.search(r"\b([A-Za-z]+) priority\b", finding.title)
    return match.group(1) if match else ""


def _collapse_unassigned(findings: list[Finding]) -> list[Finding]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    rest: list[Finding] = []
    for finding in findings:
        if finding.kind != "unassigned":
            rest.append(finding)
            continue
        grouped.setdefault((finding.project, _priority_label(finding)), []).append(finding)
    collapsed: list[Finding] = []
    for (project, label), items in grouped.items():
        if len(items) < GROUP_AT or not label:
            collapsed.extend(items)
            continue
        ordered = sorted(items, key=lambda finding: (-finding.severity, finding.id))
        keys = [finding.id.split(":", 1)[1] for finding in ordered]
        shown = ", ".join(keys[:8])
        extra = f" and {len(keys) - 8} more" if len(keys) > 8 else ""
        evidence: list[Evidence] = []
        for finding in ordered:
            evidence.extend(finding.evidence)
        collapsed.append(
            Finding(
                id=f"unassigned-count:{project}:{label}",
                kind="unassigned_count",
                severity=ordered[0].severity,
                confidence="fact",
                project=project,
                title=f"{len(items)} {label} priority items have no assignee.",
                summary=(
                    f"{len(items)} open items are {label} priority and the assignee cell is empty: "
                    f"{shown}{extra}."
                ),
                blocks_delivery=False,
                suggested_question=f"Who owns the {len(items)} unassigned {label} priority items?",
                evidence=evidence,
            )
        )
    return rest + collapsed


def _file_limits(items: list[WorkItem]) -> list[str]:
    open_items = [item for item in items if not _is_done(item.status)]
    if not open_items:
        return []
    limits = []
    if not any(item.due for item in open_items):
        limits.append("no due dates, so lateness is left out")
    unknown_priority = []
    seen: set[str] = set()
    for item in open_items:
        label = item.priority.strip()
        token = label.casefold()
        if label and token not in KNOWN_PRIORITY and token not in seen:
            seen.add(token)
            unknown_priority.append(label)
    if unknown_priority:
        limits.append("unrecognized priority: " + ", ".join(unknown_priority[:6]))
    elif not any(item.priority.strip() for item in open_items):
        limits.append("no priority")
    counts: dict[str, int] = {}
    for item in open_items:
        if item.status.strip().lower() not in KNOWN_STATUS:
            counts[item.status] = counts.get(item.status, 0) + 1
    if counts:
        parts = [
            f"{count} {name}"
            for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        ]
        limits.append("unrecognized status: " + ", ".join(parts))
    if not any(item.story_points is not None for item in items):
        limits.append("no story points")
    return limits


def _rules_could_not_run(items: list[WorkItem], project: str) -> bool:
    open_items = [item for item in items if item.project == project and not _is_done(item.status)]
    if not open_items:
        return False
    ranked = any(item.priority.strip().casefold() in KNOWN_PRIORITY for item in open_items)
    dated = any(item.due for item in open_items)
    return not ranked and not dated


def _optimistic_notes(data: dict, project: str) -> list[dict]:
    return [
        note
        for note in data["notes"]
        if note["project"] == project and OPTIMISTIC.search(note["text"])
    ]


def analyse(data: dict) -> dict:
    as_of: date = data["as_of"]
    names = _project_names(data)
    findings: list[Finding] = []
    set_aside: list[Finding] = []
    covered: set[str] = set()

    for project in names:
        notes = _optimistic_notes(data, project)
        if not notes:
            continue
        stale_items = [item for item in data["items"] if item.project == project and _stale(item, as_of, data["stale_days"])]
        overdue_items = [item for item in data["items"] if item.project == project and _overdue(item, as_of)]
        if not stale_items and not overdue_items:
            continue
        note = notes[0]
        focus = []
        seen: set[str] = set()
        for item in overdue_items + stale_items:
            if item.key in seen or item.type.strip().lower() == "epic":
                continue
            seen.add(item.key)
            focus.append(item)
        if not focus:
            focus = overdue_items or stale_items
        covered.update(item.key for item in focus)
        newest = max((item.updated for item in focus if item.updated), default=None)
        overdue_keys = [item.key for item in focus if _overdue(item, as_of)]
        detail = ", ".join(
            f"{item.key} due {_fmt_date(item.due)}, updated {_fmt_date(item.updated)}"
            for item in focus
        )
        overdue_clause = (
            f"{_and(overdue_keys)} {'is' if len(overdue_keys) == 1 else 'are'} already overdue. "
            if overdue_keys
            else ""
        )
        findings.append(
            Finding(
                id=f"note_conflict:{note['id']}",
                kind="note_conflict",
                severity=86 if overdue_keys else 76,
                confidence="inference",
                project=project,
                title=f"{names[project]} is described as on track, but the export disagrees.",
                summary=(
                    f"{note['id']} on {_fmt_date(note['date'])} says the work is on track. "
                    f"{overdue_clause}"
                    f"The newest update among these open items is {_fmt_date(newest)}. {detail}."
                ),
                blocks_delivery=False,
                suggested_question=(
                    f"What changed on {names[project]} after {_fmt_date(newest)}? "
                    f"{note['id']} and the work-item export disagree."
                ),
                evidence=[
                    _ev("notes", note["id"], "text", note["text"]),
                    _ev("notes", note["id"], "date", note["date"]),
                    *[
                        piece
                        for item in focus
                        for piece in (
                            _item_evidence(item, "status"),
                            _item_evidence(item, "updated"),
                            _item_evidence(item, "due"),
                        )
                    ],
                ],
            )
        )

    for item in data["items"]:
        if item.key in covered or _is_done(item.status):
            continue
        if item.type.strip().lower() == "decision" or _status(item) == "waiting":
            continue
        if _priority(item) not in HIGH or item.assignee:
            continue
        overdue = _overdue(item, as_of)
        imminent = item.due is not None and 0 <= (item.due - as_of).days <= 7
        if overdue:
            severity = 90
            when = f"due {_fmt_date(item.due)} (before this report)"
        elif imminent:
            severity = 82
            when = f"due {_fmt_date(item.due)}"
        else:
            severity = 74
            when = f"due {_fmt_date(item.due)}" if item.due else "with no due date"
        comment = _quoted_comment(item.comment) if item.comment else ""
        evidence = [
            _item_evidence(item, "assignee"),
            _item_evidence(item, "priority"),
            _item_evidence(item, "status"),
            _item_evidence(item, "due"),
        ]
        if item.comment:
            evidence.append(_item_evidence(item, "comment"))
        findings.append(
            Finding(
                id=f"unassigned:{item.key}",
                kind="unassigned",
                severity=severity,
                confidence="fact",
                project=item.project,
                title=f"{item.key} is {item.priority} priority, open, and has no assignee.",
                summary=(
                    f"{item.key} ({item.summary}) is {item.status}, {when}, "
                    f"and the assignee cell is empty{comment or '.'}"
                ),
                blocks_delivery=False,
                suggested_question=f"Who owns {item.key} ({item.summary})?",
                evidence=evidence,
            )
        )
        covered.add(item.key)

    for project in names:
        if _optimistic_notes(data, project):
            continue
        for item in data["items"]:
            if item.project != project or item.key in covered or _is_done(item.status):
                continue
            if item.type.strip().lower() in {"decision", "epic"} or _status(item) == "waiting":
                continue
            overdue = _overdue(item, as_of)
            stale = item.updated is not None and _stale(item, as_of, data["stale_days"])
            if not overdue and not stale:
                continue
            if overdue:
                findings.append(
                    Finding(
                        id=f"overdue:{item.key}",
                        kind="overdue",
                        severity=64,
                        confidence="fact",
                        project=item.project,
                        title=f"{item.key} is overdue and still {item.status}.",
                        summary=(
                            f"{item.key} ({item.summary}) is {item.status}, due {_fmt_date(item.due)}, "
                            f"and the assignee is {item.assignee or 'blank'}."
                        ),
                        blocks_delivery=False,
                        suggested_question=f"What is keeping {item.key} ({item.summary}) open past {_fmt_date(item.due)}?",
                        evidence=[
                            _item_evidence(item, "status"),
                            _item_evidence(item, "due"),
                            _item_evidence(item, "assignee"),
                        ],
                    )
                )
            else:
                findings.append(
                    Finding(
                        id=f"stale:{item.key}",
                        kind="stale",
                        severity=58,
                        confidence="fact",
                        project=item.project,
                        title=f"{item.key} is {item.status} and has not been updated since {_fmt_date(item.updated)}.",
                        summary=(
                            f"{item.key} ({item.summary}) is {item.status}. "
                            f"The newest update is {_fmt_date(item.updated)}."
                        ),
                        blocks_delivery=False,
                        suggested_question=f"What moved on {item.key} ({item.summary}) since {_fmt_date(item.updated)}?",
                        evidence=[
                            _item_evidence(item, "status"),
                            _item_evidence(item, "updated"),
                            _item_evidence(item, "assignee"),
                        ],
                    )
                )
            covered.add(item.key)

    for item in data["items"]:
        if _status(item) != "blocked" or _is_done(item.status):
            continue
        blocker = data["by_key"].get(item.blocked_by) if item.blocked_by else None
        evidence = [_item_evidence(item, "status"), _item_evidence(item, "blocked_by")]
        if blocker is None:
            findings.append(
                Finding(
                    id=f"blocked:{item.key}",
                    kind="blocked",
                    severity=68,
                    confidence="fact",
                    project=item.project,
                    title=f"{item.key} is blocked and names no open blocker.",
                    summary=f"{item.key} ({item.summary}) is Blocked and blocked_by is empty or unknown.",
                    blocks_delivery=True,
                    suggested_question=f"What is {item.key} waiting on?",
                    evidence=evidence,
                )
            )
            covered.add(item.key)
            continue
        evidence.extend(
            [
                _item_evidence(blocker, "status"),
                _item_evidence(blocker, "assignee"),
                _item_evidence(blocker, "due"),
            ]
        )
        if _is_done(blocker.status):
            findings.append(
                Finding(
                    id=f"blocked:{item.key}",
                    kind="blocker_finished",
                    severity=48,
                    confidence="fact",
                    project=item.project,
                    title=f"{item.key} is still blocked by {blocker.key}, which is already {blocker.status}.",
                    summary=(
                        f"{item.key} ({item.summary}) points at {blocker.key}, "
                        f"and that item is {blocker.status}."
                    ),
                    blocks_delivery=False,
                    suggested_question=f"Can {item.key} leave Blocked now that {blocker.key} is {blocker.status}?",
                    evidence=evidence,
                )
            )
            covered.add(item.key)
            continue
        severity = 70
        qualifier = []
        if _overdue(blocker, as_of) or not blocker.assignee:
            severity = 85
        if _overdue(blocker, as_of):
            qualifier.append(f"overdue ({_fmt_date(blocker.due)})")
        if not blocker.assignee:
            qualifier.append("unassigned")
        qualifier_text = " and ".join(qualifier) if qualifier else "still open"
        milestone_bits = []
        for dep in data["dependencies"]:
            if dep["predecessor"] != item.key:
                continue
            milestone = data["milestone_by_id"].get(dep["successor"])
            if milestone is None or _milestone_done(milestone):
                continue
            evidence.append(_ev("dependencies", dep["id"], "successor", dep["successor"]))
            evidence.append(_ev("milestones", milestone["id"], "due", milestone["due"]))
            milestone_bits.append(f"{milestone['name']} ({milestone['id']}) is due {_fmt_date(milestone['due'])}")
        milestone_text = (" " + " ".join(milestone_bits) + ".") if milestone_bits else ""
        findings.append(
            Finding(
                id=f"blocked:{item.key}",
                kind="blocked",
                severity=severity,
                confidence="fact",
                project=item.project,
                title=f"{item.key} is blocked by {blocker.key}, which is {qualifier_text}.",
                summary=(
                    f"{item.key} ({item.summary}) is Blocked by {blocker.key} ({blocker.summary}). "
                    f"{blocker.key} is {blocker.status}"
                    + (f", {qualifier_text}" if qualifier else "")
                    + f".{milestone_text}"
                ),
                blocks_delivery=True,
                suggested_question=(
                    f"Does the dependent milestone still hold while {item.key} is blocked by {blocker.key}?"
                ),
                evidence=evidence,
            )
        )
        covered.add(item.key)

    for epic in data["items"]:
        if epic.type.strip().lower() != "epic" or epic.percent_complete is None:
            continue
        if epic.percent_complete < 70:
            continue
        children = [
            child
            for child in _children(data, epic.key)
            if child.type.strip().lower() != "epic" and child.story_points is not None
        ]
        total = sum(child.story_points or 0 for child in children)
        if total <= 0:
            continue
        done_points = sum(child.story_points or 0 for child in children if _is_done(child.status))
        ratio = done_points / total
        if ratio > 0.5:
            continue
        reported = _fmt_num(epic.percent_complete)
        done_label = _fmt_num(done_points)
        total_label = _fmt_num(total)
        percent_done = round(ratio * 100)
        open_critical = [
            child
            for child in _children(data, epic.key)
            if not _is_done(child.status) and _priority(child) in HIGH
        ]
        evidence = [
            _item_evidence(epic, "percent_complete"),
            *[
                piece
                for child in children
                for piece in (_item_evidence(child, "status"), _item_evidence(child, "story_points"))
            ],
        ]
        note_sentence = ""
        for note in data["notes"]:
            if note["project"] != epic.project:
                continue
            if reported in note["text"] or ONLY_OPEN.search(note["text"]):
                evidence.append(_ev("notes", note["id"], "text", note["text"]))
                note_sentence = f" {note['id']} says {_quote(note['text'])}"
                break
        critical_sentence = ""
        if open_critical:
            critical_sentence = (
                " Still open at High or Highest: "
                + ", ".join(f"{child.key} ({child.summary})" for child in open_critical)
                + "."
            )
        findings.append(
            Finding(
                id=f"completion:{epic.key}",
                kind="misleading_completion",
                severity=80,
                confidence="fact",
                project=epic.project,
                title=(
                    f"{names[epic.project]} reports {reported}% complete; "
                    f"{done_label} of {total_label} story points are done."
                ),
                summary=(
                    f"{epic.key} percent_complete is {reported}. "
                    f"Child items with story points total {total_label}, of which {done_label} "
                    f"are Done ({percent_done}%).{critical_sentence}{note_sentence}"
                ),
                blocks_delivery=False,
                suggested_question=(
                    f"Which completion figure should the steering pack use: {reported}% "
                    f"or {done_label} of {total_label} story points?"
                ),
                evidence=evidence,
            )
        )

    for dep in data["dependencies"]:
        pred = dep["predecessor"]
        succ = dep["successor"]
        if pred not in data["by_key"] and pred not in data["milestone_by_id"]:
            continue
        if succ not in data["by_key"] and succ not in data["milestone_by_id"]:
            continue
        if not _record_overdue(data, pred, as_of) or _record_done(data, pred):
            continue
        if not _record_done(data, succ):
            continue
        succ_item = data["by_key"].get(succ)
        pred_label = pred
        milestone = data["milestone_by_id"].get(pred)
        evidence = [
            _ev("dependencies", dep["id"], "predecessor", pred),
            _ev("dependencies", dep["id"], "successor", succ),
        ]
        if milestone is not None:
            pred_label = f"{milestone['name']} ({pred})"
            evidence.extend(
                [
                    _ev("milestones", pred, "status", milestone["status"]),
                    _ev("milestones", pred, "due", milestone["due"]),
                ]
            )
        if succ_item is not None:
            evidence.append(_item_evidence(succ_item, "status"))
        note_sentence = ""
        project = succ_item.project if succ_item is not None else (milestone["project"] if milestone else "")
        for note in _related_notes(data, project, [succ, pred]):
            evidence.append(_ev("notes", note["id"], "text", note["text"]))
            note_sentence = f" {note['id']} says {_quote(note['text'])}"
            break
        set_aside.append(
            Finding(
                id=f"non_blocking:{dep['id']}",
                kind="non_blocking_dependency",
                severity=15,
                confidence="fact",
                project=project,
                title=f"{pred_label} is overdue, and it does not block {succ}.",
                summary=(
                    f"{dep['id']} says {pred} precedes {succ}. "
                    f"{pred_label} is overdue, and {succ} is Done.{note_sentence}"
                ),
                blocks_delivery=False,
                suggested_question="",
                evidence=evidence,
            )
        )

    decisions = []
    for item in data["items"]:
        if _is_done(item.status):
            continue
        if item.type.strip().lower() != "decision" and _status(item) != "waiting":
            continue
        successors = [
            dep["successor"]
            for dep in data["dependencies"]
            if dep["predecessor"] == item.key
        ]
        unknowns = []
        if not item.assignee:
            unknowns.append("owner")
        decisions.append(
            {
                "key": item.key,
                "project": item.project,
                "project_name": names.get(item.project, item.project),
                "summary": item.summary,
                "status": item.status,
                "owner": item.assignee or "missing",
                "due": item.due.isoformat() if item.due else None,
                "overdue": _overdue(item, as_of),
                "successors": successors,
                "unknowns": unknowns,
                "comment": item.comment,
                "evidence": [
                    _item_evidence(item, "status").to_dict(),
                    _item_evidence(item, "assignee").to_dict(),
                    _item_evidence(item, "due").to_dict(),
                    *[
                        _ev("dependencies", dep["id"], "successor", dep["successor"]).to_dict()
                        for dep in data["dependencies"]
                        if dep["predecessor"] == item.key
                    ],
                ],
            }
        )

    previous_by_key = {}
    for row in data["previous"]:
        key = _clean(row.get("key"))
        if key:
            previous_by_key[key] = row
    changes = []
    compared = ("status", "assignee", "priority", "due", "blocked_by", "percent_complete")
    current_keys = set(data["by_key"])
    for key in sorted(current_keys & set(previous_by_key)):
        item = data["by_key"][key]
        old = previous_by_key[key]
        fields = []
        for name in compared:
            current = _clean(str(getattr(item, name) if name != "percent_complete" else (
                "" if item.percent_complete is None else _fmt_num(item.percent_complete)
            )))
            if name == "due":
                current = item.due.isoformat() if item.due else ""
            prior = _clean(old.get(name))
            if name == "percent_complete" and prior:
                prior = _fmt_num(float(prior)) if _parse_number(prior, key, []) is not None else prior
            if current != prior:
                fields.append({"field": name, "before": prior or "blank", "after": current or "blank"})
        if fields:
            changes.append(
                {
                    "key": key,
                    "project": item.project,
                    "summary": item.summary,
                    "fields": fields,
                }
            )
    for key in sorted(current_keys - set(previous_by_key)):
        if previous_by_key:
            item = data["by_key"][key]
            changes.append({"key": key, "project": item.project, "summary": item.summary, "fields": [{"field": "key", "before": "absent", "after": "added"}]})
    for key in sorted(set(previous_by_key) - current_keys):
        changes.append({"key": key, "project": _clean(previous_by_key[key].get("project")), "summary": _clean(previous_by_key[key].get("summary")), "fields": [{"field": "key", "before": "present", "after": "removed"}]})

    dropped = 0
    kept_findings = []
    for finding in findings:
        if finding.evidence and all(item.id and item.field for item in finding.evidence):
            kept_findings.append(finding)
        else:
            dropped += 1
    kept_aside = []
    for finding in set_aside:
        if finding.evidence and all(item.id and item.field for item in finding.evidence):
            kept_aside.append(finding)
        else:
            dropped += 1
    findings = _collapse_unassigned(kept_findings)
    set_aside = kept_aside

    min_severity = data["min_severity"]
    ranked = sorted(
        [finding for finding in findings if finding.severity >= min_severity],
        key=lambda finding: (
            -finding.severity,
            PRIORITY_RANK.get(_priority_label(finding).casefold(), 9),
            finding.id,
        ),
    )
    top = ranked[: data["top_n"]]
    also = [finding for finding in ranked if finding not in top]

    projects = []
    blocked_projects = {
        item.project
        for item in data["items"]
        if _status(item) == "blocked" and not _is_done(item.status)
    }
    severe_projects = {finding.project for finding in findings if finding.severity >= min_severity}
    for project in sorted(names):
        if project in blocked_projects:
            state = "blocked"
        elif project in severe_projects:
            state = "needs_attention"
        elif _rules_could_not_run(data["items"], project):
            state = "unchecked"
        else:
            state = "on_track"
        projects.append({"id": project, "name": names[project], "state": state})

    limits = _file_limits(data["items"])
    on_track = [row["name"] for row in projects if row["state"] == "on_track"]
    disclaimer = "This brief cites rows in the approved export. It does not forecast that a future date will be missed."
    count = next((finding for finding in top if finding.kind == "unassigned_count"), None)
    unchecked = [row["name"] for row in projects if row["state"] == "unchecked"]
    if count is not None:
        gap = " Lateness is left out because the file has no due dates." if any(
            item.startswith("no due dates") for item in limits
        ) else ""
        lead = f"As of {_fmt_date(as_of)}, {count.title}{gap} {disclaimer}"
    elif unchecked and not severe_projects:
        gap = "; ".join(limits) if limits else "the export does not support the rules"
        lead = f"As of {_fmt_date(as_of)}, {', '.join(unchecked)} cannot be called on track. {gap}. {disclaimer}"
    else:
        lead = (
            f"As of {_fmt_date(as_of)}, {len(on_track)} of {len(projects)} projects "
            f"{'is' if len(on_track) == 1 else 'are'} on track"
            + (f" ({', '.join(on_track)})" if on_track else "")
            + ". "
            + disclaimer
        )

    questions = []
    for finding in top:
        if finding.suggested_question and finding.suggested_question not in questions:
            questions.append(finding.suggested_question)
    for decision in decisions:
        if decision["unknowns"] or decision["overdue"]:
            successor = (
                f" Recorded successor: {_and(decision['successors'])}."
                if decision["successors"]
                else ""
            )
            owner = (
                "No owner is named."
                if decision["owner"] == "missing"
                else f"The owner is {decision['owner']}."
            )
            question = f"Who can close {decision['key']} ({decision['summary']})? {owner}{successor}"
            if question not in questions:
                questions.append(question)
    actions = [
        {
            "text": question,
            "requires_approval": True,
            "status": "proposed",
        }
        for question in questions
    ]

    traceable = dropped == 0 and all(finding.evidence for finding in findings + set_aside)
    identity = {
        "top": [finding.id for finding in top],
        "aside": [finding.id for finding in set_aside],
        "decisions": [decision["key"] for decision in decisions],
        "pulse": [[row["id"], row["state"]] for row in projects],
        "changes": [
            [
                change["key"],
                [[field["field"], field["before"], field["after"]] for field in change["fields"]],
            ]
            for change in changes
        ],
    }
    result_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:10]
    return {
        "portfolio": data["meta"]["portfolio"],
        "as_of": as_of.isoformat(),
        "audience": data["meta"].get("audience", ""),
        "description": data["meta"].get("description", ""),
        "approval": "pending",
        "synthetic": bool(data["meta"].get("synthetic", False)),
        "source_profile": data["source_profile"],
        "item_count": len(data["items"]),
        "rules_version": RULES_VERSION,
        "result_id": result_id,
        "lead": lead,
        "limits": limits,
        "pulse": projects,
        "top_risks": [finding.to_dict() for finding in top],
        "also_noted": [finding.to_dict() for finding in also],
        "set_aside": [finding.to_dict() for finding in set_aside],
        "decisions": decisions,
        "changes": changes,
        "has_previous": bool(data["previous"]),
        "questions": questions,
        "actions": actions,
        "data_quality": data["issues"],
        "rules": {
            "stale_days": data["stale_days"],
            "completion_reported_at_least": 70,
            "completion_done_ratio_at_most": 0.5,
            "top_n": data["top_n"],
            "min_severity": min_severity,
        },
        "quality": {
            "traceable": traceable,
            "findings": len(findings),
            "set_aside": len(set_aside),
            "unsupported_dropped": dropped,
            "approval": "pending",
        },
    }


def _shown(value: object) -> str:
    text = "" if value is None else str(value)
    return text if text else "(blank)"


def _fmt_iso(value: str | None) -> str:
    if not value:
        return "no date"
    parsed = _parse_date(value, "date", [])
    return _fmt_date(parsed) if parsed else value


def _finding_md(finding: dict) -> list[str]:
    evidence = ", ".join(
        f"{item['source']}:{item['id']}.{item['field']}={_shown(item['value'])}"
        for item in finding["evidence"]
    )
    return [
        f"### {finding['title']}",
        "",
        finding["summary"],
        "",
        f"Confidence: {finding['confidence']}. Severity: {finding['severity']}.",
        "",
        f"Evidence: {evidence}",
        "",
    ]


def render_markdown(brief: dict) -> str:
    lines = [
        f"# {brief['portfolio']}",
        "",
        f"Weekly delivery brief · {brief['as_of']} · Audience: {brief['audience'] or 'not set'}",
        "",
        (
            f"Source: {brief.get('source_profile', 'canonical')} · "
            f"{brief.get('item_count', 0)} work items · rules {brief.get('rules_version', RULES_VERSION)} · "
            f"result {brief.get('result_id', '')}"
        ),
        "",
        "**Approval: pending.** A person still has to check this brief before it goes to a customer.",
        "",
        brief["lead"],
        "",
        "## Delivery pulse",
        "",
        "| Project | State |",
        "| --- | --- |",
    ]
    labels = {
        "on_track": "On track",
        "needs_attention": "Needs attention",
        "blocked": "Blocked",
        "unchecked": "Not checked",
    }
    for row in brief["pulse"]:
        lines.append(f"| {row['name']} | {labels[row['state']]} |")
    lines.extend(["", "## Top risks", ""])
    if not brief["top_risks"]:
        lines.append("No risk cleared the evidence rules.")
        lines.append("")
    for finding in brief["top_risks"]:
        lines.extend(_finding_md(finding))
    lines.extend(["## Changes since the previous export", ""])
    if not brief["changes"]:
        lines.append("No previous export was supplied, or nothing in the compared fields changed.")
        lines.append("")
    for change in brief["changes"]:
        bits = ", ".join(
            f"{field['field']}: {field['before']} → {field['after']}" for field in change["fields"]
        )
        lines.append(f"- {change['key']} ({change['summary']}): {bits}.")
    lines.extend(["", "## Decisions needed", ""])
    if not brief["decisions"]:
        lines.append("No open decision or waiting item is in the export.")
        lines.append("")
    for decision in brief["decisions"]:
        due = _fmt_iso(decision["due"])
        successor = f" Successor: {', '.join(decision['successors'])}." if decision["successors"] else ""
        unknown = f" Unknown: {', '.join(decision['unknowns'])}." if decision["unknowns"] else ""
        overdue = " Overdue." if decision["overdue"] else ""
        lines.append(
            f"- {decision['key']} ({decision['summary']}): owner {decision['owner']}, due {due}.{overdue}{unknown}{successor}"
        )
    lines.extend(["", "## Set aside", ""])
    if not brief["set_aside"]:
        lines.append("No candidate signal was rejected.")
        lines.append("")
    for finding in brief["set_aside"]:
        lines.append(f"- {finding['summary']}")
    lines.extend(["", "## Also noted", ""])
    if not brief["also_noted"]:
        lines.append("Nothing else cleared the severity threshold.")
        lines.append("")
    for finding in brief["also_noted"]:
        lines.append(f"- {finding['title']}")
    lines.extend(["", "## Questions for the delivery lead", ""])
    for question in brief["questions"]:
        lines.append(f"- {question}")
    lines.extend(
        [
            "",
            "## Proposed actions",
            "",
            "The questions above are the proposed actions. Nothing has been sent, and nothing in a project system changes until a person approves it.",
            "",
        ]
    )
    lines.extend(["", "## How this brief was produced", ""])
    rules = brief["rules"]
    lines.extend(
        [
            f"- Stale means an In Progress, Blocked, or Waiting item with no update in {rules['stale_days']} days.",
            f"- A completion figure is treated as misleading when an epic reports at least {rules['completion_reported_at_least']}% and at most half of its child story points are Done.",
            "- An overdue predecessor is set aside when its successor is already Done.",
            "- Jira Blocker counts as Highest and Major counts as High. Three or more unassigned items of one priority are reported as a count.",
            "- Stale and overdue items are raised without a notes file. A note that says the work is on track is still compared with the export.",
            "- Optimistic notes are an inference. The dates and statuses they are compared with are facts.",
            "- These thresholds are operating rules for the sample, not a validated predictor of delay.",
            "",
        ]
    )
    if brief.get("limits"):
        lines.extend(["", "## What this file does not support", ""])
        for item in brief["limits"]:
            lines.append(f"- {item}")
        lines.append("")
    if brief["data_quality"]:
        lines.extend(["## Data quality", ""])
        for issue in brief["data_quality"]:
            lines.append(f"- {issue}")
        lines.append("")
    lines.append("Synthetic sample." if brief["synthetic"] else "Customer export.")
    lines.append("")
    return "\n".join(lines)


def _evidence_html(finding: dict) -> str:
    rows = []
    for item in finding["evidence"]:
        rows.append(
            "<li><code>{}</code> {} = {}</li>".format(
                html.escape(item["source"]),
                html.escape(f"{item['id']}.{item['field']}"),
                html.escape(_shown(item["value"])),
            )
        )
    return "<ul class=\"evidence\">" + "".join(rows) + "</ul>"


def _profile_label(profile: str) -> str:
    return {"jira": "Jira export", "azure_devops": "Azure DevOps export", "canonical": "Canonical export"}.get(
        profile, profile
    )


def render_html(brief: dict) -> str:
    labels = {
        "on_track": "On track",
        "needs_attention": "Needs attention",
        "blocked": "Blocked",
        "unchecked": "Not checked",
    }
    pulse = "".join(
        "<li class=\"card\"><span class=\"state {}\">{}</span><span>{}</span></li>".format(
            html.escape(row["state"]),
            labels[row["state"]],
            html.escape(row["name"]),
        )
        for row in brief["pulse"]
    )
    risks = []
    for finding in brief["top_risks"]:
        count = len(finding["evidence"])
        risks.append(
            "<article class=\"card\">"
            f"<p class=\"kicker\">{html.escape(finding['confidence'])} · severity {finding['severity']}</p>"
            f"<h3>{html.escape(finding['title'])}</h3>"
            f"<p>{html.escape(finding['summary'])}</p>"
            f"<details><summary>Evidence, {count} fields</summary>{_evidence_html(finding)}</details>"
            "</article>"
        )
    if brief["changes"]:
        changes = "".join(
            "<li><strong>{}</strong> {}.</li>".format(
                html.escape(change["key"]),
                html.escape(", ".join(f"{field['field']} {field['before']} → {field['after']}" for field in change["fields"])),
            )
            for change in brief["changes"]
        )
    elif brief.get("has_previous"):
        changes = "<li>No compared field changed.</li>"
    else:
        changes = "<li>No previous export was attached.</li>"
    decisions = []
    for decision in brief["decisions"]:
        extra = []
        if decision["overdue"]:
            extra.append("overdue")
        if decision["unknowns"]:
            extra.append("unknown " + ", ".join(decision["unknowns"]))
        if decision["successors"]:
            extra.append("before " + ", ".join(decision["successors"]))
        decisions.append(
            "<li><strong>{}</strong> {} — owner {}, due {}. {}</li>".format(
                html.escape(decision["key"]),
                html.escape(decision["summary"]),
                html.escape(decision["owner"]),
                html.escape(_fmt_iso(decision["due"])),
                html.escape("; ".join(extra)),
            )
        )
    aside_items = "".join(
        f"<li>{html.escape(finding['summary'])}</li>" for finding in brief["set_aside"]
    ) or "<li>None.</li>"
    aside_count = len(brief["set_aside"])
    if aside_count == 1:
        aside_label = brief["set_aside"][0]["title"]
    elif aside_count == 0:
        aside_label = "Nothing was set aside"
    else:
        aside_label = f"{aside_count} signals set aside"
    noted = "".join(f"<li>{html.escape(finding['title'])}</li>" for finding in brief["also_noted"]) or "<li>None.</li>"
    questions = "".join(f"<li>{html.escape(question)}</li>" for question in brief["questions"])
    stamp = "Synthetic sample · pending approval" if brief["synthetic"] else "Pending approval"
    profile = _profile_label(str(brief.get("source_profile", "canonical")))
    limit_items = "".join(f"<li>{html.escape(item)}</li>" for item in brief.get("limits") or [])
    limits_html = f"<h2>What this file does not support</h2><ul>{limit_items}</ul>" if limit_items else ""
    quality_rows = brief.get("data_quality") or []
    if quality_rows:
        shown = "".join(f"<li>{html.escape(issue)}</li>" for issue in quality_rows[:40])
        more = ""
        if len(quality_rows) > 40:
            more = f"<li>{len(quality_rows) - 40} more lines are in the Markdown brief.</li>"
        quality_html = (
            f"<details><summary>Data quality, {len(quality_rows)} lines</summary><ul>{shown}{more}</ul></details>"
        )
    else:
        quality_html = ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(brief['portfolio'])} delivery brief</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; background: #f4f1ea; color: #1c1917; font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: 52rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    header {{ border-top: 6px solid #1c1917; padding-top: 0.8rem; }}
    .stamp {{ margin: 0; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 650; font-size: 0.72rem; color: #9a3412; }}
    h1 {{ font-size: 1.8rem; line-height: 1.1; margin: 0.35rem 0; }}
    h2 {{ font-size: 1.05rem; margin: 1.4rem 0 0.45rem; }}
    h3 {{ font-size: 1rem; margin: 0 0 0.35rem; }}
    .meta {{ margin: 0; color: #57534e; }}
    .lede {{ font-size: 1.02rem; }}
    .pulse {{ list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0.45rem; }}
    .card {{ background: #fffdf8; border: 1px solid #e7e0d4; padding: 0.7rem 0.8rem; }}
    .pulse .card {{ display: flex; flex-direction: column; gap: 0.2rem; min-height: 3.2rem; }}
    .state {{ font-weight: 700; font-size: 0.72rem; letter-spacing: 0.04em; text-transform: uppercase; }}
    .on_track {{ color: #166534; }}
    .needs_attention {{ color: #a16207; }}
    .blocked {{ color: #9a3412; }}
    .unchecked {{ color: #57534e; }}
    .kicker {{ margin: 0; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: #57534e; }}
    article {{ margin: 0.45rem 0; }}
    details {{ margin-top: 0.35rem; }}
    summary {{ cursor: pointer; color: #44403c; }}
    .evidence {{ font: 0.78rem/1.4 ui-monospace, monospace; color: #44403c; }}
    ul {{ padding-left: 1.1rem; }}
    footer {{ margin-top: 1.5rem; color: #57534e; }}
    @media (max-width: 640px) {{
      h1 {{ font-size: 1.45rem; }}
      .pulse {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <p class="stamp">{html.escape(stamp)}</p>
      <h1>{html.escape(brief['portfolio'])}</h1>
      <p class="meta">{html.escape(brief['as_of'])} · {html.escape(brief['audience'])} · {html.escape(profile)} · {brief.get('item_count', 0)} work items · rules {html.escape(str(brief.get('rules_version', '')))} · result {html.escape(str(brief.get('result_id', '')))}</p>
    </header>
    <p class="lede">{html.escape(brief['lead'])}</p>
    {limits_html}
    {quality_html}
    <h2>Delivery pulse</h2>
    <ul class="pulse">{pulse}</ul>
    <h2>What changed</h2>
    <ul>{changes}</ul>
    <h2>Decisions needed</h2>
    <ul>{''.join(decisions) or '<li>None.</li>'}</ul>
    <h2>Ask in the review</h2>
    <ul>{questions or '<li>None.</li>'}</ul>
    <h2>Top risks</h2>
    {''.join(risks) or '<p>No risk cleared the evidence rules.</p>'}
    <h2>Checked and set aside</h2>
    <details>
      <summary>{html.escape(aside_label)}</summary>
      <ul>{aside_items}</ul>
    </details>
    <h2>Also noted</h2>
    <ul>{noted}</ul>
    <footer>
      <p>Send this file. The recipient does not need an account or the original export. Evidence is under each risk. Nothing on this page has been sent to a project tool.</p>
      <p>A person approves the brief before a customer sees it. The result id changes when the findings change, so two copies can be compared.</p>
      <p>{html.escape(brief['description'])}</p>
    </footer>
  </main>
</body>
</html>
"""


def share_filename(brief: dict) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", brief["portfolio"]).strip("-") or "delivery-brief"
    profile = str(brief.get("source_profile") or "canonical")
    profile_bit = "" if profile == "canonical" else f"-{profile}"
    return f"{slug}{profile_bit}-{brief['as_of']}.html"


def write_brief(brief: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    page = render_html(brief)
    (output / "brief.json").write_text(json.dumps(brief, indent=2) + "\n", encoding="utf-8")
    (output / "brief.md").write_text(render_markdown(brief), encoding="utf-8")
    (output / "brief.html").write_text(page, encoding="utf-8")
    (output / share_filename(brief)).write_text(page, encoding="utf-8")


def build(directory: Path) -> dict:
    return analyse(load_portfolio(directory))


def brief_from_csv(
    source: Path,
    name: str,
    as_of: str,
    previous: Path | None = None,
    profile: str | None = None,
    columns: Path | None = None,
) -> dict:
    staging = _stage_csv(source, name, as_of, previous, profile, columns)
    try:
        return build(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_csv(source: Path, name: str, as_of: str, previous: Path | None, profile: str | None, columns: Path | None) -> Path:
    staging = Path(tempfile.mkdtemp(prefix="delivery-signal-"))
    meta = {
        "portfolio": name,
        "as_of": as_of,
        "audience": "Delivery director",
        "description": "Brief built from a single export file.",
        "synthetic": False,
    }
    if profile:
        meta["profile"] = profile
    (staging / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    shutil.copy(source, staging / "work_items.csv")
    if previous is not None:
        shutil.copy(previous, staging / "previous_work_items.csv")
    if columns is not None:
        shutil.copy(columns, staging / "columns.json")
    return staging


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if not argv_list or (argv_list[0] in {"connect", "import", "report", "help"} and not Path(argv_list[0]).exists()):
        import link

        return link.main(argv_list)
    parser = argparse.ArgumentParser(description="Build an evidence-linked delivery brief.")
    parser.add_argument("portfolio", type=Path, help="Portfolio directory, or one CSV export")
    parser.add_argument("--out", type=Path, help="Output directory (default: <portfolio>/out)")
    parser.add_argument("--name", help="Portfolio name when the input is a single CSV")
    parser.add_argument("--as-of", help="Report date YYYY-MM-DD when the input is a single CSV")
    parser.add_argument("--previous", type=Path, help="Previous CSV, used only for the change list")
    parser.add_argument("--profile", choices=extract.PROFILES, help="Force canonical, jira, or azure_devops")
    parser.add_argument("--columns", type=Path, help="columns.json header overrides")
    args = parser.parse_args(argv_list)
    staging: Path | None = None
    try:
        if args.portfolio.is_file():
            name = args.name or (args.portfolio.stem.strip() or "Portfolio")
            as_of = args.as_of or date.today().isoformat()
            staging = _stage_csv(args.portfolio, name, as_of, args.previous, args.profile, args.columns)
            brief = build(staging)
            output = args.out or (args.portfolio.parent / "out")
        else:
            brief = build(args.portfolio)
            output = args.out or (args.portfolio / "out")
    except (InputError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    if not brief["quality"]["traceable"]:
        print("Brief has a finding without evidence.", file=sys.stderr)
        return 1
    write_brief(brief, output)
    print(
        f"{brief['portfolio']}: {brief['source_profile']}, {len(brief['top_risks'])} top risks, "
        f"{len(brief['set_aside'])} set aside, result {brief['result_id']}, approval {brief['approval']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
