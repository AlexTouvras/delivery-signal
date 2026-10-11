#!/usr/bin/env python3
"""Copy the checker and the Northline sample into the published page.

GitHub Pages serves /docs from this repo. The page runs these same Python
files in the browser, so the result id matches a local run.
"""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
SAMPLE = PACKAGE / "samples" / "northline"
SAMPLE_FILES = (
    "meta.json",
    "work_items.csv",
    "previous_work_items.csv",
    "notes.csv",
    "milestones.csv",
    "dependencies.csv",
)


def refresh() -> None:
    engine = DOCS / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PACKAGE / "pipeline.py", engine / "pipeline.py")
    shutil.copyfile(PACKAGE / "extract.py", engine / "extract.py")
    sample = DOCS / "sample"
    sample.mkdir(parents=True, exist_ok=True)
    for name in SAMPLE_FILES:
        shutil.copyfile(SAMPLE / name, sample / name)
    for stale in sample.iterdir():
        if stale.name not in SAMPLE_FILES:
            stale.unlink()


if __name__ == "__main__":
    refresh()
