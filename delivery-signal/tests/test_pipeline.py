#!/usr/bin/env python3
"""The sample is only useful if the planted traps are found and the noise is not."""

import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extract  # noqa: E402
import pipeline  # noqa: E402


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "northline"
COLUMNS = [
    "key",
    "project",
    "project_name",
    "type",
    "summary",
    "status",
    "priority",
    "assignee",
    "story_points",
    "original_estimate",
    "remaining",
    "percent_complete",
    "created",
    "updated",
    "due",
    "parent",
    "blocked_by",
    "comment",
]


def write_items(directory: Path, rows: list[dict], name: str = "work_items.csv") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            full = {column: "" for column in COLUMNS}
            full.update(row)
            writer.writerow(full)


class NorthlineSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.brief = pipeline.build(SAMPLE)

    def test_planted_traps_are_the_top_risks(self) -> None:
        self.assertEqual(self.brief["result_id"], "f1fb4eaa03")
        ids = [finding["id"] for finding in self.brief["top_risks"]]
        self.assertEqual(
            ids,
            [
                "unassigned:FOR-12",
                "unassigned:LUM-4",
                "note_conflict:N-1",
                "blocked:FOR-20",
                "completion:LUM-1",
            ],
        )

    def test_overdue_dependency_that_does_not_block_is_set_aside(self) -> None:
        aside = {finding["id"] for finding in self.brief["set_aside"]}
        self.assertEqual(aside, {"non_blocking:DEP-LEG"})
        self.assertNotIn("non_blocking:DEP-LEG", {finding["id"] for finding in self.brief["top_risks"]})
        text = self.brief["set_aside"][0]["summary"]
        self.assertIn("FOR-3", text)
        self.assertIn("Done", text)

    def test_misleading_completion_uses_story_points(self) -> None:
        finding = next(item for item in self.brief["top_risks"] if item["id"] == "completion:LUM-1")
        self.assertIn("85", finding["title"])
        self.assertIn("8 of 37", finding["title"])
        self.assertIn("N-2", finding["summary"])
        self.assertEqual(finding["confidence"], "fact")

    def test_green_note_is_an_inference_and_the_healthy_note_is_not(self) -> None:
        cedar = next(item for item in self.brief["top_risks"] if item["id"] == "note_conflict:N-1")
        self.assertEqual(cedar["confidence"], "inference")
        self.assertIn("CED-2", cedar["summary"])
        self.assertIn("CED-3", cedar["summary"])
        blob = json.dumps(self.brief["top_risks"] + self.brief["also_noted"])
        self.assertNotIn("N-4", blob)
        aster = next(row for row in self.brief["pulse"] if row["id"] == "ASTER")
        self.assertEqual(aster["state"], "on_track")

    def test_decision_is_queued_with_the_missing_owner(self) -> None:
        self.assertNotIn("LUM-9", {finding["id"] for finding in self.brief["top_risks"]})
        decision = next(item for item in self.brief["decisions"] if item["key"] == "LUM-9")
        self.assertEqual(decision["owner"], "missing")
        self.assertTrue(decision["overdue"])
        self.assertIn("owner", decision["unknowns"])
        self.assertEqual(decision["successors"], ["LUM-4"])

    def test_changes_are_limited_to_the_previous_export(self) -> None:
        by_key = {change["key"]: change for change in self.brief["changes"]}
        self.assertEqual(set(by_key), {"AST-4", "FOR-12", "LUM-3", "LUM-6"})
        assignee = next(field for field in by_key["FOR-12"]["fields"] if field["field"] == "assignee")
        self.assertEqual(assignee["before"], "Alex Rahman")
        self.assertEqual(assignee["after"], "blank")
        status = next(field for field in by_key["LUM-3"]["fields"] if field["field"] == "status")
        self.assertEqual(status["before"], "In Progress")
        self.assertEqual(status["after"], "Done")
        points = next(field for field in by_key["AST-4"]["fields"] if field["field"] == "story_points")
        self.assertEqual((points["before"], points["after"]), ("3", "5"))
        remaining = next(field for field in by_key["LUM-6"]["fields"] if field["field"] == "remaining")
        self.assertEqual((remaining["before"], remaining["after"]), ("2", "5"))
        also = {finding["id"] for finding in self.brief["also_noted"]}
        self.assertIn("missing-estimate-count:LUMEN:Highest", also)
        self.assertIn("remaining-above:FOR-21", also)
        self.assertIn("done-remaining:LUM-2", also)
        self.assertNotIn("missing-estimate-count:LUMEN:Highest", {finding["id"] for finding in self.brief["top_risks"]})

    def test_every_claim_cites_a_real_row(self) -> None:
        loaded = pipeline.load_portfolio(SAMPLE)
        known = {
            "work_items": set(loaded["by_key"]),
            "notes": {note["id"] for note in loaded["notes"]},
            "milestones": set(loaded["milestone_by_id"]),
            "dependencies": {dep["id"] for dep in loaded["dependencies"]},
        }
        findings = self.brief["top_risks"] + self.brief["also_noted"] + self.brief["set_aside"]
        self.assertTrue(findings)
        for finding in findings:
            self.assertTrue(finding["evidence"])
            for evidence in finding["evidence"]:
                self.assertIn(evidence["id"], known[evidence["source"]])
                self.assertTrue(evidence["field"])
        self.assertTrue(self.brief["progress"])
        for row in self.brief["progress"]:
            self.assertTrue(row["evidence"])
            for evidence in row["evidence"]:
                self.assertIn(evidence["id"], known[evidence["source"]])
                self.assertTrue(evidence["field"])
        self.assertEqual(self.brief["data_quality"], [])
        self.assertTrue(self.brief["quality"]["traceable"])
        self.assertEqual(self.brief["approval"], "pending")

    def test_brief_does_not_forecast_a_missed_date(self) -> None:
        text = pipeline.render_markdown(self.brief).lower()
        for phrase in ("will miss", "predicted", "guarantee", "on schedule to fail"):
            self.assertNotIn(phrase, text)
        self.assertIn("does not forecast", text)

    def test_progress_covers_the_project_the_epic_and_the_feature(self) -> None:
        by_id = {row["id"]: row["text"] for row in self.brief["progress"]}
        self.assertEqual(
            by_id["LUMEN"],
            "8 of 38 story points are Done (21%). 3 items have no story point or effort. Remaining 7, original estimate 16.",
        )
        self.assertEqual(
            by_id["LUM-1"],
            "8 of 37 story points are Done (22%). 3 items have no story point or effort. Remaining 7, original estimate 16.",
        )
        self.assertEqual(by_id["FORGE"], "12 of 26 story points are Done (46%). Remaining 8, original estimate 18.")
        self.assertEqual(by_id["FOR-1"], "10 of 26 story points are Done (38%). Remaining 8, original estimate 18.")
        self.assertEqual(by_id["FOR-30"], "2 of 5 story points are Done (40%). Remaining 3, original estimate 5.")
        lumen = next(row for row in self.brief["pulse"] if row["id"] == "LUMEN")
        self.assertEqual(lumen["progress"], by_id["LUMEN"])
        page = pipeline.render_html(self.brief)
        self.assertNotIn("<h2>Progress</h2>", page)
        self.assertIn('role="progressbar"', page)
        self.assertIn('aria-valuenow="21"', page)
        self.assertIn('aria-label="About Delivery pulse"', page)
        self.assertIn("Pick tickets", page)
        self.assertIn("8 of 38 story points are Done", page)
        self.assertIn("background: #12141c", page)
        self.assertIn("color: #e7e9f0", page)
        self.assertIn("color: #22d3ee", page)
        self.assertIn("color: #34d399", page)
        self.assertIn("color: #fcd34d", page)
        self.assertIn("color: #f87171", page)
        self.assertNotIn("#f4f1ea", page)
        self.assertNotIn("#fffdf8", page)
        markdown = pipeline.render_markdown(self.brief)
        self.assertNotIn("## Progress", markdown)
        self.assertIn("FOR-30 Pick tickets", markdown)
        above = next(item for item in self.brief["also_noted"] if item["id"] == "remaining-above:FOR-21")
        blob = f"{above['title']} {above['summary']}".lower()
        self.assertNotIn("day", blob)
        self.assertNotIn("hour", blob)

    def test_forge_is_blocked_and_lumen_needs_attention(self) -> None:
        states = {row["id"]: row["state"] for row in self.brief["pulse"]}
        self.assertEqual(states["FORGE"], "blocked")
        self.assertEqual(states["LUMEN"], "needs_attention")
        self.assertEqual(states["CEDAR"], "needs_attention")


class RuleTests(unittest.TestCase):
    def test_clean_portfolio_has_no_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps(
                    {
                        "portfolio": "Clean",
                        "as_of": "2026-10-10",
                        "audience": "Delivery director",
                        "synthetic": True,
                    }
                ),
                encoding="utf-8",
            )
            write_items(
                directory,
                [
                    {
                        "key": "CLN-1",
                        "project": "CLEAN",
                        "project_name": "Clean export",
                        "type": "Story",
                        "summary": "Scheduler",
                        "status": "In Progress",
                        "priority": "Medium",
                        "assignee": "Mina Cho",
                        "story_points": "3",
                        "updated": "2026-10-09",
                        "due": "2026-10-24",
                    }
                ],
            )
            (directory / "notes.csv").write_text(
                "id,date,project,author,text\nN-1,2026-10-09,CLEAN,Mina Cho,The scheduler is on track. No blockers.\n",
                encoding="utf-8",
            )
            brief = pipeline.build(directory)
        self.assertEqual(brief["top_risks"], [])
        self.assertEqual(brief["pulse"][0]["state"], "on_track")

    def test_missing_column_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Broken", "as_of": "2026-10-10"}),
                encoding="utf-8",
            )
            (directory / "work_items.csv").write_text("key,summary\nA-1,Hello\n", encoding="utf-8")
            with self.assertRaises(pipeline.InputError):
                pipeline.build(directory)

    def test_duplicate_key_is_data_quality_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Dupes", "as_of": "2026-10-10"}),
                encoding="utf-8",
            )
            write_items(
                directory,
                [
                    {
                        "key": "DUP-1",
                        "project": "DUP",
                        "project_name": "Dupes",
                        "type": "Task",
                        "summary": "First",
                        "status": "Done",
                        "assignee": "Samir Adeyemi",
                    },
                    {
                        "key": "DUP-1",
                        "project": "DUP",
                        "project_name": "Dupes",
                        "type": "Task",
                        "summary": "Second",
                        "status": "In Progress",
                        "priority": "Highest",
                        "due": "2026-10-01",
                    },
                ],
            )
            brief = pipeline.build(directory)
        self.assertTrue(any("duplicate work item DUP-1" in issue for issue in brief["data_quality"]))
        self.assertEqual(brief["top_risks"], [])

    def test_cli_writes_the_three_brief_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            code = pipeline.main([str(SAMPLE), "--out", str(output)])
            self.assertEqual(code, 0)
            for name in ("brief.json", "brief.md", "brief.html"):
                self.assertTrue((output / name).is_file())
            html = (output / "brief.html").read_text(encoding="utf-8")
            self.assertIn("Northline Advisory", html)
            self.assertIn("Checked and set aside", html)
            self.assertIn("FOR-3 is Done", html)
            self.assertIn("<details", html)
            self.assertIn("pending approval", html.lower())
            self.assertTrue((output / "Northline-Advisory-2026-10-10.html").is_file())


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = pipeline.build(SAMPLE)

    def test_jira_and_azure_devops_match_the_canonical_result(self) -> None:
        root = SAMPLE.parent
        for folder in ("northline-jira", "northline-ado"):
            brief = pipeline.build(root / folder)
            self.assertEqual(brief["result_id"], self.base["result_id"])
            self.assertEqual(
                [item["id"] for item in brief["top_risks"]],
                [item["id"] for item in self.base["top_risks"]],
            )
            self.assertEqual(brief["data_quality"], [])
        self.assertEqual(pipeline.build(root / "northline-jira")["source_profile"], "jira")
        self.assertEqual(pipeline.build(root / "northline-ado")["source_profile"], "azure_devops")

    def test_rewritten_profiles_match_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for profile in ("jira", "azure_devops"):
                dest = Path(tmp) / profile
                extract.write_profile_copy(SAMPLE, dest, profile)
                brief = pipeline.build(dest)
                self.assertEqual(brief["result_id"], self.base["result_id"])
                self.assertEqual(brief["source_profile"], profile)

    def test_export_dates_and_joined_comments(self) -> None:
        self.assertEqual(extract.parse_date("08/Oct/26 4:12 PM", "jira"), date(2026, 10, 8))
        self.assertEqual(extract.parse_date("10/08/2026 4:12:00 PM", "azure_devops"), date(2026, 10, 8))
        self.assertEqual(extract.parse_date("08.10.2026", "jira"), date(2026, 10, 8))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Comments", "as_of": "2026-10-10", "profile": "jira"}),
                encoding="utf-8",
            )
            (directory / "work_items.csv").write_text(
                "Issue key,Summary,Status,Comment,Comment\n"
                "C-1,Join me,Done,First note.,Second note.\n",
                encoding="utf-8",
            )
            loaded = pipeline.load_portfolio(directory)
        self.assertEqual(loaded["items"][0].comment, "First note. Second note.")

    def test_column_override_and_single_csv_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Renamed", "as_of": "2026-10-10"}),
                encoding="utf-8",
            )
            (directory / "columns.json").write_text(
                json.dumps({"profile": "canonical", "columns": {"key": "Ticket"}}),
                encoding="utf-8",
            )
            write_items(
                directory,
                [
                    {
                        "key": "T-1",
                        "project": "ONE",
                        "project_name": "One",
                        "type": "Task",
                        "summary": "Named ticket",
                        "status": "Done",
                        "assignee": "Mina Cho",
                    }
                ],
            )
            text = (directory / "work_items.csv").read_text(encoding="utf-8").replace("key,", "Ticket,", 1)
            (directory / "work_items.csv").write_text(text, encoding="utf-8")
            brief = pipeline.build(directory)
            self.assertEqual(brief["item_count"], 1)
            code = pipeline.main(
                [
                    str(directory / "work_items.csv"),
                    "--name",
                    "From a file",
                    "--as-of",
                    "2026-10-10",
                    "--columns",
                    str(directory / "columns.json"),
                    "--out",
                    str(directory / "out"),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue((directory / "out" / "From-a-file-2026-10-10.html").is_file())

    def test_csv_command_defaults_the_name_and_the_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            csv_path = directory / "Northwind.csv"
            csv_path.write_text(
                "key,summary,status\nT-1,Rotate keys,To Do\n",
                encoding="utf-8",
            )
            code = pipeline.main([str(csv_path), "--out", str(directory / "out")])
            self.assertEqual(code, 0)
            brief = json.loads((directory / "out" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual(brief["portfolio"], "Northwind")
            self.assertEqual(brief["as_of"], date.today().isoformat())

    def test_unknown_headers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Odd", "as_of": "2026-10-10"}),
                encoding="utf-8",
            )
            (directory / "work_items.csv").write_text("Foo,Bar\nA,B\n", encoding="utf-8")
            with self.assertRaises(pipeline.InputError):
                pipeline.build(directory)


class LearnedExportTests(unittest.TestCase):
    def test_blocker_and_major_are_counted_ahead_of_a_false_green(self) -> None:
        rows = ["Issue key,Project key,Project name,Issue Type,Summary,Status,Priority,Assignee,Updated"]
        for number in range(1, 9):
            rows.append(
                f"PHP-{number},PHP,phpBB,Bug,Open blocker {number},To Do,Blocker,,2025-11-01"
            )
        rows.append("PHP-20,PHP,phpBB,Bug,A major,To Do,Major,,2025-11-01")
        rows.append("PHP-21,PHP,phpBB,Bug,Another major,To Do,Major,Ada,2025-11-01")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.csv"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            brief = pipeline.brief_from_csv(path, "phpBB", "2025-11-05")
        self.assertEqual(brief["top_risks"][0]["id"], "unassigned-count:PHP:Highest")
        self.assertIn("8 Highest priority items have no assignee", brief["lead"])
        self.assertNotIn("are on track", brief["lead"])
        self.assertEqual(brief["pulse"][0]["state"], "needs_attention")
        page = pipeline.render_html(brief)
        self.assertIn("no due dates", page)
        self.assertIn("What this file does not support", page)
        self.assertIn("PHP-1", brief["top_risks"][0]["summary"])

    def test_missing_facts_are_not_called_on_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.csv"
            path.write_text(
                "ID,Work Item Type,Title,Assigned To,State\n10,Bug,Login,Ada,Active\n",
                encoding="utf-8",
            )
            brief = pipeline.brief_from_csv(path, "Board", "2026-10-11", profile="azure_devops")
        self.assertEqual(brief["top_risks"], [])
        self.assertEqual(brief["pulse"][0]["state"], "unchecked")
        self.assertIn("cannot be called on track", brief["lead"])
        self.assertIn("no due dates", brief["lead"])
        self.assertIn("Not checked", pipeline.render_html(brief))

    def test_overdue_without_a_notes_file_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.csv"
            path.write_text(
                "key,summary,status,priority,assignee,due\n"
                "T-2,Review patch,In Review,Medium,Ada,2024-10-02\n",
                encoding="utf-8",
            )
            brief = pipeline.brief_from_csv(path, "Tutorial", "2026-10-11")
        self.assertEqual(brief["top_risks"][0]["id"], "overdue:T-2")
        self.assertEqual(brief["pulse"][0]["state"], "needs_attention")
        self.assertIn("In Review", " ".join(brief["data_quality"]))
        self.assertIn("Data quality", pipeline.render_html(brief))

    def test_target_end_fills_an_empty_due_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "issues.csv"
            path.write_text(
                "Issue key,Summary,Status,Priority,Assignee,Due date,Custom field (Target end),"
                "Custom field (Story point estimate),Parent,Custom field (Epic Link)\n"
                "PT-1,First,To Do,Highest,, ,2026-10-01,5,,EPIC-1\n",
                encoding="utf-8",
            )
            brief = pipeline.brief_from_csv(path, "Cloud", "2026-10-11")
        finding = brief["top_risks"][0]
        self.assertEqual(finding["id"], "unassigned:PT-1")
        self.assertIn("1 Oct 2026", finding["summary"])
        due = next(item for item in finding["evidence"] if item["field"] == "due")
        self.assertEqual(due["value"], "2026-10-01")


def _estimate_item(**kwargs: str) -> dict:
    row = {
        "key": "EST-1",
        "project": "EST",
        "project_name": "Estimates",
        "type": "Story",
        "summary": "Cutover",
        "status": "In Progress",
        "priority": "High",
        "assignee": "Ada",
        "story_points": "3",
        "updated": "2026-10-09",
        "due": "2026-10-24",
    }
    row.update(kwargs)
    return row


def _build_rows(rows: list[dict], previous: list[dict] | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "meta.json").write_text(
            json.dumps(
                {
                    "portfolio": "Estimates",
                    "as_of": "2026-10-10",
                    "audience": "Delivery director",
                    "synthetic": True,
                }
            ),
            encoding="utf-8",
        )
        write_items(directory, rows)
        if previous is not None:
            write_items(directory, previous, "previous_work_items.csv")
        return pipeline.build(directory)


def _finding_ids(brief: dict) -> set[str]:
    return {finding["id"] for finding in brief["top_risks"] + brief["also_noted"] + brief["set_aside"]}


class EstimateTests(unittest.TestCase):
    def test_missing_estimates_count_from_three_and_ignore_lower_priority(self) -> None:
        brief = _build_rows(
            [
                _estimate_item(key="EST-1", story_points="", summary="One"),
                _estimate_item(key="EST-2", story_points="", remaining="4", original_estimate="8", summary="Hours only"),
                _estimate_item(key="EST-3", priority="Medium", story_points="", summary="Medium"),
                _estimate_item(key="EST-4", story_points="5", remaining="", summary="Has points"),
            ]
        )
        ids = _finding_ids(brief)
        self.assertEqual(
            {item for item in ids if item.startswith("missing-estimate")},
            {"missing-estimate:EST-1", "missing-estimate:EST-2"},
        )
        self.assertNotIn("remaining-above:EST-2", ids)

    def test_three_open_high_items_without_points_are_one_count(self) -> None:
        brief = _build_rows(
            [
                _estimate_item(key=f"EST-{number}", story_points="", priority="Highest", summary=f"Item {number}")
                for number in range(1, 4)
            ]
            + [_estimate_item(key="EST-9", story_points="", priority="High", summary="Lone")]
        )
        ids = _finding_ids(brief)
        self.assertIn("missing-estimate-count:EST:Highest", ids)
        self.assertNotIn("missing-estimate:EST-1", ids)
        self.assertIn("missing-estimate:EST-9", ids)
        count = next(item for item in brief["top_risks"] + brief["also_noted"] if item["id"].startswith("missing-estimate-count"))
        self.assertEqual(count["title"], "3 Highest priority items have no estimate.")
        self.assertIn("EST-1", count["summary"])
        self.assertIn("EST-2", count["summary"])
        self.assertIn("EST-3", count["summary"])
        self.assertEqual({item["id"] for item in count["evidence"]}, {"EST-1", "EST-2", "EST-3"})

    def test_remaining_above_original_and_done_remaining_cite_the_cells(self) -> None:
        brief = _build_rows(
            [
                _estimate_item(key="EST-1", original_estimate="5", remaining="8"),
                _estimate_item(key="EST-2", status="Done", priority="Medium", original_estimate="5", remaining="8"),
                _estimate_item(key="EST-3", status="Done", priority="Medium", original_estimate="5", remaining="0"),
                _estimate_item(key="EST-4", status="Done", priority="Medium", original_estimate="5", remaining=""),
                _estimate_item(key="EST-5", original_estimate="8", remaining="8"),
                _estimate_item(key="EST-6", original_estimate="8", remaining="3"),
            ]
        )
        ids = _finding_ids(brief)
        self.assertEqual(
            {item for item in ids if item.startswith("remaining-above") or item.startswith("done-remaining")},
            {"remaining-above:EST-1", "remaining-above:EST-2", "done-remaining:EST-2"},
        )
        finding = next(item for item in brief["top_risks"] + brief["also_noted"] if item["id"] == "remaining-above:EST-1")
        blob = f"{finding['title']} {finding['summary']} {finding['suggested_question']}".lower()
        self.assertIn("8", blob)
        self.assertIn("5", blob)
        for phrase in ("day", "hour", "forecast", "will miss", "velocity"):
            self.assertNotIn(phrase, blob)
        done = next(item for item in brief["top_risks"] + brief["also_noted"] if item["id"] == "done-remaining:EST-2")
        self.assertIn("8", done["title"])
        self.assertIn("Done", done["title"])

    def test_duration_text_is_not_turned_into_a_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Durations", "as_of": "2026-10-10", "synthetic": True}),
                encoding="utf-8",
            )
            (directory / "work_items.csv").write_text(
                "key,summary,status,priority,assignee,story_points,original_estimate,remaining\n"
                "T-1,Task,In Progress,High,Ada,2,1d,3d\n"
                "T-2,Other,In Progress,High,Ada,2,??,4\n",
                encoding="utf-8",
            )
            brief = pipeline.build(directory)
        self.assertNotIn("remaining-above:T-1", _finding_ids(brief))
        self.assertTrue(any("T-2 original_estimate is not a number" in issue for issue in brief["data_quality"]))
        self.assertFalse(any("1d" in issue or "3d" in issue for issue in brief["data_quality"]))

    def test_a_moved_point_or_remaining_figure_joins_the_change_list(self) -> None:
        brief = _build_rows(
            [
                _estimate_item(key="OLD-1", story_points="8", remaining="5", original_estimate="9"),
                _estimate_item(key="NEW-1", story_points="2", remaining="2"),
            ],
            [
                _estimate_item(key="OLD-1", story_points="5", remaining="3", original_estimate="4"),
                _estimate_item(key="GONE-1", story_points="1", remaining="1"),
            ],
        )
        by_key = {change["key"]: change for change in brief["changes"]}
        self.assertEqual(set(by_key), {"OLD-1", "NEW-1", "GONE-1"})
        fields = {field["field"]: field for field in by_key["OLD-1"]["fields"]}
        self.assertEqual(fields["story_points"], {"field": "story_points", "before": "5", "after": "8"})
        self.assertEqual(fields["remaining"], {"field": "remaining", "before": "3", "after": "5"})
        self.assertNotIn("original_estimate", fields)
        self.assertEqual(by_key["NEW-1"]["fields"][0]["after"], "added")
        self.assertEqual(by_key["GONE-1"]["fields"][0]["after"], "removed")
        text = pipeline.render_markdown(brief).lower()
        self.assertIn("does not forecast", text)
        for phrase in ("will miss", "predicted", "guarantee", "on schedule to fail"):
            self.assertNotIn(phrase, text)

    def test_jira_and_azure_devops_estimate_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "meta.json").write_text(
                json.dumps({"portfolio": "Headers", "as_of": "2026-10-10", "synthetic": True}),
                encoding="utf-8",
            )
            (directory / "work_items.csv").write_text(
                "Issue key,Summary,Status,Priority,Assignee,Story Points,Remaining Estimate,Original Estimate\n"
                "J-1,Task,Done,Medium,Ada,3,4,2\n"
                "J-2,Open,To Do,High,Ada,,,\n",
                encoding="utf-8",
            )
            jira = pipeline.build(directory)
            (directory / "work_items.csv").write_text(
                "ID,Work Item Type,Title,State,Priority,Assigned To,Effort,Remaining Work,Original Estimate\n"
                "11,User Story,Pay,Active,2,Ada,,5,8\n"
                "12,User Story,Ok,Active,2,Ada,3,1,5\n"
                "10,User Story,Login,Closed,2,Ada,,3,1\n",
                encoding="utf-8",
            )
            ado = pipeline.build(directory)
        self.assertEqual(
            {item for item in _finding_ids(jira) if "estimate" in item or "remaining" in item},
            {"remaining-above:J-1", "done-remaining:J-1", "missing-estimate:J-2"},
        )
        self.assertEqual(
            {item for item in _finding_ids(ado) if "estimate" in item or "remaining" in item},
            {"missing-estimate:11", "remaining-above:10", "done-remaining:10"},
        )


if __name__ == "__main__":
    unittest.main()
