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
from datetime import date, datetime, timedelta
from pathlib import Path

import extract


RULES_VERSION = "2026-10-11"
DONE = {"done", "closed", "resolved"}
ACTIVE = {"in progress", "blocked", "waiting"}
KNOWN_STATUS = {"done", "in progress", "to do", "blocked", "waiting"}
KNOWN_PRIORITY = {"highest", "high", "medium", "low"}
HIGH = {"highest", "high"}
PRIORITY_RANK = {"highest": 0, "high": 1, "medium": 2, "low": 3}
ROLLUP_TYPES = {"epic", "feature", "initiative"}
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
    original_estimate: float | None
    remaining: float | None
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
                    "story_points": "" if item.story_points is None else _fmt_num(item.story_points),
                    "remaining": "" if item.remaining is None else _fmt_num(item.remaining),
                    "type": item.type,
                    "created": item.created.isoformat() if item.created else "",
                    "updated": item.updated.isoformat() if item.updated else "",
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


_NUMBER_FIELDS = {"percent_complete", "story_points", "remaining"}


def _compared_current(item: WorkItem, name: str) -> str:
    if name == "due":
        return item.due.isoformat() if item.due else ""
    if name in _NUMBER_FIELDS:
        value = getattr(item, name)
        return "" if value is None else _fmt_num(value)
    return _clean(str(getattr(item, name) or ""))


def _compared_prior(old: dict, name: str) -> str:
    prior = _clean(old.get(name))
    if name in _NUMBER_FIELDS and prior:
        parsed = _parse_number(prior, name, [])
        if parsed is not None:
            return _fmt_num(parsed)
    return prior


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


def _collapse_missing_estimates(findings: list[Finding]) -> list[Finding]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    rest: list[Finding] = []
    for finding in findings:
        if finding.kind != "missing_estimate":
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
                id=f"missing-estimate-count:{project}:{label}",
                kind="missing_estimate_count",
                severity=ordered[0].severity,
                confidence="fact",
                project=project,
                title=f"{len(items)} {label} priority items have no estimate.",
                summary=(
                    f"{len(items)} open items are {label} priority and the story point or effort cell is empty: "
                    f"{shown}{extra}."
                ),
                blocks_delivery=False,
                suggested_question=f"What is the estimate on the {len(items)} {label} priority items?",
                evidence=evidence,
            )
        )
    return rest + collapsed


def _progress_record(kind: str, ident: str, name: str, project: str, items: list[WorkItem]) -> dict | None:
    counted = [item for item in items if item.story_points is not None]
    omitted = [item for item in items if item.story_points is None]
    paired = [
        item
        for item in items
        if item.original_estimate is not None and item.remaining is not None
    ]
    if not counted and not paired:
        return None
    done_points = sum(item.story_points or 0 for item in counted if _is_done(item.status))
    total_points = sum(item.story_points or 0 for item in counted)
    parts = []
    percent = None
    if total_points > 0:
        percent = round(done_points / total_points * 100)
        parts.append(
            f"{_fmt_num(done_points)} of {_fmt_num(total_points)} story points are Done ({percent}%)."
        )
    if omitted:
        noun = "item has" if len(omitted) == 1 else "items have"
        parts.append(f"{len(omitted)} {noun} no story point or effort.")
    if paired:
        remaining_sum = sum(item.remaining or 0 for item in paired)
        original_sum = sum(item.original_estimate or 0 for item in paired)
        parts.append(
            f"Remaining {_fmt_num(remaining_sum)}, original estimate {_fmt_num(original_sum)}."
        )
    evidence = []
    for item in sorted(counted, key=lambda item: item.key):
        evidence.extend(
            [
                _item_evidence(item, "status").to_dict(),
                _item_evidence(item, "story_points").to_dict(),
            ]
        )
    for item in sorted(omitted, key=lambda item: item.key):
        evidence.append(_item_evidence(item, "story_points").to_dict())
    for item in sorted(paired, key=lambda item: item.key):
        evidence.extend(
            [
                _item_evidence(item, "remaining").to_dict(),
                _item_evidence(item, "original_estimate").to_dict(),
            ]
        )
    return {
        "kind": kind,
        "id": ident,
        "name": name,
        "project": project,
        "text": " ".join(parts),
        "percent": percent,
        "evidence": evidence,
    }


def _portfolio_progress(data: dict, names: dict[str, str]) -> list[dict]:
    rows = []
    for project in sorted(names):
        leaves = [
            item
            for item in data["items"]
            if item.project == project and item.type.strip().lower() not in ROLLUP_TYPES
        ]
        record = _progress_record("project", project, names[project], project, leaves)
        if record:
            rows.append(record)
        parents = [
            item
            for item in data["items"]
            if item.project == project and item.type.strip().lower() in ROLLUP_TYPES
        ]
        for parent in sorted(parents, key=lambda item: item.key):
            record = _progress_record(
                parent.type.strip().lower(),
                parent.key,
                parent.summary,
                project,
                _children(data, parent.key),
            )
            if record:
                rows.append(record)
    return rows


def _span(start: date, end: date) -> str:
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%b %Y')}"
    return f"{_fmt_date(start)} – {_fmt_date(end)}"


def _line(ident: str, text: str, evidence: list[Evidence]) -> dict:
    return {"id": ident, "text": text, "evidence": [item.to_dict() for item in evidence]}


def _change_fields(change: dict) -> dict[str, dict]:
    return {field["field"]: field for field in change["fields"]}


def _moved(before: str, after: str) -> str:
    return f"{before or 'blank'} → {after or 'blank'}"


def _build_reading(
    data: dict,
    names: dict[str, str],
    projects: list[dict],
    progress: list[dict],
    changes: list[dict],
    findings: list[Finding],
) -> dict:
    """A week reading from cells already in the export. Not a forecast."""
    as_of: date = data["as_of"]
    this_start, this_end = as_of, as_of + timedelta(days=6)
    next_start, next_end = as_of + timedelta(days=7), as_of + timedelta(days=13)
    horizon = as_of + timedelta(days=13)
    by_key: dict[str, WorkItem] = data["by_key"]

    by_change = {change["key"]: change for change in changes}
    closed = []
    for change in changes:
        status = _change_fields(change).get("status")
        if status and not _is_done(status["before"]) and _is_done(status["after"]):
            closed.append(change)

    def tight(before: str, after: str) -> str:
        return f"{before}→{after}"

    def day(value: date | None) -> str:
        if value is None:
            return "no date"
        if value.year == as_of.year:
            return f"{value.day} {value.strftime('%b')}"
        return _fmt_date(value)

    progress_lines: list[dict] = []
    if not data["previous"]:
        progress_lines.append(_line("progress:no-previous", "No previous export.", []))
    elif not closed:
        progress_lines.append(_line("progress:none-closed", "Nothing.", []))
    else:
        evidence = []
        points = 0.0
        counted = False
        keys = []
        for change in closed:
            item = by_key[change["key"]]
            keys.append(item.key)
            evidence.append(_item_evidence(item, "status"))
            if item.story_points is not None:
                points += item.story_points
                counted = True
                evidence.append(_item_evidence(item, "story_points"))
        if len(closed) == 1:
            item = by_key[closed[0]["key"]]
            text = f"{item.key}, {_fmt_num(item.story_points)} points." if item.story_points is not None else f"{item.key}."
        else:
            point_bit = f", {_fmt_num(points)} points" if counted else ""
            shown = ", ".join(keys[:3])
            more = f" +{len(keys) - 3}" if len(keys) > 3 else ""
            text = f"{len(closed)}{point_bit}: {shown}{more}."
        progress_lines.append(_line("progress:closed", text, evidence))

    def _fragment(change: dict) -> tuple[int, str, str, list[Evidence]] | None:
        fields = _change_fields(change)
        item = by_key.get(change["key"])
        if "key" in fields and fields["key"]["after"] == "removed":
            return 0, "scope", f"{change['key']} dropped out", []
        if item is None:
            return None
        if "key" in fields and fields["key"]["after"] == "added":
            return 0, "scope", f"{item.key} is new", [_item_evidence(item, "status")]
        status = fields.get("status")
        if status and not _is_done(status["before"]) and _is_done(status["after"]):
            return None
        bits: list[str] = []
        evidence: list[Evidence] = []
        rank = 5
        if "assignee" in fields:
            before, after = fields["assignee"]["before"], fields["assignee"]["after"]
            if after == "blank" and before != "blank":
                bits.append(f"lost {before}")
                rank = min(rank, 0)
            else:
                bits.append(f"owner {tight(before, after)}")
                rank = min(rank, 3)
            evidence.append(_item_evidence(item, "assignee"))
        if "story_points" in fields:
            bits.append(f"estimate {tight(fields['story_points']['before'], fields['story_points']['after'])}")
            rank = min(rank, 2)
            evidence.append(_item_evidence(item, "story_points"))
        if "remaining" in fields:
            before_n = _parse_number(fields["remaining"]["before"], "before", [])
            after_n = _parse_number(fields["remaining"]["after"], "after", [])
            grew = before_n is not None and after_n is not None and after_n > before_n
            bits.append(f"remaining {tight(fields['remaining']['before'], fields['remaining']['after'])}")
            rank = min(rank, 1 if grew else 4)
            evidence.append(_item_evidence(item, "remaining"))
        if "due" in fields:
            bits.append(f"due {tight(fields['due']['before'], fields['due']['after'])}")
            rank = min(rank, 3)
            evidence.append(_item_evidence(item, "due"))
        if "blocked_by" in fields:
            bits.append(f"blocker {tight(fields['blocked_by']['before'], fields['blocked_by']['after'])}")
            rank = min(rank, 1)
            evidence.append(_item_evidence(item, "blocked_by"))
        if "priority" in fields:
            bits.append(f"priority {tight(fields['priority']['before'], fields['priority']['after'])}")
            rank = min(rank, 3)
            evidence.append(_item_evidence(item, "priority"))
        if "percent_complete" in fields:
            bits.append(f"percent {tight(fields['percent_complete']['before'], fields['percent_complete']['after'])}")
            rank = min(rank, 4)
            evidence.append(_item_evidence(item, "percent_complete"))
        if status:
            bits.append(tight(status["before"], status["after"]))
            rank = min(rank, 4)
            evidence.append(_item_evidence(item, "status"))
        if not bits:
            return None
        return rank, "drift", f"{item.key} " + ", ".join(bits), evidence

    changed: list[dict] = []
    if not data["previous"]:
        changed.append(_line("change:no-previous", "No previous export.", []))
    elif not changes:
        changed.append(_line("change:same", "No change.", []))
    else:
        pieces = []
        for change in changes:
            got = _fragment(change)
            if got is not None:
                pieces.append(got)
        if not pieces:
            changed.append(_line("change:only-closed", "Only the close.", []))
        else:
            pieces.sort(key=lambda row: (row[0], row[2]))
            scope = [text for _rank, kind, text, _evidence in pieces if kind == "scope"]
            drift = [text for _rank, kind, text, _evidence in pieces if kind == "drift"]
            evidence = [piece for _rank, _kind, _text, group in pieces for piece in group]
            parts = []
            if scope:
                parts.append(". ".join(scope))
            if drift:
                parts.append(". ".join(drift))
            changed.append(_line("change:drift", ". ".join(parts) + ".", evidence))

    def _remaining_pressure(item: WorkItem) -> tuple[str, str] | None:
        change = by_change.get(item.key)
        remaining = _change_fields(change).get("remaining") if change else None
        if remaining is not None:
            before_n = _parse_number(remaining["before"], "before", [])
            after_n = _parse_number(remaining["after"], "after", [])
            if before_n is not None and after_n is not None and after_n > before_n:
                return "grew", tight(remaining["before"], remaining["after"])
        if (
            item.remaining is not None
            and item.original_estimate is not None
            and item.remaining > item.original_estimate
        ):
            return "above", f"{_fmt_num(item.remaining)} vs {_fmt_num(item.original_estimate)}"
        return None

    clear_bits: list[str] = []
    grew_bits: list[str] = []
    above_bits: list[str] = []
    blocked_bits: list[str] = []
    unowned_bits: list[str] = []
    coming_evidence: list[Evidence] = []
    next_items = [
        item
        for item in data["items"]
        if not _is_done(item.status)
        and item.type.strip().lower() not in ROLLUP_TYPES
        and item.due is not None
        and next_start <= item.due <= next_end
    ]
    next_items.sort(key=lambda item: (item.due or date.max, item.key))
    for item in next_items:
        evidence = [_item_evidence(item, "due"), _item_evidence(item, "status"), _item_evidence(item, "assignee")]
        pressure = _remaining_pressure(item)
        if pressure:
            evidence.extend([_item_evidence(item, "remaining"), _item_evidence(item, "original_estimate")])
        if item.blocked_by:
            evidence.append(_item_evidence(item, "blocked_by"))
            blocked_bits.append(f"{item.key} by {item.blocked_by}")
        elif not item.assignee:
            unowned_bits.append(f"{item.key} {day(item.due)}")
        elif pressure:
            kind, label = pressure
            (grew_bits if kind == "grew" else above_bits).append(f"{item.key} {label}")
        else:
            clear_bits.append(f"{item.key} {item.assignee}")
        coming_evidence.extend(evidence)
    milestone_bits: list[str] = []
    for milestone in sorted(data["milestones"], key=lambda row: (row["due"] or date.max, row["id"])):
        due = milestone["due"]
        if due is None or _milestone_done(milestone) or not (next_start <= due <= next_end):
            continue
        milestone_bits.append(f"{milestone['name']} {day(due)}")
        coming_evidence.extend(
            [
                _ev("milestones", milestone["id"], "due", milestone["due"]),
                _ev("milestones", milestone["id"], "status", milestone["status"]),
            ]
        )
    coming_bits: list[str] = []
    if not next_items and not milestone_bits:
        coming_bits.append("Nothing dated")
    else:
        if clear_bits:
            coming_bits.append("Ready " + ", ".join(clear_bits))
        if grew_bits:
            coming_bits.append("Remaining up " + ", ".join(grew_bits))
        if above_bits:
            coming_bits.append("Over estimate " + ", ".join(above_bits))
        if blocked_bits:
            coming_bits.append("Blocked " + ", ".join(blocked_bits))
        if milestone_bits:
            coming_bits.append(", ".join(milestone_bits) + ", milestone")
        if unowned_bits:
            coming_bits.append(", ".join(unowned_bits) + ", no owner")
    coming = [_line("coming:read", ". ".join(coming_bits) + ".", coming_evidence)]

    forward: dict[str, list[str]] = {}
    for dep in data["dependencies"]:
        forward.setdefault(dep["predecessor"], []).append(dep["successor"])

    def _open_id(ident: str) -> bool:
        item = by_key.get(ident)
        if item is not None:
            return not _is_done(item.status)
        milestone = data["milestone_by_id"].get(ident)
        return milestone is not None and not _milestone_done(milestone)

    def _in_play(ident: str) -> bool:
        item = by_key.get(ident)
        if item is not None:
            if _is_done(item.status):
                return False
            return item.due is None or item.due <= horizon or _overdue(item, as_of)
        milestone = data["milestone_by_id"].get(ident)
        if milestone is None or _milestone_done(milestone):
            return False
        due = milestone["due"]
        return due is not None and due <= horizon

    held_by_open: set[str] = set()
    for pred, succs in forward.items():
        if not _open_id(pred):
            continue
        for succ in succs:
            if _open_id(succ):
                held_by_open.add(succ)

    def _walk(ident: str, seen: set[str]) -> list:
        if ident in seen or not _in_play(ident):
            return []
        seen.add(ident)
        item = by_key.get(ident)
        nodes: list = [item] if item is not None else [data["milestone_by_id"][ident]]
        for succ in forward.get(ident, []):
            nodes.extend(_walk(succ, seen))
        return nodes

    def _chain_phrase(nodes: list) -> str:
        work = [node for node in nodes if not isinstance(node, dict)]
        milestones = [node for node in nodes if isinstance(node, dict)]
        unowned = [item for item in work if not item.assignee]
        mark_each = len(unowned) != len(work)

        def work_bit(item: WorkItem) -> str:
            if mark_each and not item.assignee:
                return f"{item.key} (no owner)"
            return item.key

        if len(work) == 1 and not milestones:
            item = work[0]
            why = []
            if not item.assignee:
                why.append("no owner")
            if item.blocked_by:
                why.append(f"blocked by {item.blocked_by}")
            return item.key + (", " + ", ".join(why) if why else "")
        parts = [work_bit(item) for item in work]
        for row in milestones:
            parts.append(f"{row['name']} {day(row['due'])}" if row["due"] else row["name"])
        text = " → ".join(parts)
        if len(work) > 1 and unowned and len(unowned) == len(work):
            text += ", no owners"
        return text

    def _chain_evidence(nodes: list) -> list[Evidence]:
        evidence: list[Evidence] = []
        ids: set[str] = set()
        for node in nodes:
            if isinstance(node, dict):
                ids.add(node["id"])
                evidence.extend(
                    [
                        _ev("milestones", node["id"], "due", node["due"]),
                        _ev("milestones", node["id"], "status", node["status"]),
                    ]
                )
            else:
                ids.add(node.key)
                evidence.extend(
                    [
                        _item_evidence(node, "summary"),
                        _item_evidence(node, "due"),
                        _item_evidence(node, "status"),
                        _item_evidence(node, "assignee"),
                        _item_evidence(node, "priority"),
                    ]
                )
                if node.blocked_by:
                    evidence.append(_item_evidence(node, "blocked_by"))
        for dep in data["dependencies"]:
            if dep["predecessor"] in ids and dep["successor"] in ids:
                evidence.extend(
                    [
                        _ev("dependencies", dep["id"], "predecessor", dep["predecessor"]),
                        _ev("dependencies", dep["id"], "successor", dep["successor"]),
                    ]
                )
        return evidence

    def _this_window(item: WorkItem) -> bool:
        return _overdue(item, as_of) or (item.due is not None and this_start <= item.due <= this_end)

    roots = []
    for item in data["items"]:
        if _is_done(item.status) or item.key in held_by_open or not _this_window(item):
            continue
        blocks = any(_in_play(succ) for succ in forward.get(item.key, []))
        if _priority(item) == "highest" or item.blocked_by or blocks:
            roots.append(item)
    roots.sort(key=lambda item: (item.due or date.min, item.key))
    chains = []
    chained: set[str] = set()
    for root in roots:
        if root.key in chained:
            continue
        nodes = _walk(root.key, set())
        if not nodes:
            continue
        worth = any(
            isinstance(node, dict) or _priority(node) == "highest" or node.blocked_by or _overdue(node, as_of)
            for node in nodes
        )
        if not worth:
            continue
        for node in nodes:
            chained.add(node["id"] if isinstance(node, dict) else node.key)
        chains.append(nodes)

    priorities: list[dict] = []
    phrases = [_chain_phrase(nodes) for nodes in chains]
    priority_evidence = [piece for nodes in chains for piece in _chain_evidence(nodes)]
    later = [
        item
        for item in data["items"]
        if not _is_done(item.status) and _priority(item) == "highest" and item.key not in chained
    ]
    later.sort(key=lambda item: (item.due or date.max, item.key))
    if later:
        grouped: dict[date | None, list[WorkItem]] = {}
        for item in later:
            grouped.setdefault(item.due, []).append(item)
        parts = []
        evidence = []
        for due in sorted(grouped, key=lambda value: value or date.max):
            group = grouped[due]
            when = day(due) if due else "no date"
            if len(group) == 1:
                item = group[0]
                extra = []
                if not item.assignee:
                    extra.append("no owner")
                if item.story_points is None:
                    extra.append("no estimate")
                tail = " " + " ".join(extra) if extra else ""
                parts.append(f"{item.key}{tail} {when}")
            else:
                extra = []
                if all(not item.assignee for item in group):
                    extra.append("no owner")
                elif any(not item.assignee for item in group):
                    extra.append(", ".join(item.key for item in group if not item.assignee) + " no owner")
                if all(item.story_points is None for item in group):
                    extra.append("no estimate")
                elif any(item.story_points is None for item in group):
                    extra.append(", ".join(item.key for item in group if item.story_points is None) + " no estimate")
                tail = " " + " ".join(extra) if extra else ""
                parts.append(f"{', '.join(item.key for item in group)}{tail} {when}")
            for item in group:
                evidence.extend(
                    [
                        _item_evidence(item, "priority"),
                        _item_evidence(item, "due"),
                        _item_evidence(item, "assignee"),
                        _item_evidence(item, "story_points"),
                    ]
                )
        phrases.append(("Later: " if phrases else "") + "; ".join(parts))
        priority_evidence.extend(evidence)
    if phrases:
        priorities.append(_line("priority:late", ". ".join(phrases) + ".", priority_evidence))
    else:
        priorities.append(_line("priority:none", "None.", []))
    mentioned_milestones = {node["id"] for nodes in chains for node in nodes if isinstance(node, dict)}

    delay: list[dict] = []
    for milestone in sorted(data["milestones"], key=lambda row: (row["due"] or date.max, row["id"])):
        due = milestone["due"]
        if due is None or _milestone_done(milestone) or due > horizon or milestone["id"] in mentioned_milestones:
            continue
        for dep in data["dependencies"]:
            if dep["successor"] != milestone["id"]:
                continue
            pred = by_key.get(dep["predecessor"])
            if pred is None or _is_done(pred.status):
                continue
            blocker = by_key.get(pred.blocked_by) if pred.blocked_by else None
            pressed = (
                _overdue(pred, as_of)
                or _status(pred) == "blocked"
                or not pred.assignee
                or (blocker is not None and (_overdue(blocker, as_of) or not blocker.assignee))
            )
            if not pressed:
                continue
            evidence = [
                _ev("milestones", milestone["id"], "due", milestone["due"]),
                _ev("dependencies", dep["id"], "predecessor", dep["predecessor"]),
                _ev("dependencies", dep["id"], "successor", dep["successor"]),
                _item_evidence(pred, "status"),
                _item_evidence(pred, "due"),
            ]
            if blocker is None:
                who = "" if pred.assignee else " no owner"
                text = f"{milestone['name']} {day(due)} behind {pred.key}{who}"
            else:
                evidence.extend(
                    [
                        _item_evidence(pred, "blocked_by"),
                        _item_evidence(blocker, "status"),
                        _item_evidence(blocker, "assignee"),
                        _item_evidence(blocker, "due"),
                    ]
                )
                who = "" if blocker.assignee else " no owner"
                text = f"{milestone['name']} {day(due)} behind {blocker.key}{who}"
            delay.append(_line(f"delay:milestone:{milestone['id']}", text, evidence))
    for finding in findings:
        if finding.kind == "note_conflict":
            overdue = [
                item
                for item in data["items"]
                if item.project == finding.project and _overdue(item, as_of) and item.type.strip().lower() != "epic"
            ]
            newest = max((item.updated for item in overdue if item.updated), default=None)
            keys = _and([item.key for item in sorted(overdue, key=lambda item: item.key)[:4]])
            note = next((piece for piece in finding.evidence if piece.source == "notes"), None)
            note_id = note.id if note else "A note"
            gap = (as_of - newest).days if newest is not None else None
            age = f", {gap}d" if gap is not None else ""
            delay.append(_line(f"delay:{finding.id}", f"{note_id} vs {keys}{age}", list(finding.evidence)))
        elif finding.kind == "misleading_completion":
            note = next((piece for piece in finding.evidence if piece.source == "notes" and ONLY_OPEN.search(piece.value)), None)
            match = re.search(r"(\d+(?:\.\d+)?)% complete; (\S+) of (\S+) story points", finding.title)
            text = f"{match.group(1)}% is {match.group(2)}/{match.group(3)}" if match else finding.title
            if note:
                text += f". {note.id}: one item"
            delay.append(_line(f"delay:{finding.id}", text, list(finding.evidence)))
    if not delay:
        delay.append(_line("delay:none", "None.", []))
    elif len(delay) > 1:
        delay = [
            {
                "id": "delay:read",
                "text": ". ".join(row["text"].rstrip(".") for row in delay) + ".",
                "evidence": [piece for row in delay for piece in row["evidence"]],
            }
        ]
    elif not delay[0]["text"].endswith("."):
        delay[0]["text"] += "."

    return {
        "this_week": [this_start.isoformat(), this_end.isoformat()],
        "next_week": [next_start.isoformat(), next_end.isoformat()],
        "progress": progress_lines,
        "changed": changed,
        "coming": coming,
        "priorities": priorities,
        "delay_risks": delay,
    }


FLOW_WEEKS = 8
FLOW_SKIP = ROLLUP_TYPES | {"decision"}


def _flow_leaf(type_name: str) -> bool:
    return type_name.strip().lower() not in FLOW_SKIP


def _percentile_rank(count: int, percent: int) -> int:
    return (percent * count + 99) // 100


def _build_flow(data: dict, names: dict[str, str]) -> dict:
    """Counts a table can hold. Weeks come from dates already on the rows."""
    as_of: date = data["as_of"]
    ends = [as_of - timedelta(days=7 * (FLOW_WEEKS - 1 - index)) for index in range(FLOW_WEEKS)]

    def bucket(updated: date | None) -> int | None:
        if updated is None or updated > as_of:
            return None
        index = FLOW_WEEKS - 1 - (as_of - updated).days // 7
        if index < 0:
            return None
        return index

    def pack(ident: str, label: str, items: list[WorkItem], previous: list[dict] | None) -> dict:
        weekly = [0] * FLOW_WEEKS
        latest: list[WorkItem] = []
        for item in items:
            if not _is_done(item.status):
                continue
            index = bucket(item.updated)
            if index is None:
                continue
            weekly[index] += 1
            if index == FLOW_WEEKS - 1:
                latest.append(item)
        since = []
        previous_by_key = {row.get("key"): row for row in previous} if previous is not None else {}
        if previous is not None:
            for item in items:
                if not _is_done(item.status):
                    continue
                prior = previous_by_key.get(item.key)
                if prior is None or not _is_done(str(prior.get("status", ""))):
                    since.append(item)
        open_items = [item for item in items if not _is_done(item.status)]
        wip_previous = None
        if previous is not None:
            wip_previous = sum(
                1
                for row in previous
                if not _is_done(str(row.get("status", ""))) and _flow_leaf(str(row.get("type", "")))
            )
        cycles = [
            ((item.updated - item.created).days, item)
            for item in items
            if _is_done(item.status) and item.created is not None and item.updated is not None and item.updated >= item.created
        ]
        cycles.sort(key=lambda pair: (pair[0], pair[1].key))
        p85_days = None
        p85_item = None
        if cycles:
            p85_days, p85_item = cycles[_percentile_rank(len(cycles), 85) - 1]
        since_points = sum(item.story_points or 0 for item in since)
        since_has_points = any(item.story_points is not None for item in since)
        evidence: list[Evidence] = []
        for item in since:
            evidence.extend([_item_evidence(item, "status"), _item_evidence(item, "updated")])
            if item.story_points is not None:
                evidence.append(_item_evidence(item, "story_points"))
        for item in latest:
            evidence.extend([_item_evidence(item, "status"), _item_evidence(item, "updated"), _item_evidence(item, "type")])
        for item in open_items:
            evidence.append(_item_evidence(item, "status"))
        if p85_item is not None:
            evidence.extend([_item_evidence(p85_item, "created"), _item_evidence(p85_item, "updated")])
        return {
            "id": ident,
            "name": label,
            "closed": len(since) if previous is not None else None,
            "closed_points": since_points if since_has_points else None,
            "wip": len(open_items),
            "wip_previous": wip_previous,
            "p85_days": p85_days,
            "p85_n": len(cycles),
            "p85_key": p85_item.key if p85_item is not None else "",
            "weekly": weekly,
            "evidence": [item.to_dict() for item in evidence],
        }

    leaves = [item for item in data["items"] if _flow_leaf(item.type)]
    previous = data["previous"] if data["previous"] else None
    rows = [pack("all", "All", leaves, previous)]
    for project in sorted(names):
        project_previous = None
        if previous is not None:
            project_previous = [row for row in previous if row.get("project") == project]
        rows.append(
            pack(
                project,
                names[project].split(" — ")[0].split(" - ")[0],
                [item for item in leaves if item.project == project],
                project_previous,
            )
        )
    scale = max((value for row in rows for value in row["weekly"]), default=0)
    return {
        "week_ends": [day.isoformat() for day in ends],
        "scale": scale,
        "rows": rows,
    }


def _iso_day(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _week_index(as_of: date, day: date | None) -> int | None:
    if day is None or day > as_of:
        return None
    index = FLOW_WEEKS - 1 - (as_of - day).days // 7
    return index if index >= 0 else None


def _p85_days(cycles: list[int]) -> int | None:
    if not cycles:
        return None
    ordered = sorted(cycles)
    return ordered[_percentile_rank(len(ordered), 85) - 1]


def _judge(delta: int | None, better: str | None) -> str:
    if delta is None or delta == 0 or not better:
        return "same"
    improved = (better == "up" and delta > 0) or (better == "down" and delta < 0)
    return "better" if improved else "worse"


def _build_metrics(data: dict, names: dict[str, str]) -> dict:
    """KPI rows a team filter can swap. Targets are dates already in the file."""
    as_of: date = data["as_of"]
    ends = [as_of - timedelta(days=7 * (FLOW_WEEKS - 1 - index)) for index in range(FLOW_WEEKS)]
    leaves = [item for item in data["items"] if _flow_leaf(item.type)]
    previous = data["previous"] or []
    previous_by_key = {row.get("key"): row for row in previous}

    def previous_rows(project: str | None) -> list[dict] | None:
        if not previous:
            return None
        rows = previous if project is None else [row for row in previous if row.get("project") == project]
        return [row for row in rows if _flow_leaf(str(row.get("type", "")))]

    def was_done(item: WorkItem) -> bool:
        prior = previous_by_key.get(item.key)
        return prior is not None and _is_done(str(prior.get("status", "")))

    def pack(project: str | None, items: list[WorkItem]) -> list[dict]:
        weekly_done = [0] * FLOW_WEEKS
        weekly_due = [0] * FLOW_WEEKS
        weekly_due_done = [0] * FLOW_WEEKS
        weekly_wip = [0] * FLOW_WEEKS
        for item in items:
            if _is_done(item.status):
                done_index = _week_index(as_of, item.updated)
                if done_index is not None:
                    weekly_done[done_index] += 1
            if item.due is not None:
                due_index = _week_index(as_of, item.due)
                if due_index is not None:
                    weekly_due[due_index] += 1
                    if _is_done(item.status):
                        weekly_due_done[due_index] += 1
        for end_index, end in enumerate(ends):
            for item in items:
                if item.created is not None and item.created > end:
                    continue
                if _is_done(item.status) and item.updated is not None and item.updated <= end:
                    continue
                if item.created is None and _is_done(item.status):
                    continue
                weekly_wip[end_index] += 1
        weekly_p85: list[int | None] = []
        for end in ends:
            cycles = [
                (item.updated - item.created).days
                for item in items
                if _is_done(item.status)
                and item.created is not None
                and item.updated is not None
                and item.created <= item.updated <= end
            ]
            weekly_p85.append(_p85_days(cycles))
        prior = previous_rows(project)
        since = [item for item in items if _is_done(item.status) and not was_done(item)] if prior is not None else []
        open_items = [item for item in items if not _is_done(item.status)]
        dated = [item for item in items if item.due is not None and item.due <= as_of]
        dated_done = [item for item in dated if _is_done(item.status)]
        blocked = [
            item
            for item in open_items
            if item.status.strip().lower() == "blocked" or bool(item.blocked_by)
        ]
        unassigned = [item for item in open_items if not item.assignee.strip()]
        wip_previous = None
        blocked_previous = None
        unassigned_previous = None
        p85_previous = None
        still_previous = None
        if prior is not None:
            wip_previous = sum(1 for row in prior if not _is_done(str(row.get("status", ""))))
            blocked_previous = sum(
                1
                for row in prior
                if not _is_done(str(row.get("status", "")))
                and (str(row.get("status", "")).strip().lower() == "blocked" or bool(row.get("blocked_by")))
            )
            unassigned_previous = sum(
                1
                for row in prior
                if not _is_done(str(row.get("status", ""))) and not str(row.get("assignee", "")).strip()
            )
            prior_cycles = []
            for row in prior:
                if not _is_done(str(row.get("status", ""))):
                    continue
                created = _iso_day(row.get("created"))
                updated = _iso_day(row.get("updated"))
                if created is None or updated is None or updated < created:
                    continue
                prior_cycles.append((updated - created).days)
            p85_previous = _p85_days(prior_cycles)
            still_previous = sum(1 for item in dated if not was_done(item))
        cycles_now = [
            (item.updated - item.created).days
            for item in items
            if _is_done(item.status)
            and item.created is not None
            and item.updated is not None
            and item.updated >= item.created
        ]
        p85_now = _p85_days(cycles_now)
        since_points = [item.story_points for item in since if item.story_points is not None]
        latest = weekly_done[-1]
        prior_week = weekly_done[-2] if len(weekly_done) > 1 else 0
        return [
            {
                "id": "closed",
                "name": "Since last export",
                "actual": len(since) if prior is not None else None,
                "actual_note": f"{_fmt_num(sum(since_points))} pts" if since_points else "",
                "target": None,
                "gap": None,
                "delta": None,
                "better": None,
                "weekly": weekly_done,
                "target_weekly": None,
            },
            {
                "id": "throughput",
                "name": "Marked Done",
                "actual": latest,
                "actual_note": "latest week",
                "target": weekly_due[-1],
                "gap": weekly_due[-1] - latest,
                "delta": latest - prior_week,
                "better": "up",
                "weekly": weekly_done,
                "target_weekly": weekly_due,
            },
            {
                "id": "dates",
                "name": "Dates reached",
                "actual": len(dated_done),
                "actual_note": "",
                "target": len(dated),
                "gap": len(dated) - len(dated_done),
                "delta": None if still_previous is None else len(dated_done) - (len(dated) - still_previous),
                "better": "up",
                "weekly": weekly_due_done,
                "target_weekly": weekly_due,
            },
            {
                "id": "wip",
                "name": "WIP",
                "actual": len(open_items),
                "actual_note": "",
                "target": None,
                "gap": None,
                "delta": None if wip_previous is None else len(open_items) - wip_previous,
                "better": "down",
                "weekly": weekly_wip,
                "target_weekly": None,
            },
            {
                "id": "cycle",
                "name": "Cycle p85",
                "actual": p85_now,
                "actual_note": f"{len(cycles_now)} finished" if cycles_now else "",
                "target": None,
                "gap": None,
                "delta": None if p85_now is None or p85_previous is None else p85_now - p85_previous,
                "better": "down",
                "weekly": weekly_p85,
                "target_weekly": None,
            },
            {
                "id": "blocked",
                "name": "Blocked",
                "actual": len(blocked),
                "actual_note": "",
                "target": None,
                "gap": None,
                "delta": None if blocked_previous is None else len(blocked) - blocked_previous,
                "better": "down",
                "weekly": None,
                "target_weekly": None,
            },
            {
                "id": "unassigned",
                "name": "Unassigned",
                "actual": len(unassigned),
                "actual_note": "",
                "target": None,
                "gap": None,
                "delta": None if unassigned_previous is None else len(unassigned) - unassigned_previous,
                "better": "down",
                "weekly": None,
                "target_weekly": None,
            },
        ]

    scopes = [{"id": "all", "name": "All", "kpis": pack(None, leaves)}]
    for project in sorted(names):
        scopes.append(
            {
                "id": project,
                "name": names[project].split(" — ")[0].split(" - ")[0],
                "kpis": pack(project, [item for item in leaves if item.project == project]),
            }
        )
    scales: dict[str, int] = {}
    for scope in scopes:
        for kpi in scope["kpis"]:
            values = [value for value in (kpi["weekly"] or []) if isinstance(value, int)]
            values.extend(value for value in (kpi["target_weekly"] or []) if isinstance(value, int))
            scales[kpi["id"]] = max(scales.get(kpi["id"], 0), max(values, default=0))
    for scope in scopes:
        for kpi in scope["kpis"]:
            kpi["scale"] = max(scales.get(kpi["id"], 0), 1)
            kpi["judge"] = _judge(kpi["delta"], kpi["better"])
    return {"week_ends": [day.isoformat() for day in ends], "scopes": scopes}


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

    for item in data["items"]:
        if _is_done(item.status) or _priority(item) not in HIGH or item.story_points is not None:
            continue
        findings.append(
            Finding(
                id=f"missing-estimate:{item.key}",
                kind="missing_estimate",
                severity=56,
                confidence="fact",
                project=item.project,
                title=f"{item.key} is {item.priority} priority, open, and has no estimate.",
                summary=(
                    f"{item.key} ({item.summary}) is {item.status} and {item.priority} priority. "
                    "The story point or effort cell is empty."
                ),
                blocks_delivery=False,
                suggested_question=f"What is the estimate on {item.key} ({item.summary})?",
                evidence=[
                    _item_evidence(item, "status"),
                    _item_evidence(item, "priority"),
                    _item_evidence(item, "story_points"),
                ],
            )
        )

    for item in data["items"]:
        if item.original_estimate is None or item.remaining is None or item.remaining <= item.original_estimate:
            continue
        original = _fmt_num(item.original_estimate)
        left = _fmt_num(item.remaining)
        findings.append(
            Finding(
                id=f"remaining-above:{item.key}",
                kind="remaining_above_original",
                severity=62,
                confidence="fact",
                project=item.project,
                title=f"{item.key} remaining is {left} and the original estimate is {original}.",
                summary=(
                    f"{item.key} ({item.summary}) is {item.status}. "
                    f"Remaining is {left}. The original estimate is {original}."
                ),
                blocks_delivery=False,
                suggested_question=(
                    f"Why is remaining {left} on {item.key} ({item.summary}) "
                    f"when the original estimate is {original}?"
                ),
                evidence=[
                    _item_evidence(item, "remaining"),
                    _item_evidence(item, "original_estimate"),
                ],
            )
        )

    for item in data["items"]:
        if not _is_done(item.status) or item.remaining is None or item.remaining == 0:
            continue
        left = _fmt_num(item.remaining)
        findings.append(
            Finding(
                id=f"done-remaining:{item.key}",
                kind="done_remaining",
                severity=54,
                confidence="fact",
                project=item.project,
                title=f"{item.key} is Done and remaining is {left}.",
                summary=f"{item.key} ({item.summary}) is {item.status} and the remaining cell is {left}.",
                blocks_delivery=False,
                suggested_question=f"Why does {item.key} ({item.summary}) still have remaining {left} after it was marked Done?",
                evidence=[
                    _item_evidence(item, "status"),
                    _item_evidence(item, "remaining"),
                ],
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
    compared = (
        "status",
        "assignee",
        "priority",
        "due",
        "blocked_by",
        "percent_complete",
        "story_points",
        "remaining",
    )
    current_keys = set(data["by_key"])
    for key in sorted(current_keys & set(previous_by_key)):
        item = data["by_key"][key]
        old = previous_by_key[key]
        fields = []
        for name in compared:
            current = _compared_current(item, name)
            prior = _compared_prior(old, name)
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
    findings = _collapse_missing_estimates(_collapse_unassigned(kept_findings))
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

    progress = _portfolio_progress(data, names)
    project_progress = {row["id"]: row["text"] for row in progress if row["kind"] == "project"}
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
        projects.append(
            {
                "id": project,
                "name": names[project],
                "state": state,
                "progress": project_progress.get(project, ""),
            }
        )

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

    reading = _build_reading(data, names, projects, progress, changes, findings)
    flow = _build_flow(data, names)
    metrics = _build_metrics(data, names)

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
        "reading": reading,
        "flow": flow,
        "metrics": metrics,
        "limits": limits,
        "pulse": projects,
        "progress": progress,
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


_READING_SECTIONS = (
    ("progress", "Closed", "What reached Done."),
    ("changed", "Moved", "Field movement other than the close."),
    ("coming", "Next week", "Dates in the seven days after this week."),
    ("priorities", "Late", "Chains already late, then Highest work after that."),
    ("delay_risks", "Disagrees", "A note or a completion percent the rows contradict."),
)
_WEEK_NOTE = "Five lines from the export. A due date is the date in the file. Evidence is the cells. The field list below is every change."


def _reading_md(brief: dict) -> list[str]:
    reading = brief.get("reading") or {}
    if not reading:
        return []
    this_week = re.sub(r"\s+\d{4}$", "", _span(date.fromisoformat(reading["this_week"][0]), date.fromisoformat(reading["this_week"][1])))
    next_week = re.sub(r"\s+\d{4}$", "", _span(date.fromisoformat(reading["next_week"][0]), date.fromisoformat(reading["next_week"][1])))
    lines = ["", f"{this_week} · next {next_week}", ""]
    for key, title, _note in _READING_SECTIONS:
        rows = reading.get(key) or []
        text = " ".join(row["text"] for row in rows) if rows else "None."
        lines.append(f"**{title}.** {text}")
    lines.append("")
    return lines


def _week_labels(flow: dict) -> list[str]:
    labels = []
    for value in flow.get("week_ends") or []:
        parsed = date.fromisoformat(value)
        labels.append(f"{parsed.day} {parsed.strftime('%b')}")
    return labels


def _flow_claim(weekly: list[int]) -> str:
    """One sentence a reader can repeat. Counts only, from the portfolio row."""
    weeks = len(weekly)
    noun = "week" if weeks == 1 else "weeks"
    if weeks == 0 or not any(weekly):
        return f"Nothing closed in these {weeks} {noun}."
    quiet = 0
    while quiet < weeks and weekly[quiet] == 0:
        quiet += 1
    active = weekly[quiet:]
    if quiet >= 3 and 1 <= len(active) <= 4:
        quiet_noun = "week" if quiet == 1 else "weeks"
        if len(active) == 1:
            tail = str(active[0])
        else:
            tail = ", ".join(str(value) for value in active[:-1]) + f", and {active[-1]}"
        return f"{quiet} {quiet_noun} closed nothing. Then {tail}."
    total = sum(weekly)
    return f"{total} closed in these {weeks} weeks. The latest week closed {weekly[-1]}."


def _delta_mark(current: int, previous: int | None) -> str:
    if previous is None:
        return ""
    delta = current - previous
    if delta == 0:
        return "same"
    if delta > 0:
        return f"▲ {delta}"
    return f"▼ {abs(delta)}"


def _flow_closed_text(row: dict) -> str:
    if row["closed"] is None:
        return "—"
    text = str(row["closed"])
    if row["closed_points"] is not None:
        text += f" · {_fmt_num(row['closed_points'])} pts"
    return text


def _flow_wip_text(row: dict) -> str:
    mark = _delta_mark(row["wip"], row["wip_previous"])
    if not mark:
        return str(row["wip"])
    return f"{row['wip']} {mark}"


def _flow_p85_text(row: dict) -> str:
    if row["p85_days"] is None:
        return "—"
    return f"{row['p85_days']}d · {row['p85_n']}"


def _metric_actual_text(kpi: dict) -> str:
    if kpi["actual"] is None:
        return "—"
    text = f"{kpi['actual']}d" if kpi["id"] == "cycle" else str(kpi["actual"])
    if kpi.get("actual_note"):
        text += f" · {kpi['actual_note']}"
    return text


def _metric_change_text(kpi: dict) -> str:
    delta = kpi["delta"]
    if delta is None:
        return "—"
    if delta == 0 or kpi["judge"] == "same":
        return "same"
    mark = "▲" if delta > 0 else "▼"
    word = "better" if kpi["judge"] == "better" else "worse"
    return f"{mark} {abs(delta)} {word}"


def _flow_md(brief: dict) -> list[str]:
    metrics = brief.get("metrics") or {}
    scopes = metrics.get("scopes") or []
    flow = brief.get("flow") or {}
    rows = flow.get("rows") or []
    if not scopes:
        return []
    ends = _week_labels(metrics or flow)
    lines = [
        "",
        "## Metrics",
        "",
    ]
    if rows:
        lines.extend([_flow_claim(rows[0]["weekly"]), ""])
    lines.extend(
        [
            "Stories, bugs, and tasks. All is the total. A target is a count of due dates in the file. The gap on Marked Done is items dated that week minus items marked Done that week. The gap on Dates reached is items still open after a date that has arrived. A triangle points the way the number moved. Better means more finished work, or fewer open, blocked, unassigned, or days.",
            "",
        ]
    )
    for scope in scopes:
        lines.extend([f"### {scope['name']}", "", "| KPI | Actual | Target | Gap | Change | Trendline |", "| --- | --- | --- | --- | --- | --- |"])
        for kpi in scope["kpis"]:
            target = "—" if kpi["target"] is None else str(kpi["target"])
            gap = "—" if kpi["gap"] is None else str(kpi["gap"])
            weekly = kpi["weekly"] or []
            trend = " ".join("—" if value is None else str(value) for value in weekly) if weekly else "—"
            lines.append(
                f"| {kpi['name']} | {_metric_actual_text(kpi)} | {target} | {gap} | {_metric_change_text(kpi)} | {trend} |"
            )
        lines.append("")
    if ends:
        lines.extend(["### Weekly counts", "", "Marked Done, then items dated that week.", ""])
        lines.append("| Team | Series | " + " | ".join(ends) + " |")
        lines.append("| --- | --- | " + " | ".join("---" for _ in ends) + " |")
        for scope in scopes:
            for kpi in scope["kpis"]:
                if kpi["id"] != "throughput":
                    continue
                lines.append(
                    "| "
                    + scope["name"]
                    + " | Marked Done | "
                    + " | ".join(str(value) for value in kpi["weekly"])
                    + " |"
                )
                lines.append(
                    "| "
                    + scope["name"]
                    + " | Dated | "
                    + " | ".join(str(value) for value in kpi["target_weekly"])
                    + " |"
                )
        lines.append("")
    return lines


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
        "## Summary",
        *_reading_md(brief),
        *_flow_md(brief),
        "## Progress",
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
    show_progress = any(row.get("progress") for row in brief["pulse"])
    if show_progress:
        lines[-2] = "| Project | State | Progress |"
        lines[-1] = "| --- | --- | --- |"
    for row in brief["pulse"]:
        if show_progress:
            lines.append(f"| {row['name']} | {labels[row['state']]} | {row.get('progress') or ''} |")
        else:
            lines.append(f"| {row['name']} | {labels[row['state']]} |")
    children = [row for row in brief.get("progress") or [] if row["kind"] != "project"]
    if children:
        lines.append("")
        for row in children:
            lines.append(f"- {row['id']} {row['name']}: {row['text']}")
        lines.append("")
    lines.extend(["", "## Risks", "", "## Top risks", ""])
    if not brief["top_risks"]:
        lines.append("No risk cleared the evidence rules.")
        lines.append("")
    for finding in brief["top_risks"]:
        lines.extend(_finding_md(finding))
    lines.extend(["## Field changes", ""])
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
            "- An open Highest or High item with an empty story point or effort cell has no estimate. Three or more in one project and priority are one count.",
            "- Remaining above the original estimate cites both numbers. A Done item whose remaining cell is not zero is cited. Those figures are not turned into days.",
            "- A story point or remaining figure that changed since the previous export is listed with the other changes.",
            "- Progress counts story points Done on items that have a story point or effort. Epic, feature, and initiative points stay out of the project total. Those parents are counted from their direct children. Remaining and the original estimate are summed where both cells are numbers.",
            "- Stale and overdue items are raised without a notes file. A note that says the work is on track is still compared with the export.",
            "- Optimistic notes are an inference. The dates and statuses they are compared with are facts.",
            "- This week is the report date and the six days after it. Next week is the seven days after that. A due date in either window is the date in the file.",
            "- Metrics count stories, bugs, and tasks. The opening sentence is the portfolio's weekly closes. Since last export is what moved to Done. Marked Done is the latest week, and its target is how many items are dated in that week. Dates reached counts items whose due date is on or before the report date, and how many of those are Done. The gap is the ones still open. Cycle p85 is the 85th percentile of days from created to the newest update on Done items, with the count of those items. A triangle points the way the number moved. Better means more finished work, or fewer open, blocked, unassigned, or days. None of these is a forecast.",
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


def _line_svg(actual: list, target: list | None, scale: int) -> str:
    width, height = 128, 36
    count = max(len(actual), 1)
    span = max(count - 1, 1)

    def point(index: int, value: int) -> tuple[float, float]:
        x = 3 + index * (width - 6) / span
        y = height - 3 - (value / scale) * (height - 8) if scale else height - 3
        return x, y

    def path(series: list, dashed: bool) -> str:
        commands = []
        pen = "M"
        for index, value in enumerate(series):
            if value is None:
                pen = "M"
                continue
            x, y = point(index, int(value))
            commands.append(f"{pen}{x:.1f},{y:.1f}")
            pen = "L"
        if not commands:
            return ""
        klass = "target-line" if dashed else "actual-line"
        return f'<path d="{" ".join(commands)}" class="{klass}"></path>'

    dots = []
    for index, value in enumerate(actual):
        if value is None:
            continue
        x, y = point(index, int(value))
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" class="actual-line"></circle>')
    actual_label = ", ".join("—" if value is None else str(value) for value in actual)
    target_label = ""
    if target:
        target_label = ". Dated " + ", ".join(str(value) for value in target)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape("Trend " + actual_label + target_label)}">'
        + path(actual, False)
        + (path(target, True) if target else "")
        + "".join(dots)
        + "</svg>"
    )


def _change_html(kpi: dict) -> str:
    delta = kpi["delta"]
    if delta is None:
        return "—"
    if delta == 0 or kpi["judge"] == "same":
        return '<span class="same">same</span>'
    direction = "up" if delta > 0 else "down"
    word = "better" if kpi["judge"] == "better" else "worse"
    return (
        f'<span class="chg {kpi["judge"]}"><i class="tri {direction}" aria-hidden="true"></i> '
        f"{abs(delta)} {word}</span>"
    )


def _kpi_figure(kpi: dict | None, label: str) -> str:
    if kpi is None or kpi["actual"] is None:
        value, note = "—", ""
    elif kpi["id"] == "cycle":
        value, note = f'{kpi["actual"]}<span class="unit">d</span>', kpi.get("actual_note") or ""
    else:
        value, note = str(kpi["actual"]), kpi.get("actual_note") or ""
    change = ""
    if kpi is not None and kpi["delta"] is not None:
        change = _change_html(kpi)
    note_html = f'<p class="quiet">{html.escape(note)}</p>' if note else ""
    if change:
        note_html += f'<p class="quiet">{change}</p>'
    return (
        f'<div class="cast-cell">'
        f'<p class="kicker">{html.escape(label)}</p>'
        f'<p class="figure">{value}</p>{note_html}</div>'
    )


def _summary_cast(brief: dict) -> str:
    scopes = (brief.get("metrics") or {}).get("scopes") or []
    if not scopes:
        return ""
    by_id = {kpi["id"]: kpi for kpi in scopes[0]["kpis"]}
    return (
        '<div class="cast" role="group" aria-label="Portfolio flow">'
        + _kpi_figure(by_id.get("closed"), "Closed")
        + _kpi_figure(by_id.get("wip"), "WIP")
        + _kpi_figure(by_id.get("dates"), "Dates reached")
        + _kpi_figure(by_id.get("cycle"), "Cycle p85")
        + "</div>"
    )


def _metric_cell(value: object) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def _metrics_html(brief: dict) -> str:
    metrics = brief.get("metrics") or {}
    scopes = metrics.get("scopes") or []
    flow = brief.get("flow") or {}
    rows = flow.get("rows") or []
    if not scopes:
        return ""
    ends = _week_labels(metrics)
    axis = f"{ends[0]}–{ends[-1]}" if ends else ""
    inputs = []
    labels = []
    bodies = []
    for scope in scopes:
        ident = "team-" + re.sub(r"[^A-Za-z0-9_-]", "", scope["id"]) or "team"
        checked = " checked" if scope["id"] == "all" else ""
        inputs.append(f'<input class="team-input" type="radio" name="team" id="{ident}"{checked}>')
        labels.append(f'<label for="{ident}">{html.escape(scope["name"])}</label>')
        kpi_rows = []
        for kpi in scope["kpis"]:
            actual = _metric_actual_text(kpi)
            trend = "—"
            if kpi["weekly"]:
                trend = _line_svg(kpi["weekly"], kpi["target_weekly"], int(kpi["scale"] or 1))
            kpi_rows.append(
                "<tr>"
                f"<td>{html.escape(kpi['name'])}</td>"
                f'<td class="num">{html.escape(actual)}</td>'
                f'<td class="num">{_metric_cell(kpi["target"])}</td>'
                f'<td class="num">{_metric_cell(kpi["gap"])}</td>'
                f'<td class="num">{_change_html(kpi)}</td>'
                f'<td class="trend">{trend}</td>'
                "</tr>"
            )
        bodies.append(f'<tbody class="scope scope-{ident}">{"".join(kpi_rows)}</tbody>')
    evidence = (rows[0].get("evidence") if rows else None) or []
    fold = ""
    if evidence:
        fold = f"<details class=\"fold\"><summary>Evidence</summary>{_evidence_html({'evidence': evidence})}</details>"
    week_head = "".join(f"<th>{html.escape(label)}</th>" for label in ends)
    week_rows = []
    for scope in scopes:
        for kpi in scope["kpis"]:
            if kpi["id"] != "throughput":
                continue
            done = "".join(f"<td>{value}</td>" for value in kpi["weekly"])
            dated = "".join(f"<td>{value}</td>" for value in kpi["target_weekly"])
            week_rows.append(f"<tr><td>{html.escape(scope['name'])}</td><td>Marked Done</td>{done}</tr>")
            week_rows.append(f"<tr><td>{html.escape(scope['name'])}</td><td>Dated</td>{dated}</tr>")
    note = (
        "Stories, bugs, and tasks. All is the total. "
        "A target is filled only from a due date in the file. "
        "Marked Done compares items marked Done that week with items dated that week. They are not the same items. "
        "Dates reached counts items whose due date is on or before the report date, and how many of those are Done. "
        "The gap there is items still open. "
        "The solid line is the actual. The dashed line is items dated that week. Every team uses the portfolio scale for that row. "
        "A triangle points the way the number moved. Better means more finished work, or fewer open, blocked, unassigned, or days. "
        "Cycle p85 describes items already Done. This is not a forecast."
    )
    rules = []
    for scope in scopes:
        ident = "team-" + re.sub(r"[^A-Za-z0-9_-]", "", scope["id"]) or "team"
        rules.append(
            f"#{ident}:checked ~ .flow-wrap .scope-{ident} {{ display: table-row-group; }}"
            f'#{ident}:checked ~ .team-bar label[for="{ident}"] {{ color: var(--fg); border-color: var(--cyan); background: var(--cyan-dim); }}'
        )
    return (
        "<style>" + "".join(rules) + "</style>"
        + '<div class="section-head"><div><p class="kicker">Metrics</p>'
        f'<h2>One team, or the total</h2></div><details class="info">'
        f'<summary aria-label="About Metrics">i</summary><p>{html.escape(note)}</p></details></div>'
        + '<div class="metrics panel">'
        + "".join(inputs)
        + f'<div class="team-bar" role="radiogroup" aria-label="Team">{"".join(labels)}</div>'
        + '<div class="flow-wrap"><table class="flow"><thead><tr>'
        + "<th>KPI</th><th class=\"num\">Actual</th><th class=\"num\">Target</th>"
        + "<th class=\"num\">Gap</th><th class=\"num\">Change</th>"
        + f'<th class="num">Trendline<span class="axis">{html.escape(axis)}</span></th>'
        + "</tr></thead>"
        + "".join(bodies)
        + "</table></div></div>"
        + '<details class="sheet fold"><summary>Weekly counts</summary>'
        + '<div class="flow-wrap"><table class="flow weeks"><thead><tr><th>Team</th><th>Series</th>'
        + week_head
        + "</tr></thead><tbody>"
        + "".join(week_rows)
        + "</tbody></table></div></details>"
        + fold
    )


def _summary_head(brief: dict) -> str:
    flow = brief.get("flow") or {}
    rows = flow.get("rows") or []
    claim = _flow_claim(rows[0]["weekly"]) if rows else "Flow"
    note = (
        "The sentence is how many stories, bugs, and tasks were marked Done in each of the last eight weeks. "
        "The figures under it are the portfolio total."
    )
    return (
        '<div class="section-head flow-head"><div><p class="kicker">Flow</p>'
        f"<h2>{html.escape(claim)}</h2></div><details class=\"info\">"
        f'<summary aria-label="About this shape">i</summary><p>{html.escape(note)}</p></details></div>'
    )


def _reading_html(brief: dict) -> str:
    reading = brief.get("reading") or {}
    if not reading:
        return ""
    this_week = re.sub(
        r"\s+\d{4}$",
        "",
        _span(date.fromisoformat(reading["this_week"][0]), date.fromisoformat(reading["this_week"][1])),
    )
    next_week = re.sub(
        r"\s+\d{4}$",
        "",
        _span(date.fromisoformat(reading["next_week"][0]), date.fromisoformat(reading["next_week"][1])),
    )
    rows = []
    for key, title, _note in _READING_SECTIONS:
        items = reading.get(key) or []
        text = " ".join(row["text"] for row in items) if items else "None."
        evidence = [piece for row in items for piece in row.get("evidence") or []]
        fold = ""
        if evidence:
            fold = "<details class=\"fold\"><summary>Evidence</summary>{}</details>".format(
                _evidence_html({"evidence": evidence})
            )
        tone = "tone-empty" if not items else "tone-live"
        if key in {"priorities", "delay_risks"} and items:
            tone = "tone-warn"
        rows.append(
            f'<article class="brief-line {tone}">'
            f'<p class="brief-label">{html.escape(title)}</p>'
            f'<p class="brief-text">{html.escape(text)}</p>{fold}</article>'
        )
    head = (
        '<div class="section-head"><div><p class="kicker">This week</p>'
        f'<h2>{html.escape(this_week)} <span class="soft">· next {html.escape(next_week)}</span></h2>'
        f'</div><details class="info"><summary aria-label="About this week">i</summary>'
        f'<p>{html.escape(_WEEK_NOTE)}</p></details></div>'
    )
    return head + '<div class="briefing">' + "".join(rows) + "</div>"


def _section(title: str, note: str) -> str:
    return (
        '<div class="section-head"><h2>{}</h2><details class="info">'
        '<summary aria-label="About {}">i</summary><p>{}</p></details></div>'
    ).format(html.escape(title), html.escape(title), html.escape(note))


def _bar_html(row: dict) -> str:
    percent = row.get("percent")
    if percent is None:
        return ""
    width = max(0, min(100, int(percent)))
    label = f"{row['name']}, {width} percent of story points Done"
    return (
        f'<div class="bar" role="progressbar" aria-valuenow="{width}" aria-valuemin="0" '
        f'aria-valuemax="100" aria-label="{html.escape(label)}">'
        f'<span style="--w:{width}%"></span></div>'
    )


def _portfolio_health(pulse: list[dict]) -> tuple[str, str, str]:
    states = [row.get("state", "unchecked") for row in pulse]
    if any(state == "blocked" for state in states):
        return (
            "blocked",
            "Blocked",
            "At least one project has a blocked item. Escalation belongs in the review.",
        )
    if any(state == "needs_attention" for state in states):
        return (
            "needs_attention",
            "Needs attention",
            "Material risks cleared the evidence rules. A decision or owner move is waiting.",
        )
    if any(state == "unchecked" for state in states):
        return (
            "unchecked",
            "Not checked",
            "The export does not support a full on-track call for every project.",
        )
    return (
        "on_track",
        "On track",
        "No blocked project and no high-severity finding cleared the rules.",
    )


def _mix_html(pulse: list[dict]) -> str:
    order = ("blocked", "needs_attention", "on_track", "unchecked")
    labels = {
        "on_track": "On track",
        "needs_attention": "Needs attention",
        "blocked": "Blocked",
        "unchecked": "Not checked",
    }
    counts = {key: 0 for key in order}
    for row in pulse:
        state = row.get("state", "unchecked")
        counts[state] = counts.get(state, 0) + 1
    total = max(sum(counts.values()), 1)
    segments = []
    legend = []
    for key in order:
        count = counts[key]
        if not count:
            continue
        pct = 100 * count / total
        segments.append(
            f'<span class="mix-seg {key}" style="flex:{count}" title="{html.escape(labels[key])}: {count}"></span>'
        )
        legend.append(
            f'<li><i class="dot {key}" aria-hidden="true"></i>{html.escape(labels[key])} '
            f'<strong>{count}</strong></li>'
        )
    return (
        f'<div class="mix" role="img" aria-label="Project status mix">'
        f'<div class="mix-bar">{"".join(segments)}</div>'
        f'<ul class="mix-legend">{"".join(legend)}</ul></div>'
    )


def _ask_html(brief: dict) -> str:
    decisions = brief.get("decisions") or []
    questions = brief.get("questions") or []
    cards = []
    for index, decision in enumerate(decisions, start=1):
        extra = []
        if decision["overdue"]:
            extra.append('<span class="pill overdue">Overdue</span>')
        if decision["unknowns"]:
            extra.append(
                '<span class="pill">Unknown: {}</span>'.format(
                    html.escape(", ".join(decision["unknowns"]))
                )
            )
        if decision["successors"]:
            extra.append(
                '<span class="pill">Before {}</span>'.format(
                    html.escape(", ".join(decision["successors"]))
                )
            )
        cards.append(
            '<article class="ask-card decision">'
            f'<p class="ask-index">Decision {index:02d}</p>'
            f'<h3><span class="key">{html.escape(decision["key"])}</span> {html.escape(decision["summary"])}</h3>'
            f'<p class="ask-meta">Owner <strong>{html.escape(decision["owner"])}</strong> · '
            f'Due <strong>{html.escape(_fmt_iso(decision["due"]))}</strong></p>'
            f'<div class="pill-row">{"".join(extra)}</div></article>'
        )
    for index, question in enumerate(questions, start=1):
        cards.append(
            '<article class="ask-card">'
            f'<p class="ask-index">Ask {index:02d}</p>'
            f'<p class="ask-text">{html.escape(question)}</p></article>'
        )
    if not cards:
        cards.append('<p class="empty">No open decision or review question cleared the rules.</p>')
    return (
        _section(
            "The ask",
            "What the delivery lead, product owner, or scrum master needs from the room. "
            "Questions are drawn from top risks and open decisions. Nothing is sent until a person approves the brief.",
        )
        + f'<div class="ask-grid">{"".join(cards)}</div>'
    )


def _scope_ident(scope_id: str) -> str:
    return "team-" + re.sub(r"[^A-Za-z0-9_-]", "", scope_id) or "team"


def _series_scale(actual: list | None, target: list | None) -> int:
    values = [
        int(value)
        for value in list(actual or []) + list(target or [])
        if isinstance(value, (int, float))
    ]
    return max(values, default=1)


def _chart_svg(
    actual: list | None,
    target: list | None,
    labels: list[str],
    label: str,
    mode: str,
) -> str:
    actual = list(actual or [])
    target = list(target or [])
    count = max(len(labels), len(actual), len(target), 1)
    scale = _series_scale(actual, target)
    if not any(isinstance(value, (int, float)) for value in actual + target):
        return '<p class="empty">No points in this window.</p>'
    width, height = 720, 220
    pad_l, pad_r, pad_t, pad_b = 46, 16, 16, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    slot = plot_w / count
    y_min, y_max = 0, scale
    if mode == "line":
        line_values = [int(value) for value in actual if isinstance(value, (int, float))]
        if line_values:
            lo, hi = min(line_values), max(line_values)
            if lo == hi:
                pad = max(1, round(hi * 0.12)) if hi else 1
                y_min, y_max = max(0, lo - pad), hi + pad
            else:
                pad = max(1, round((hi - lo) * 0.35))
                y_min, y_max = max(0, lo - pad), hi + pad
    y_span = max(y_max - y_min, 1)

    def y_of(value: float) -> float:
        return pad_t + plot_h - ((float(value) - y_min) / y_span) * plot_h

    def center(index: int) -> float:
        return pad_l + index * slot + slot / 2

    ticks = sorted({y_min, y_min + (y_max - y_min) // 2, y_max})
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">'
    ]
    base = pad_t + plot_h
    for tick in ticks:
        y = y_of(tick)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"></line>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>'
        )
    if mode == "bars":
        bar_w = min(26, slot * 0.42)
        for index, value in enumerate(actual):
            if not isinstance(value, (int, float)) or value <= 0:
                continue
            y = y_of(value)
            parts.append(
                '<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="4" class="bar-col"></rect>'.format(
                    x=center(index) - bar_w / 2,
                    y=y,
                    w=bar_w,
                    h=max(base - y, 0),
                )
            )
    else:
        segments: list[list[str]] = []
        current: list[str] = []
        for index, value in enumerate(actual):
            if not isinstance(value, (int, float)):
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append(f"{center(index):.1f},{y_of(value):.1f}")
        if current:
            segments.append(current)
        for segment in segments:
            flat = len({point.split(",")[1] for point in segment}) <= 1
            if len(segment) >= 2 and not flat:
                first_x = segment[0].split(",")[0]
                last_x = segment[-1].split(",")[0]
                area = " ".join(segment) + f" {last_x},{base:.1f} {first_x},{base:.1f}"
                parts.append(f'<polygon points="{area}" class="area"></polygon>')
            parts.append(f'<polyline points="{" ".join(segment)}" class="series"></polyline>')
        for index, value in enumerate(actual):
            if not isinstance(value, (int, float)):
                continue
            last = index == len(actual) - 1 or not any(
                isinstance(item, (int, float)) for item in actual[index + 1 :]
            )
            radius = 4.2 if last else 2.6
            parts.append(
                f'<circle cx="{center(index):.1f}" cy="{y_of(value):.1f}" r="{radius}" class="dot"></circle>'
            )
    if target:
        points = [
            f"{center(index):.1f},{y_of(value):.1f}"
            for index, value in enumerate(target)
            if isinstance(value, (int, float))
        ]
        if len(points) >= 2:
            parts.append(f'<polyline points="{" ".join(points)}" class="target"></polyline>')
        for index, value in enumerate(target):
            if not isinstance(value, (int, float)):
                continue
            parts.append(
                f'<circle cx="{center(index):.1f}" cy="{y_of(value):.1f}" r="2.3" class="target-dot"></circle>'
            )
    for index, text in enumerate(labels):
        if index >= count:
            break
        parts.append(
            f'<text class="axis" x="{center(index):.1f}" y="{height - 8}" text-anchor="middle">{html.escape(text)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _meter_html(actual: int | None, target: int | None, higher_is_better: bool) -> str:
    if actual is None:
        return '<div class="meter meter-empty"></div>'
    ceiling = max(actual, 0 if target is None else target, 1)
    fill = max(0, min(100, 100 * actual / ceiling))
    tone = "open"
    mark = ""
    if target is not None:
        short = actual < target if higher_is_better else actual > target
        tone = "short" if short else "met"
        mark_at = max(0, min(100, 100 * target / ceiling))
        mark = f'<i class="meter-mark" style="left:{mark_at:.1f}%"></i>'
    return (
        f'<div class="meter {tone}" role="img" '
        f'aria-label="{actual} against {target if target is not None else "no target"}">'
        f'<span class="meter-fill" style="width:{fill:.1f}%"></span>{mark}</div>'
    )


def _history_rail(weekly: list | None, current: int | None) -> str:
    nums = [int(value) for value in (weekly or []) if isinstance(value, (int, float))]
    if current is None or not nums:
        return ""
    high = max(max(nums), current, 1)
    pos = max(0, min(100, 100 * current / high))
    return (
        f'<div class="rail" role="img" aria-label="This reading on an eight-week high of {high}">'
        f'<span class="rail-mark" style="left:{pos:.1f}%"></span></div>'
    )


def _level_figure(kpi: dict) -> str:
    if kpi["actual"] is None:
        return "—"
    if kpi["id"] == "cycle":
        return f'{kpi["actual"]}<span class="unit">d</span>'
    if kpi["target"] is not None:
        return f'{kpi["actual"]}<span class="unit">/ {kpi["target"]}</span>'
    return str(kpi["actual"])


def _level_card(kpi: dict, kind: str, caption: str) -> str:
    change = ""
    if kpi["delta"] is not None:
        change = _change_html(kpi)
    gap = ""
    if kpi["gap"] is not None:
        gap = f'<span class="gap">Gap {kpi["gap"]}</span>'
    meter = ""
    if kind == "target":
        higher = kpi.get("better") != "down"
        meter = _meter_html(kpi["actual"], kpi["target"], higher)
    elif kind == "history":
        meter = _history_rail(kpi.get("weekly"), kpi["actual"] if isinstance(kpi["actual"], int) else None)
    note = kpi.get("actual_note") or ""
    note_html = f'<p class="quiet">{html.escape(note)}</p>' if note else ""
    meta = f'<p class="level-meta">{gap}{change}</p>' if gap or change else ""
    return (
        f'<article class="level-card {kind}">'
        f'<p class="kicker">{html.escape(kpi["name"])}</p>'
        f'<p class="figure">{_level_figure(kpi)}</p>'
        f"{note_html}{meter}{meta}"
        f'<p class="quiet">{html.escape(caption)}</p></article>'
    )


def _chart_card(title: str, note: str, legend: str, svg: str, wide: bool) -> str:
    klass = "chart-card wide" if wide else "chart-card"
    return (
        f'<article class="{klass}"><p class="kicker">Over time</p><h3>{html.escape(title)}</h3>'
        f"{legend}<p class=\"quiet\">{html.escape(note)}</p>{svg}</article>"
    )


def _board_html(brief: dict) -> str:
    metrics = brief.get("metrics") or {}
    scopes = metrics.get("scopes") or []
    if not scopes:
        return ""
    labels = _week_labels(metrics)
    inputs = []
    pills = []
    panels = []
    rules = []
    captions = {
        "throughput": "Latest week against items dated that week.",
        "dates": "Items already Done whose due date is on or before the report date, against every such date.",
        "closed": "Moved to Done since the previous export.",
        "wip": "No target in the file. The mark is this week on the eight-week high.",
        "cycle": "No target in the file. Finished items only.",
        "blocked": "No target in the file.",
        "unassigned": "No target in the file.",
    }
    chart_notes = {
        "throughput": "Bars are items marked Done. The dashed line is due dates in that week. They are not the same items.",
        "dates": "Bars are due dates reached that week. The dashed line is items dated that week.",
        "wip": "Open stories, bugs, and tasks at each week end.",
        "cycle": "Cycle p85 of items already Done by that week end. Gaps are weeks with nothing finished.",
    }
    for scope in scopes:
        ident = _scope_ident(scope["id"])
        board_id = "board-" + ident
        checked = " checked" if scope["id"] == "all" else ""
        inputs.append(f'<input class="team-input" type="radio" name="board" id="{board_id}"{checked}>')
        pills.append(f'<label for="{board_id}">{html.escape(scope["name"])}</label>')
        by_id = {kpi["id"]: kpi for kpi in scope["kpis"]}
        levels = []
        for key, kind in (
            ("throughput", "target"),
            ("dates", "target"),
        ):
            kpi = by_id.get(key)
            if kpi:
                levels.append(_level_card(kpi, kind, captions[key]))
        snaps = []
        for key, kind in (
            ("wip", "history"),
            ("cycle", "history"),
            ("closed", "snap"),
            ("blocked", "snap"),
            ("unassigned", "snap"),
        ):
            kpi = by_id.get(key)
            if kpi:
                snaps.append(_level_card(kpi, kind, captions[key]))
        charts = []
        chart_titles = {
            "throughput": "Marked Done",
            "dates": "Dates reached each week",
            "wip": "WIP",
            "cycle": "Cycle p85",
        }
        for key, mode, legend in (
            (
                "throughput",
                "bars",
                '<p class="chart-legend"><i class="swatch actual"></i>Marked Done<i class="swatch target"></i>Dated that week</p>',
            ),
            (
                "dates",
                "bars",
                '<p class="chart-legend"><i class="swatch actual"></i>Reached<i class="swatch target"></i>Dated that week</p>',
            ),
            ("wip", "line", ""),
            ("cycle", "line", ""),
        ):
            kpi = by_id.get(key)
            if not kpi:
                continue
            svg = _chart_svg(kpi.get("weekly"), kpi.get("target_weekly"), labels, chart_titles[key], mode)
            charts.append(_chart_card(chart_titles[key], chart_notes[key], legend, svg, True))
        panels.append(
            f'<div class="board-scope board-scope-{ident}">'
            f'<div class="levels-target">{"".join(levels)}</div>'
            f'<div class="levels-snap">{"".join(snaps)}</div>'
            f'<div class="chart-grid">{"".join(charts)}</div></div>'
        )
        rules.append(
            f"#{board_id}:checked ~ .board-stage .board-scope-{ident} {{ display: block; }}"
            f'#{board_id}:checked ~ .board-pills label[for="{board_id}"] {{ color: #071018; background: var(--cyan); border-color: var(--cyan); }}'
        )
    note = (
        "Stories, bugs, and tasks. A target is filled only from a due date in the file. "
        "Marked Done and Dates reached draw that target. "
        "WIP, cycle p85, blocked, and unassigned have no target in the file. "
        "Each chart uses its own scale. Cycle p85 describes items already Done. This is not a forecast."
    )
    return (
        "<style>" + "".join(rules) + "</style>"
        + '<div class="section-head"><div><p class="kicker">Board</p>'
        + "<h2>Levels, then the path</h2></div><details class=\"info\">"
        + f'<summary aria-label="About the board">i</summary><p>{html.escape(note)}</p></details></div>'
        + '<div class="board">'
        + "".join(inputs)
        + f'<div class="board-pills" role="radiogroup" aria-label="Team">{"".join(pills)}</div>'
        + f'<div class="board-stage">{"".join(panels)}</div></div>'
    )


def render_html(brief: dict) -> str:
    labels = {
        "on_track": "On track",
        "needs_attention": "Needs attention",
        "blocked": "Blocked",
        "unchecked": "Not checked",
    }
    health_key, health_label, health_note = _portfolio_health(brief["pulse"])
    progress_rows = brief.get("progress") or []
    by_project = {row["id"]: row for row in progress_rows if row["kind"] == "project"}
    children_of: dict[str, list[dict]] = {}
    for row in progress_rows:
        if row["kind"] != "project":
            children_of.setdefault(row["project"], []).append(row)
    cards = []
    for row in brief["pulse"]:
        parts = [
            f'<div class="pulse-top"><span class="state {html.escape(row["state"])}">{labels[row["state"]]}</span>'
            f'<span class="pulse-name">{html.escape(row["name"])}</span></div>',
        ]
        record = by_project.get(row["id"])
        if record:
            parts.append(_bar_html(record))
        if row.get("progress"):
            parts.append(f'<span class="progress">{html.escape(row["progress"])}</span>')
        nested = []
        for child in children_of.get(row["id"], []):
            nested.append(
                '<li><span class="name">{} {}</span>{}<span class="progress">{}</span></li>'.format(
                    html.escape(child["id"]),
                    html.escape(child["name"]),
                    _bar_html(child),
                    html.escape(child["text"]),
                )
            )
        if nested:
            parts.append(f'<ul class="children">{"".join(nested)}</ul>')
        cards.append(f'<li class="card pulse-card {html.escape(row["state"])}">' + "".join(parts) + "</li>")
    pulse = "".join(cards)
    risks = []
    for finding in brief["top_risks"]:
        count = len(finding["evidence"])
        severity = int(finding["severity"])
        width = max(12, min(100, severity * 12))
        risks.append(
            '<article class="risk-card">'
            f'<div class="risk-rail" style="--sev:{width}%"></div>'
            '<div class="risk-body">'
            f'<p class="kicker">{html.escape(finding["confidence"])} · severity {severity}</p>'
            f"<h3>{html.escape(finding['title'])}</h3>"
            f"<p>{html.escape(finding['summary'])}</p>"
            f'<details class="fold"><summary>Evidence, {count} fields</summary>{_evidence_html(finding)}</details>'
            "</div></article>"
        )
    if brief["changes"]:
        changes = "".join(
            '<li class="change-row"><span class="key">{}</span><span class="change-body">{}</span></li>'.format(
                html.escape(change["key"]),
                html.escape(
                    ", ".join(
                        f"{field['field']} {field['before']} → {field['after']}"
                        for field in change["fields"]
                    )
                ),
            )
            for change in brief["changes"]
        )
    elif brief.get("has_previous"):
        changes = "<li class=\"change-row\"><span class=\"change-body\">No compared field changed.</span></li>"
    else:
        changes = "<li class=\"change-row\"><span class=\"change-body\">No previous export was attached.</span></li>"
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
    stamp = "Synthetic sample · pending approval" if brief["synthetic"] else "Pending approval"
    profile = _profile_label(str(brief.get("source_profile", "canonical")))
    limit_items = "".join(f"<li>{html.escape(item)}</li>" for item in brief.get("limits") or [])
    limits_html = (
        _section(
            "What this file does not support",
            "Columns or words the rules could not use. Lateness, priority, and story points are left out when those cells are missing.",
        )
        + f'<ul class="plain">{limit_items}</ul>'
        if limit_items
        else ""
    )
    quality_rows = brief.get("data_quality") or []
    if quality_rows:
        shown = "".join(f"<li>{html.escape(issue)}</li>" for issue in quality_rows[:40])
        more = ""
        if len(quality_rows) > 40:
            more = f"<li>{len(quality_rows) - 40} more lines are in the Markdown brief.</li>"
        quality_html = (
            f'<details class="fold panel-soft"><summary>Data quality, {len(quality_rows)} lines</summary>'
            f'<ul class="plain">{shown}{more}</ul></details>'
        )
    else:
        quality_html = ""
    as_of_label = _fmt_iso(brief["as_of"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{html.escape(brief['portfolio'])} · Delivery Signal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Serif:ital,wght@0,500;0,600;1,500&display=swap" rel="stylesheet">
  <style>
    :root {{
      color-scheme: dark;
      --void: #090b10;
      --ink: #0f1219;
      --panel: rgba(22, 26, 36, 0.88);
      --panel-solid: #161a24;
      --line: rgba(231, 233, 240, 0.10);
      --line-strong: rgba(231, 233, 240, 0.16);
      --fg: #e8eaf2;
      --soft: #c4c9d8;
      --muted: #8f96a8;
      --cyan: #2dd4e8;
      --cyan-dim: rgba(45, 212, 232, 0.14);
      --green: #3ecf8e;
      --amber: #e8b84a;
      --red: #f07178;
      --shadow: 0 18px 50px rgba(0, 0, 0, 0.35);
      --font: "IBM Plex Sans", "Segoe UI", sans-serif;
      --display: "IBM Plex Serif", "Iowan Old Style", Georgia, serif;
      --mono: "IBM Plex Mono", ui-monospace, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--fg);
      font: 15px/1.5 var(--font);
      background:
        radial-gradient(1200px 600px at 8% -10%, rgba(45, 212, 232, 0.16), transparent 55%),
        radial-gradient(900px 500px at 100% 0%, rgba(62, 207, 142, 0.08), transparent 45%),
        radial-gradient(700px 400px at 70% 100%, rgba(240, 113, 120, 0.07), transparent 50%),
        linear-gradient(180deg, #0c1018 0%, var(--void) 42%, #0a0c11 100%);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.035;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    }}
    main {{
      position: relative;
      max-width: 58rem;
      margin: 0 auto;
      padding: 1.5rem 1.1rem 3.5rem;
    }}
    .hero {{
      position: relative;
      padding: 1.15rem 1.2rem 1.25rem;
      border: 1px solid var(--line);
      border-radius: 1.15rem;
      background:
        linear-gradient(135deg, rgba(45, 212, 232, 0.08), transparent 42%),
        linear-gradient(180deg, rgba(255,255,255,0.03), transparent 55%),
        var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: var(--health);
    }}
    .hero.blocked {{ --health: var(--red); }}
    .hero.needs_attention {{ --health: var(--amber); }}
    .hero.on_track {{ --health: var(--green); }}
    .hero.unchecked {{ --health: var(--muted); }}
    .brand-row {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 0.55rem 1rem;
      align-items: center;
      margin-bottom: 0.85rem;
    }}
    .brand {{
      margin: 0;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--cyan);
    }}
    .stamp {{
      margin: 0;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      color: var(--muted);
      font-family: var(--mono);
    }}
    .health {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      margin: 0 0 0.35rem;
      padding: 0.22rem 0.55rem 0.22rem 0.35rem;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, var(--health) 45%, transparent);
      background: color-mix(in srgb, var(--health) 14%, transparent);
      color: var(--health);
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .health .pulse-dot {{
      width: 0.45rem;
      height: 0.45rem;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 0 0 color-mix(in srgb, var(--health) 55%, transparent);
      animation: beat 2.4s ease-out infinite;
    }}
    h1 {{
      margin: 0.1rem 0 0.45rem;
      font-family: var(--display);
      font-size: clamp(1.85rem, 4vw, 2.55rem);
      font-weight: 600;
      letter-spacing: -0.03em;
      line-height: 1.08;
    }}
    .lede {{
      margin: 0.35rem 0 0;
      max-width: 42rem;
      color: var(--soft);
      font-size: 1.02rem;
    }}
    .health-note {{
      margin: 0.35rem 0 0;
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .meta {{
      margin: 0.7rem 0 0;
      color: var(--muted);
      font-size: 0.82rem;
      font-family: var(--mono);
    }}
    .mix {{ margin-top: 1rem; }}
    .mix-bar {{
      display: flex;
      height: 0.42rem;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255,255,255,0.06);
      gap: 2px;
    }}
    .mix-seg {{ display: block; min-width: 0.35rem; }}
    .mix-seg.on_track {{ background: var(--green); }}
    .mix-seg.needs_attention {{ background: var(--amber); }}
    .mix-seg.blocked {{ background: var(--red); }}
    .mix-seg.unchecked {{ background: var(--muted); }}
    .mix-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem 1rem;
      list-style: none;
      padding: 0.55rem 0 0;
      margin: 0;
      color: var(--muted);
      font-size: 0.78rem;
    }}
    .mix-legend strong {{ color: var(--fg); font-weight: 600; }}
    .dot {{
      display: inline-block;
      width: 0.45rem;
      height: 0.45rem;
      border-radius: 50%;
      margin-right: 0.35rem;
      vertical-align: 0.05rem;
    }}
    .dot.on_track, .state.on_track {{ color: var(--green); }}
    .dot.needs_attention, .state.needs_attention {{ color: var(--amber); }}
    .dot.blocked, .state.blocked {{ color: var(--red); }}
    .dot.unchecked, .state.unchecked {{ color: var(--muted); }}
    .dot.on_track {{ background: var(--green); }}
    .dot.needs_attention {{ background: var(--amber); }}
    .dot.blocked {{ background: var(--red); }}
    .dot.unchecked {{ background: var(--muted); }}
    .sheet-input, .team-input {{ position: absolute; opacity: 0; pointer-events: none; }}
    .pages {{
      display: flex;
      gap: 0.25rem;
      margin: 1.1rem 0 0.35rem;
      padding: 0.28rem;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(8, 10, 14, 0.55);
      backdrop-filter: blur(8px);
      overflow-x: auto;
    }}
    .pages label {{
      cursor: pointer;
      flex: 1 0 auto;
      text-align: center;
      padding: 0.48rem 0.85rem;
      border-radius: 999px;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      transition: color 160ms ease, background 160ms ease;
    }}
    .page {{ display: none; animation: rise 320ms ease both; }}
    #page-summary:checked ~ #summary,
    #page-board:checked ~ #board,
    #page-progress:checked ~ #progress,
    #page-metrics:checked ~ #metrics,
    #page-risks:checked ~ #risks {{ display: block; }}
    #page-summary:checked ~ .pages label[for="page-summary"],
    #page-board:checked ~ .pages label[for="page-board"],
    #page-progress:checked ~ .pages label[for="page-progress"],
    #page-metrics:checked ~ .pages label[for="page-metrics"],
    #page-risks:checked ~ .pages label[for="page-risks"] {{
      color: #071018;
      background: var(--cyan);
    }}
    .section-head {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 0.75rem;
      margin: 1.55rem 0 0.7rem;
    }}
    .section-head h2 {{
      margin: 0.12rem 0 0;
      font-size: 1.18rem;
      font-weight: 600;
      letter-spacing: -0.02em;
      line-height: 1.25;
    }}
    .flow-head h2 {{
      font-family: var(--display);
      font-size: clamp(1.25rem, 2.8vw, 1.55rem);
      font-weight: 500;
      max-width: 40rem;
    }}
    .soft {{ color: var(--muted); font-weight: 500; }}
    .kicker {{
      margin: 0;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .info {{ position: relative; margin: 0; flex: 0 0 auto; }}
    .info summary {{
      list-style: none;
      width: 1.35rem;
      height: 1.35rem;
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      display: grid;
      place-items: center;
      font: italic 700 0.72rem/1 var(--display);
      color: var(--muted);
      cursor: pointer;
      background: rgba(255,255,255,0.02);
    }}
    .info summary::-webkit-details-marker {{ display: none; }}
    .info p {{
      position: absolute;
      right: 0;
      z-index: 3;
      width: min(22rem, 74vw);
      margin: 0.4rem 0 0;
      padding: 0.7rem 0.8rem;
      background: var(--panel-solid);
      color: var(--soft);
      border: 1px solid var(--line);
      border-radius: 0.7rem;
      box-shadow: var(--shadow);
      font-size: 0.82rem;
      font-weight: 400;
    }}
    .briefing {{
      display: grid;
      gap: 0.55rem;
    }}
    .brief-line {{
      display: grid;
      grid-template-columns: 6.5rem 1fr;
      gap: 0.55rem 1rem;
      padding: 0.85rem 0.95rem;
      border: 1px solid var(--line);
      border-radius: 0.85rem;
      background: rgba(255,255,255,0.025);
    }}
    .brief-line.tone-warn {{
      border-color: color-mix(in srgb, var(--amber) 35%, var(--line));
      background: color-mix(in srgb, var(--amber) 7%, transparent);
    }}
    .brief-line.tone-empty .brief-text {{ color: var(--muted); }}
    .brief-label {{
      margin: 0;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}
    .brief-text {{ margin: 0; }}
    .cast {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0.65rem;
      margin: 0.2rem 0 0.4rem;
    }}
    .cast-cell {{
      padding: 0.85rem 0.9rem;
      border: 1px solid var(--line);
      border-radius: 0.9rem;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.03), transparent 55%),
        rgba(12, 15, 22, 0.55);
      min-height: 6.2rem;
    }}
    .cast .figure {{
      margin: 0.28rem 0 0;
      font-size: clamp(1.7rem, 3.5vw, 2.15rem);
      line-height: 1;
      font-weight: 650;
      font-variant-numeric: tabular-nums;
      letter-spacing: -0.04em;
      font-family: var(--display);
    }}
    .cast .unit {{ font-size: 0.95rem; font-weight: 500; color: var(--muted); font-family: var(--font); }}
    .cast .quiet {{ margin: 0.28rem 0 0; color: var(--muted); font-size: 0.8rem; }}
    .ask-grid {{
      display: grid;
      gap: 0.65rem;
    }}
    .ask-card {{
      position: relative;
      padding: 0.95rem 1rem 1rem 1.05rem;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      background: rgba(255,255,255,0.03);
      overflow: hidden;
    }}
    .ask-card::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 3px;
      background: var(--cyan);
    }}
    .ask-card.decision::before {{ background: var(--amber); }}
    .ask-index {{
      margin: 0 0 0.35rem;
      font-family: var(--mono);
      font-size: 0.7rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .ask-card h3 {{
      margin: 0;
      font-size: 1.02rem;
      font-weight: 600;
      letter-spacing: -0.01em;
    }}
    .ask-card .key, .change-row .key {{
      font-family: var(--mono);
      color: var(--cyan);
      font-weight: 500;
    }}
    .ask-meta {{ margin: 0.4rem 0 0; color: var(--soft); font-size: 0.9rem; }}
    .ask-text {{ margin: 0; font-size: 1rem; }}
    .pill-row {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.55rem; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 0.12rem 0.5rem;
      border-radius: 999px;
      border: 1px solid var(--line-strong);
      color: var(--soft);
      font-size: 0.72rem;
      font-weight: 600;
    }}
    .pill.overdue {{
      border-color: color-mix(in srgb, var(--red) 45%, transparent);
      color: var(--red);
      background: color-mix(in srgb, var(--red) 10%, transparent);
    }}
    .pulse {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(16.5rem, 1fr));
      gap: 0.65rem;
    }}
    .pulse-card {{
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      min-height: 4.5rem;
      padding: 0.9rem 0.95rem;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      background: rgba(255,255,255,0.03);
      border-top: 2px solid color-mix(in srgb, currentColor 55%, transparent);
    }}
    .pulse-card.on_track {{ color: var(--green); }}
    .pulse-card.needs_attention {{ color: var(--amber); }}
    .pulse-card.blocked {{ color: var(--red); }}
    .pulse-card.unchecked {{ color: var(--muted); }}
    .pulse-top {{ display: flex; flex-direction: column; gap: 0.2rem; color: var(--fg); }}
    .state {{
      font-weight: 700;
      font-size: 0.68rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .pulse-name {{ font-weight: 600; letter-spacing: -0.01em; }}
    .progress {{ color: var(--soft); font-size: 0.85rem; }}
    .bar {{
      height: 0.42rem;
      background: rgba(255,255,255,0.08);
      border-radius: 999px;
      overflow: hidden;
      margin: 0.4rem 0 0.15rem;
    }}
    .bar > span {{
      display: block;
      height: 100%;
      width: var(--w);
      background: linear-gradient(90deg, #1fb8cc, var(--cyan));
      border-radius: inherit;
      animation: fill 700ms ease forwards;
    }}
    .children {{
      list-style: none;
      padding: 0.45rem 0 0;
      margin: 0.35rem 0 0;
      border-top: 1px solid var(--line);
      color: var(--fg);
    }}
    .children li {{ margin-top: 0.5rem; }}
    .children .name {{ display: block; font-size: 0.82rem; color: var(--soft); }}
    .children .bar {{ height: 0.28rem; }}
    .changelog {{
      list-style: none;
      margin: 0;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      overflow: hidden;
      background: rgba(255,255,255,0.02);
    }}
    .change-row {{
      display: grid;
      grid-template-columns: 6.5rem 1fr;
      gap: 0.75rem;
      padding: 0.75rem 0.9rem;
      border-bottom: 1px solid var(--line);
    }}
    .change-row:last-child {{ border-bottom: 0; }}
    .change-body {{ color: var(--soft); }}
    .panel {{
      padding: 0.85rem;
      border: 1px solid var(--line);
      border-radius: 1rem;
      background: rgba(255,255,255,0.02);
    }}
    .panel-soft {{
      margin-top: 0.8rem;
      padding: 0.7rem 0.85rem;
      border: 1px solid var(--line);
      border-radius: 0.85rem;
      background: rgba(255,255,255,0.02);
    }}
    .flow-wrap {{ overflow-x: auto; }}
    table.flow {{ width: 100%; border-collapse: collapse; }}
    table.flow th {{
      text-align: left;
      color: var(--muted);
      font-size: 0.68rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
      padding: 0.4rem 0.5rem;
      border-bottom: 1px solid var(--line-strong);
    }}
    table.flow td {{
      padding: 0.48rem 0.5rem;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
    }}
    table.flow .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    table.flow .trend {{ width: 7.2rem; }}
    table.flow .axis {{
      display: block;
      margin-top: 0.15rem;
      font-weight: 500;
      letter-spacing: 0;
      text-transform: none;
      color: var(--muted);
    }}
    table.weeks td, table.weeks th {{ text-align: right; }}
    table.weeks td:first-child, table.weeks th:first-child,
    table.weeks td:nth-child(2), table.weeks th:nth-child(2) {{ text-align: left; }}
    .spark {{ width: 8rem; height: 2.25rem; display: block; margin-left: auto; }}
    .spark path.actual-line {{ stroke: var(--cyan); fill: none; stroke-width: 1.7; }}
    .spark path.target-line {{ stroke: var(--muted); fill: none; stroke-width: 1.4; stroke-dasharray: 3 2; }}
    .spark circle.actual-line {{ fill: var(--cyan); stroke: none; }}
    .team-bar {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.15rem 0 0.75rem; }}
    .team-bar label {{
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      padding: 0.18rem 0.7rem;
      font-size: 0.78rem;
      cursor: pointer;
      color: var(--muted);
      transition: border-color 140ms ease, color 140ms ease, background 140ms ease;
    }}
    .metrics .scope {{ display: none; }}
    .chg {{ white-space: nowrap; font-size: 0.82rem; }}
    .chg.better {{ color: var(--green); }}
    .chg.worse {{ color: var(--red); }}
    .tri {{
      display: inline-block;
      width: 0;
      height: 0;
      border-left: 0.28rem solid transparent;
      border-right: 0.28rem solid transparent;
      margin-right: 0.15rem;
      vertical-align: 0.05rem;
    }}
    .tri.up {{ border-bottom: 0.42rem solid currentColor; }}
    .tri.down {{ border-top: 0.42rem solid currentColor; }}
    .same, .quiet, .empty {{ color: var(--muted); }}
    .sheet {{ margin: 0.45rem 0 0.7rem; }}
    .risk-card {{
      display: grid;
      grid-template-columns: 0.35rem 1fr;
      gap: 0;
      margin: 0.65rem 0;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      overflow: hidden;
      background: rgba(255,255,255,0.03);
    }}
    .risk-rail {{
      background: linear-gradient(180deg, var(--red), color-mix(in srgb, var(--amber) 70%, var(--red)));
      opacity: 0.9;
      clip-path: inset(calc(100% - var(--sev, 40%)) 0 0 0);
      min-height: 100%;
    }}
    .risk-body {{ padding: 0.9rem 1rem; }}
    .risk-body h3 {{ margin: 0.2rem 0 0.4rem; font-size: 1.02rem; }}
    .risk-body p {{ margin: 0; color: var(--soft); }}
    .fold {{ margin-top: 0.45rem; }}
    .fold > summary {{
      cursor: pointer;
      color: var(--soft);
      font-size: 0.85rem;
      list-style: none;
    }}
    .fold > summary::-webkit-details-marker {{ display: none; }}
    .fold > summary::before {{
      content: "";
      display: inline-block;
      width: 0.35rem;
      height: 0.35rem;
      margin-right: 0.4rem;
      border-right: 1.5px solid currentColor;
      border-bottom: 1.5px solid currentColor;
      transform: rotate(-45deg);
      vertical-align: 0.15rem;
      transition: transform 140ms ease;
    }}
    .fold[open] > summary::before {{ transform: rotate(45deg); }}
    .evidence {{
      margin: 0.45rem 0 0;
      padding-left: 1rem;
      font: 0.78rem/1.45 var(--mono);
      color: var(--soft);
    }}
    ul.plain {{ padding-left: 1.1rem; color: var(--soft); }}
    footer {{
      margin-top: 2rem;
      padding-top: 1rem;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 0.88rem;
    }}
    footer p {{ margin: 0.35rem 0; max-width: 42rem; }}
    .board .board-scope {{ display: none; }}
    .board-pills {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0 0 0.85rem; }}
    .board-pills label {{
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      padding: 0.22rem 0.75rem;
      font-size: 0.78rem;
      cursor: pointer;
      color: var(--muted);
    }}
    .levels-target {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.65rem; }}
    .levels-snap {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 0.55rem;
      margin-top: 0.65rem;
    }}
    .level-card {{
      padding: 0.9rem 0.95rem 0.85rem;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.035), transparent 58%),
        rgba(12, 15, 22, 0.5);
    }}
    .level-card .figure {{
      margin: 0.2rem 0 0.35rem;
      font-family: var(--display);
      font-size: 2.05rem;
      line-height: 1;
      letter-spacing: -0.04em;
      font-variant-numeric: tabular-nums;
    }}
    .levels-snap .figure {{ font-size: 1.55rem; }}
    .level-card .unit {{ font-size: 0.95rem; color: var(--muted); font-family: var(--font); }}
    .level-meta {{ display: flex; flex-wrap: wrap; gap: 0.45rem 0.7rem; align-items: center; margin: 0.4rem 0 0.15rem; }}
    .gap {{ color: var(--soft); font-size: 0.8rem; font-variant-numeric: tabular-nums; }}
    .meter {{
      position: relative;
      height: 0.5rem;
      margin-top: 0.15rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
    }}
    .meter-fill {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #1fb8cc, var(--cyan));
    }}
    .meter.met .meter-fill {{ background: linear-gradient(90deg, #1f9d6a, var(--green)); }}
    .meter.short .meter-fill {{ background: linear-gradient(90deg, #b8882a, var(--amber)); }}
    .meter-mark {{
      position: absolute;
      top: -0.22rem;
      width: 2px;
      height: 0.94rem;
      background: var(--fg);
      border-radius: 1px;
    }}
    .rail {{
      position: relative;
      height: 0.28rem;
      margin-top: 0.35rem;
      border-radius: 999px;
      background: linear-gradient(90deg, rgba(45,212,232,0.15), rgba(45,212,232,0.45));
    }}
    .rail-mark {{
      position: absolute;
      top: 50%;
      width: 0.62rem;
      height: 0.62rem;
      border-radius: 50%;
      background: var(--cyan);
      transform: translate(-50%, -50%);
      box-shadow: 0 0 0 4px rgba(45, 212, 232, 0.16);
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.65rem;
      margin-top: 0.75rem;
    }}
    .chart-card {{
      padding: 0.85rem 0.85rem 0.55rem;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      background: rgba(8, 10, 16, 0.45);
    }}
    .chart-card.wide {{ grid-column: 1 / -1; }}
    .chart-card h3 {{ margin: 0.12rem 0 0.2rem; font-size: 1.05rem; font-weight: 600; letter-spacing: -0.02em; }}
    .chart-legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem 0.9rem;
      margin: 0.1rem 0 0.15rem;
      color: var(--muted);
      font-size: 0.75rem;
    }}
    .swatch {{
      display: inline-block;
      width: 0.85rem;
      height: 0.4rem;
      margin-right: 0.35rem;
      border-radius: 2px;
      vertical-align: -0.05rem;
    }}
    .swatch.actual {{ background: var(--cyan); }}
    .swatch.target {{
      height: 0;
      border-top: 2px dashed var(--amber);
      border-radius: 0;
      vertical-align: 0.15rem;
    }}
    .chart {{ width: 100%; height: auto; display: block; }}
    .chart .grid {{ stroke: rgba(231, 233, 240, 0.08); stroke-width: 1; }}
    .chart text {{ fill: #8f96a8; font-size: 11px; font-family: "IBM Plex Sans", sans-serif; }}
    .chart .bar-col {{ fill: #2dd4e8; }}
    .chart .series {{ fill: none; stroke: #2dd4e8; stroke-width: 2.4; stroke-linejoin: round; stroke-linecap: round; }}
    .chart .area {{ fill: rgba(45, 212, 232, 0.16); stroke: none; }}
    .chart .dot {{ fill: #2dd4e8; stroke: #090b10; stroke-width: 1.5; }}
    .chart .target {{ fill: none; stroke: #e8b84a; stroke-width: 1.7; stroke-dasharray: 5 4; stroke-linejoin: round; }}
    .chart .target-dot {{ fill: #e8b84a; stroke: none; }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(8px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes fill {{
      from {{ width: 0; }}
      to {{ width: var(--w); }}
    }}
    @keyframes beat {{
      0% {{ box-shadow: 0 0 0 0 color-mix(in srgb, var(--health) 45%, transparent); }}
      70% {{ box-shadow: 0 0 0 0.55rem transparent; }}
      100% {{ box-shadow: 0 0 0 0 transparent; }}
    }}
    @media (max-width: 720px) {{
      .brief-line, .change-row {{ grid-template-columns: 1fr; gap: 0.2rem; }}
      .cast {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .cast .figure {{ font-size: 1.55rem; }}
      .pulse {{ grid-template-columns: 1fr; }}
      .pages {{ border-radius: 0.9rem; }}
      .levels-target, .levels-snap, .chart-grid {{ grid-template-columns: 1fr; }}
      .chart-card.wide {{ grid-column: auto; }}
      .levels-snap {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .page, .bar > span, .health .pulse-dot {{ animation: none !important; }}
      .bar > span {{ width: var(--w); }}
    }}
    @media print {{
      body {{ background: white; color: #111; }}
      body::before, .pages, .info {{ display: none !important; }}
      .page {{ display: block !important; break-inside: avoid; }}
      .hero, .panel, .brief-line, .ask-card, .pulse-card, .risk-card, .cast-cell, .level-card, .chart-card {{
        box-shadow: none;
        background: white;
        border-color: #ddd;
        color: #111;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="hero {html.escape(health_key)}">
      <div class="brand-row">
        <p class="brand">Delivery Signal</p>
        <p class="stamp">{html.escape(stamp)}</p>
      </div>
      <p class="health"><span class="pulse-dot" aria-hidden="true"></span>{html.escape(health_label)}</p>
      <h1>{html.escape(brief['portfolio'])}</h1>
      <p class="health-note">{html.escape(health_note)}</p>
      <p class="lede">{html.escape(brief['lead'])}</p>
      {_mix_html(brief['pulse'])}
      <p class="meta">{html.escape(as_of_label)} · {html.escape(brief['audience'] or 'Delivery lead')} · {html.escape(profile)} · {brief.get('item_count', 0)} work items · rules {html.escape(str(brief.get('rules_version', '')))} · result {html.escape(str(brief.get('result_id', '')))}</p>
    </header>
    <input class="sheet-input" type="radio" name="page" id="page-summary" checked>
    <input class="sheet-input" type="radio" name="page" id="page-board">
    <input class="sheet-input" type="radio" name="page" id="page-progress">
    <input class="sheet-input" type="radio" name="page" id="page-metrics">
    <input class="sheet-input" type="radio" name="page" id="page-risks">
    <nav class="pages" aria-label="Report pages">
      <label for="page-summary">Summary</label>
      <label for="page-board">Board</label>
      <label for="page-progress">Progress</label>
      <label for="page-metrics">Metrics</label>
      <label for="page-risks">Risks</label>
    </nav>
    <section class="page" id="summary">
    {_reading_html(brief)}
    {_summary_head(brief)}
    {_summary_cast(brief)}
    {_ask_html(brief)}
    {limits_html}
    {quality_html}
    </section>
    <section class="page" id="progress">
    {_section("Delivery pulse", "Each card is one project: On track, Needs attention, Blocked, or Not checked. The bar is story points marked Done, among items that have a story point or effort. An epic, feature, or initiative is listed on that card and counted from its direct children. Remaining and the original estimate are sums of rows where both cells are numbers.")}
    <ul class="pulse">{pulse}</ul>
    {_section("Field changes", "Every compared cell: status, assignee, priority, due date, blocker, percent complete, story points, and remaining. The week reading is the conclusion. This list is the audit.")}
    <ul class="changelog">{changes}</ul>
    </section>
    <section class="page" id="board">
    {_board_html(brief)}
    </section>
    <section class="page" id="metrics">
    {_metrics_html(brief)}
    </section>
    <section class="page" id="risks">
    {_section("Top risks", "The highest-severity findings that cite a row in the export. Evidence stays folded under each one.")}
    {''.join(risks) or '<p class="empty">No risk cleared the evidence rules.</p>'}
    {_section("Checked and set aside", "A signal the rules looked at and did not promote. An overdue predecessor is set aside when its successor is already Done.")}
    <details class="fold panel-soft">
      <summary>{html.escape(aside_label)}</summary>
      <ul class="plain">{aside_items}</ul>
    </details>
    {_section("Also noted", "Findings that cleared the severity threshold and did not fit at the top of the brief.")}
    <ul class="plain">{noted}</ul>
    </section>
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
