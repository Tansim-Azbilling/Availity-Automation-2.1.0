# Availity Automation 2.1.0

Tkinter + Playwright desktop app that checks claim status in Availity from an Excel workbook and writes enriched results back to `Automated.xlsx`.

## What this app does

- Streams an Excel collection file (`.xlsx`) into a local SQLite run database.
- Opens Microsoft Edge with remote debugging (CDP) and connects via Playwright.
- Runs a two-phase workflow: **Open Edge** (load file + browser) → sign in → **Start Automation**.
- Searches Availity claim status for the selected payer, matching rows by `InvoiceNumber` and visit/service dates.
- Extracts claim details (status, amounts, denial reason, finalized/check data).
- Supports **Recheck** (`yes` / `no` / empty) for resume and selective re-runs.
- Commits claim progress to SQLite after each row or search group and exports a complete `Automated.xlsx` at batch end or on Stop.

## Supported payers

All of these appear in the GUI **Select Payer** dropdown:

| Payer | Notes |
| --- | --- |
| Healthfirst | HIPAA tab, line-level amounts, bundle date fallback |
| Integra | HIPAA tab, line-level amounts |
| SWHNY | HIPAA tab, line-level + claim-level remark fallback |
| Villagecaremax | Remittance viewer for denial reasons |
| Aetna Better Health | Aetna selector profile (no HIPAA tab) |
| Anthem BCBS | HIPAA tab, line-level, bundle date fallback |
| Fidelis | HIPAA tab, line-level, bundle date fallback |
| Empire | HIPAA tab, strict line-level visit-date matching |

Payer behavior is configured in `PAYER_CONFIG` inside `availity_app/constants.py` (URL, HIPAA tab, line-level vs remittance, selector profile, etc.).

## Requirements

- Python 3.10+ recommended
- Microsoft Edge installed
- Python packages (see `requirements.txt`):
  - `pandas`
  - `openpyxl`
  - `playwright`
  - `numpy`

## Setup

1. Create and activate a virtual environment (recommended).
2. Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

3. Edge is launched automatically by the app with CDP on port `9222` (you do not need to start the browser manually). Close other Edge windows if the CDP port is stuck.

## Input Excel format

Required columns:

- `Alt Patient ID` (or `AltPatientID`)
- `Patient Name` (or `PatientName`; expected format: `LastName, FirstName`)
- `DOB`
- `StartDate`
- `EndDate`
- `Invoice #` (or `InvoiceNumber`)
- `Visit Date` (or `VisitDate`)

Optional:

- `Recheck` — `yes` (re-run Done rows), `no` (skip), or empty (new rows). The output file includes a yes/no dropdown for this column.

Notes:

- `PatientName` is split internally into `Last_Name` and `First_Name`.
- Date and invoice values are normalized for matching (including Excel float/serial quirks).
- If `InvoiceNumber` is empty, that row is skipped with an error status.

## Run the app

From project root:

```bash
python Availity_Automation.py
```

In the GUI:

1. Select **Collection File** (input `.xlsx`).
2. Select **Output Folder**.
3. Enter **Claim Search Limit** (positive integer batch size).
4. Choose **Select Payer** (all configured payers are listed).
5. Click **Open Edge** — imports or resumes the run and launches Edge.
6. Sign in to Availity in the Edge window when prompted.
7. Click **Start Automation**.

Settings (OrgID, Payer ID, NPI, and payer flags) are stored in `settings.json` in the same folder as the app — next to the `.exe` or next to `Availity_Automation.py`. Zip that folder when sharing a configured copy; do not send the `.exe` alone.

Use **Stop** to finish the active row, commit progress, and export `Automated.xlsx`. **Export current progress** can create a snapshot during a run. To rerun completed rows, edit `Recheck` in the exported workbook and use **Import Recheck edits** before starting another batch; only Recheck values are imported.

The live run database is under `%LOCALAPPDATA%\AvailityAutomation\runs`. This location must be writable and persist across VDI resets. Do not place the live database on a network share. `settings.json` remains beside the app.

### SQLite run storage

**Open Edge** streams the selected collection workbook into a local SQLite database. The app resumes that database when you reopen the same source file with the same output folder and payer. Claim results are committed as processing continues, so `Automated.xlsx` is an export snapshot, not the live progress store. The database contains claim and patient data; keep the local user profile protected and persistent, and include it when backing up an unfinished run.

**Export current progress**, batch completion, and **Stop** write a full `Automated.xlsx` to the selected output folder. Exporting reads a consistent database snapshot while claim processing can continue. If an export fails, the previously exported workbook is preserved; retry after closing the workbook in Excel. To resume, reopen the original collection file with the same output folder and payer. Editing result cells in `Automated.xlsx` does not update the database; use **Import Recheck edits** to bring back only `Recheck` changes.

If a matching `Automated.xlsx` already exists in the output folder, the first SQLite import copies its completed results after checking every row's member, invoice, and visit date. It then replays any newer `Automated_progress.jsonl` entries from the previous app. A mismatch stops the import so results cannot be attached to the wrong collection. The old files remain available for rollback. Keep the source workbook unchanged while a run is in progress; use a different output folder for a changed source file.

Row stats (**Total Rows Read**, **Rows with Recheck**, **New Rows**) update after the file is loaded. An activity log and progress bar show live status.

## Output files

The app writes a full workbook snapshot in your output folder:

- `Automated.xlsx` — all imported rows, including pending rows, at each export

Added / maintained output columns include:

- `Claim ID`
- `Billed Amount`
- `Paid Amount`
- `Claim Status`
- `Denial Reason`
- `Last Action Taken`
- `Finalized Date`
- `Check Number`
- `Check Date`
- `Recheck`
- `AutomationStatus`
- `LastError`

Close `Automated.xlsx` in Excel if a save fails (file lock).

## Common statuses and error handling

Rows can be marked with statuses such as:

- `No claims found. Advised to search manually`
- `Claim not found. Search manually`
- `Claim Not found`
- `Invalid Member ID`
- `DOB Not Found`
- `Paid (Overpayment)`
- `Navigation failed`
- `Form error`
- `Search failed`
- `Missing InvoiceNumber`
- `Critical error`

The app continues with later rows when one row fails. SQLite commits keep progress if you stop early.

## Adding a payer

1. Add an entry to `PAYER_CONFIG` in `availity_app/constants.py` with URL and feature flags.
2. Optionally set `selector_profile` to `mui` (default), `legacy`, or `aetna`.
3. Optionally set `gui_enabled: False` to hide a payer from the dropdown (default is shown).

No other code changes are required for the dropdown, navigation, or extraction paths.

## Troubleshooting

- **Browser / CDP connection fails**  
  Close all Edge windows and click **Open Edge** again. Port `9222` must be free for CDP.

- **Not ready / Start Automation disabled**  
  Complete **Open Edge** and sign in to Availity first.

- **No page loaded / selectors time out**  
  Confirm you are logged into Availity and can open the payer claim-status page manually.

- **Missing columns error**  
  Verify Excel headers match the required names exactly.

- **Save failed**  
  Close `Automated.xlsx` if it is open in Excel, then retry or re-run.

- **Playwright errors on first setup**  
  Re-run `python -m playwright install chromium`.

## Project structure

- `Availity_Automation.py` — entry point
- `availity_app/` — GUI, Edge/CDP driver, Excel I/O, and claim automation
- `requirements.txt` — Python dependencies
- `README.md` — this guide

## Disclaimer

This tool automates interactions with payer portals. Use it in compliance with your organization's security, privacy, and portal usage policies.
