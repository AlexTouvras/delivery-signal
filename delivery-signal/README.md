# Delivery Signal

A weekly delivery brief from a Jira, Azure DevOps, or canonical CSV. The page says what changed, which work needs attention, and which decision is waiting. Every material claim cites a field. The same inputs produce the same result id.

Send the HTML file. The recipient does not need an account or the original export.

The reason to pay for that, and what is deliberately not built yet, is in [PLAN.md](PLAN.md). Nobody has paid. The page does not take orders.

## Install

The page has four options: Apps, One-liner, npm, and Hackable. Switch between macOS & Linux and Windows.

Apps is this page on a phone or a computer. Add it to the Home Screen, or install it from the browser.

One-liner, macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/AlexTouvras/delivery-signal/main/delivery-signal/install.sh | bash
```

One-liner, Windows:

```powershell
powershell -c "irm https://raw.githubusercontent.com/AlexTouvras/delivery-signal/main/delivery-signal/install.ps1 | iex"
```

That puts `delivery-signal` on macOS and Linux, and `run` on Windows. It uses Python when Python is already there. Run the same command again to update.

npm, when Node and Python are already on the machine:

```bash
npm install -g github:AlexTouvras/delivery-signal
delivery-signal
```

Hackable, from the checkout: `./run` on macOS and Linux, and `run` on Windows, inside `delivery-signal`.

## On a phone or a computer

The product page is a headline, then Quick Start. Orbit hosts the [`docs/`](../docs) folder at `/products/delivery-signal/` on alextouvras.com. Until that section is published, open the page from this repo:

https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html#quickstart

Choose this week's CSV. The export stays in that browser. Share or download the HTML brief. The page runs the same checker as this repo. The Northline sample produces result `f1fb4eaa03`.

The files live in [`docs/`](../docs). Open that preview to use the page today. https://alextouvras.github.io/delivery-signal/ is the Home Screen address after Pages is set to branch `main` and folder `/docs` (Settings, Pages, Deploy from a branch). That switch is not on: creating the Pages site returns HTTP 403 for this access.

## From your own terminal

In `delivery-signal`, `run` opens the configuration page on this computer.

Command Prompt: `run`. PowerShell: `.\run`. bash: `./run`.

On that page, choose Jira, Azure DevOps, or another tracker and enter the project. Jira and Azure DevOps are read from the project. A CSV is only for another tracker. The report opens on the page. The token is used for that read and is left out of the project file. The project file keeps the site and the project key. The page stays at http://127.0.0.1:8767 until you stop it with Ctrl+C.

`run report` writes the brief again from the saved import. A second import becomes last week's file.

A script can send the same connection without the page:

```bat
run connect jira --site https://acme.atlassian.net --project FORGE
run import
run connect ado --org https://dev.azure.com/contoso --project Fabrikam
run connect other --name "Board" --project WEB
run import issues.csv
run report
```

Jira reads `JIRA_EMAIL` and `JIRA_API_TOKEN` when the page's token field is empty. Azure DevOps reads `ADO_PAT`.

A single CSV is still one command: `run issues.csv`, `.\run issues.csv`, or `./run issues.csv`. Add `--previous issues-last-week.csv` when last week's export is there. The name defaults to the file name. The date defaults to today. The CSV stays on the machine. An agent in this repo uses that same command in the terminal it already has.

If Python is not already on the machine, the command prints the page link instead of starting an installer.

## On this computer

`run` is the configuration page. A second local page is still there for a browser on the same machine:

```bash
python3 delivery-signal/connect.py
```

The page is at http://127.0.0.1:8766. Open the Northline sample, or choose a CSV. The export stays on this machine.

From a terminal, the same checker without the page:

```bash
python3 delivery-signal/pipeline.py issues.csv --name "Northwind" --as-of 2026-10-10 --previous issues-last-week.csv

curl -F name="Northwind" -F as_of="2026-10-10" -F export=@issues.csv \
  http://127.0.0.1:8766/run -o brief.html
```

## Run

One export, without the page:

```bash
python3 delivery-signal/pipeline.py issues.csv --name "Northwind" --as-of 2026-10-10 --previous issues-last-week.csv
```

A folder, when you also have notes, milestones, or dependencies:

```bash
python3 delivery-signal/pipeline.py delivery-signal/samples/northline
python3 delivery-signal/tests/test_pipeline.py
```

Open `delivery-signal/samples/northline/out/brief.html`. The file to forward is next to it, named with the portfolio and the report date.

`samples/northline-jira` and `samples/northline-ado` are the same portfolio in the other two header layouts. All three briefs share one result id.

If a team renamed a column, put `columns.json` beside the export. `templates/columns.example.json` is the shape. Recognized profiles are `canonical`, `jira`, and `azure_devops`.

## What the sample shows

`samples/northline` is invented. The result id is `f1fb4eaa03`.

- The pulse card has a bar for story points Done, and a thinner bar for each epic or feature. An i on each section explains what is shown.
- Forge `FOR-12` is overdue and unassigned, and it blocks `FOR-20`.
- Lumen reports the epic 85% complete while 8 of 37 story points are Done. The project line is 8 of 38, because the decision sits outside the epic.
- Three Highest Lumen stories have an empty story point cell, so they are one count.
- `LUM-2` is Done and remaining is 2. `FOR-21` remaining is 5 and the original estimate is 3.
- `AST-4` story points moved from 3 to 5, and `LUM-6` remaining moved from 2 to 5.
- Forge feature `FOR-30` Pick tickets is 2 of 5 story points Done. The Forge project line is 12 of 26, and the epic line is 10 of 26.
- Cedar's 9 Oct note says the project is green. The open stories were last updated in August and are already overdue.
- The legacy-extract milestone is overdue, and its successor `FOR-3` is Done, so that signal is set aside on the first screen.
- Aster's note also says "on track". The export agrees, so it stays on track.

## Left for a later cycle

Accounts and payments. The live read does not write back to Jira or Azure DevOps. The next useful input is one real sanitized export.
