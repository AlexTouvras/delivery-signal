# Current task

Research-led Delivery Signal brief redesign for product owners, scrum masters, and delivery leads.

## Done this turn

- Redesigned the Monday brief HTML: portfolio verdict first, status mix, week briefing rail, flow cast, and “The ask” decision cards before the field audit.
- Brought Orbit’s week-reading / sheets checker into this repo (`pipeline.py` + `docs/engine/`).
- Restyled `docs/index.html` with brand-first hero, role grid, and matching visual system (IBM Plex, atmospheric void, cyan accent).
- Updated Northline sample `out/` briefs and color assertions in `test_pipeline.py`.
- Verify: `python3 -m unittest delivery-signal.tests.test_pipeline delivery-signal.tests.test_site.PublishedFilesTests` → OK. Result id stays `f1fb4eaa03`.

## Still open

- One real project across two weekly exports (unchanged).
- Orbit products copy of `docs/` if still desired on alextouvras.com (Orbit already proxies `/delivery-signal/`).
