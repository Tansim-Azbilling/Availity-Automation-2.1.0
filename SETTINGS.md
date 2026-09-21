# Settings panel

The Settings panel lets you change how Availity Automation searches claims, extracts amounts and denials, and saves results. Open it from the gear icon in the top-right of the main window. Close it with **Close** or **Escape**.

The panel is split into two sections:

- **All payers** — values that apply to every payer (and to the app as a whole).
- **Selected payer** — values that apply only to the payer currently chosen on the main screen (**Select Payer**). Changing that dropdown reloads this section.

Settings are saved to a local JSON file, not into the Excel workbook.

The file is `settings.json` in the **same folder as the app**:

- Frozen `.exe` — next to the executable (zip this whole folder when you share an agency pack; a lone `.exe` has no config).
- Running from source — next to `Availity_Automation.py` (the project root).

Until you click **Save**, changes in the form are not written to disk and are not used by a run. **Save** still writes global values plus the **currently selected payer** only; switch payer and Save again to store another payer in the same file.

The Settings footer shows the full path. **Open folder** reveals that directory in Explorer.

---

## Buttons

| Button | What it does |
| --- | --- |
| **Save** | Validates the form, then writes **both** the All-payers values and the currently selected payer’s overrides to `settings.json` beside the app. A status line such as `Saved for Healthfirst to settings.json.` appears on success. Invalid values or a write failure show an error dialog and nothing is written. |
| **Clear** | Removes saved overrides for the **currently selected payer only**, then reloads that payer’s built-in defaults (OrgID, Payer ID, NPI, flags, selector profile, line matching). Global (All payers) values are left unchanged. |
| **Open folder** | Opens the folder that contains `settings.json` so you can zip the agency pack. |
| **Close** | Hides the panel. Unsaved edits are discarded the next time the panel is opened. |

**Save** always requires a payer selected on the main screen, because it stores global values and one payer’s overrides in the same write.

---

## All payers

These values apply to every payer until a payer-specific field (in the section below) overrides them. The subtitle in the UI is: *These values apply to every payer until you override them below.*

### Default payer

Dropdown of GUI-enabled payers (Healthfirst, Integra, Villagecaremax, Aetna Better Health, Anthem BCBS, Fidelis, Empire).

This is the payer pre-selected on the main screen when the app starts. It does not change which payer a run uses if you pick a different one before clicking **Start Automation**.

**Default:** first GUI-enabled payer (`Healthfirst`).

### Default Provider NPI

Ten-digit National Provider Identifier filled on the Availity claim-search form when the selected payer does not have its own NPI override.

Used on the claim-search form when **Leave NPI blank** is checked (the default), or when the payer has no payer-specific **Provider NPI**. An unchecked payer NPI override takes precedence.

**Default:** `1730244294`  
**Validation:** must be exactly 10 digits.

### Remit check-date grace days

Used when a payer opens the remittance viewer (see **Use remittance viewer for denials**). The same Claim # can appear on more than one remits row with different Check/EFT dates.

When claim-detail **Check Date** is present, matching requires both Claim # and date. A remits Check/EFT Date is accepted if it equals the claim-detail Check Date, or falls up to this many **calendar days after** it (never before). When several remits rows are in range, the closest date wins (an exact match first).

When claim-detail Check Date is missing (`--`), remits matching uses Claim # only: the row is used **only if exactly one** remits row has that Claim #. Two or more rows with the same Claim # are treated as not found.

Example with the default of `7`: claim-detail Check Date `12/23/2025` also matches remits dates `12/24/2025` through `12/30/2025`.

**Default:** `7`  
**Validation:** whole number, `0` or greater.

### Format Excel output

When checked, `Automated.xlsx` is written with styled headers, cell borders, text wrap, autofit column widths, and a Recheck yes/no dropdown.

When unchecked, the same columns are written as a plain workbook (faster, smaller, no styling). Claim data is the same either way.

**Default:** on.

### Session error limit

How many **consecutive** logged-out / navigation-shell failures trip the circuit breaker and stop the batch. A successful re-login resets the counter.

**Default:** `3`  
**Validation:** whole number, at least `1`.

Typical session failures: Availity session expired, the claim-status shell failed to load, or CDP navigation to the payer search page failed.

### Transient error limit

How many **consecutive** retryable failures **at the same processing stage** trip the circuit breaker. A successfully handled row resets the counter. A different stage starts a new streak.

Stages include labels such as `Navigation failed`, `Form error`, `Search failed`, `Critical error`, and `Unknown row processing error`.

**Default:** `5`  
**Validation:** whole number, at least `1`.

### Medium file save interval

How often to write mid-batch progress to `Automated.xlsx` when the workbook has **101–500** rows.

A save also runs if at least 20 seconds have passed since the last save, even if fewer rows have been processed.

Workbooks with 100 rows or fewer save after every completed row; this interval does not apply to those files.

**Default:** `10` (every 10 processed rows)  
**Validation:** whole number, at least `1`.

### Large file save interval

Same idea for workbooks with **more than 500** rows. A save also runs if at least 30 seconds have passed since the last save.

**Default:** `25`  
**Validation:** whole number, at least `1`.

Lower intervals write more often (safer if you stop early; slower). Higher intervals reduce Excel write overhead on large files.

### Search miss phrases

Applies to **every Availity payer**. After Search is clicked, the automation waits for `#claimsTable` or a known miss. Wait/progress banners are ignored, even if their wording changes.

Each saved row is:

- **Phrase** — matched as a case-insensitive substring of any visible MUI search alert.
- **Outcome** — `Claim Not Found` or `Invalid Member ID`.

Built-in misses (Acknowledgement/Not Found, payer could not find any results, subscriber/member id not found) still apply. GUI phrases are extra and work the same on Healthfirst, Aetna, Fidelis, and the rest.

Do **not** add wait-banner copy such as “taking longer than expected”. Those banners are not claim outcomes. If Search never produces a table or a known miss, the existing blank-search path still opens the session-expired dialog after retries.

**Default:** none (built-in misses only).

**Validation:** optional. Empty phrases are skipped. At most 30 phrases, each at most 200 characters.

---

## Selected payer

Applies only to the payer chosen on the main screen. Each payer has its own saved entry, so Healthfirst OrgID and flags do not affect Fidelis, and so on.

Built-in defaults come from `PAYER_CONFIG`. Saved settings overlay those defaults for the next run.

### OrgID

Availity organization ID substituted into the payer’s claim-search URL (`orgId`).

**Validation:** required. Letters, numbers, hyphens, and underscores only (no spaces).

### Payer ID

Availity payer ID substituted into the same URL (`payerId`). Examples from built-in defaults: `80141T` (Healthfirst), `AETNA` (Aetna Better Health), `303` (Integra / Anthem / Empire).

**Validation:** same rules as OrgID.

### Provider NPI

Ten-digit NPI filled on this payer’s claim-search form. Overrides **Default Provider NPI** when **Leave NPI blank** is unchecked.

Leave the box empty with **Leave NPI blank** checked to use **Default Provider NPI**, or uncheck it and enter a 10-digit NPI for this payer only.

**Validation:** 10 digits unless **Leave NPI blank** is checked.

### Leave NPI blank

Checked by default. The Settings NPI text box is disabled, and `#providerNpi` on the claim-search form is filled with **Default Provider NPI**. Uncheck to type a payer-specific NPI instead.

### Selector profile

Which Availity UI generation the automation talks to. Wrong profile means date fields, claim-detail panels, and buttons will not be found.

| Value | When to use |
| --- | --- |
| `mui` | Current Material UI claim-status forms (HIPAA Standard tab, MUI date pickers). Default for most payers. |
| `legacy` | Older HIPAA-tab forms with plain `#patientBirthDate` / `#fromDate` / `#toDate` inputs. |
| `aetna` | Aetna Better Health dashboard: different member-ID field, no HIPAA tab, `#editSearch` / `#clearForm`, claim-detail panels keyed by `id` instead of `data-testid`. |

**Built-in default:** `mui`, except **Aetna Better Health** (`aetna`).

### Line match mode

How spreadsheet **VisitDate** is compared to service-date ranges on `#lineLevelTable` rows (and related date matching). Only matters when **Use line-level amounts** is on and **Filter lines by visit date** is on.

| Value | Matching rule |
| --- | --- |
| `exact` | Normalized ranges must be equal. Spreadsheet `01/03/2025` matches a line of `01/03/2025-01/03/2025`. Default. |
| `within_range` | VisitDate may fall anywhere inside the line’s service range (for example `01/08/2025` inside `12/25/2024-01/08/2025`). |
| `contains` | Legacy: VisitDate equals either bound of the line range. |

---

## Selected payer checkboxes

These flags control search, matching, amount extraction, and how Denial Reason is written. Built-in defaults differ by payer; the form shows that payer’s current effective values.

### Use HIPAA Standard tab

After the claim-search page loads, click the **HIPAA Standard** tab before filling the form.

Turn this **on** for Healthfirst, Integra, Anthem BCBS, Fidelis, and Empire. Turn it **off** for Villagecaremax and Aetna Better Health (those search UIs do not use that tab).

### Use line-level amounts

Read billed and paid amounts from matching rows in the line-level service table (`#lineLevelTable`), not from the claim-header billed/paid panels.

When **off**, amounts are taken from the header panels (Villagecaremax). When **on**, VisitDate filtering and the line-match options below apply.

Line-level payers with paid `$0` and billed greater than `$0` are treated as denied (unless remittance lookup is also enabled).

### Use remittance viewer for denials

Open Availity’s remittance viewer to obtain Denial Reason. Lookup runs whenever a **Claim ID** is present (Check Number is not required).

Turn this **on** for Villagecaremax, Aetna Better Health, Healthfirst, Integra, Anthem BCBS, and Empire. Leave it **off** for Fidelis (Reason/Remark codes only). Remit row matching uses **Remit check-date grace days**.

When remittance data is missing, Denial Reason is written as `Remittance not found` unless line/HIPAA codes fill the gap (see **Remittance only when Reason/Remark is --**).

### Remittance only when Reason/Remark is --

When **Use remittance viewer for denials** is also on, open remittance only if Reason/Remark (or HIPAA) denial is blank / `--`. HIPAA 585/101 expansion still runs first for Integra, Anthem BCBS, and Empire.

When **off**, remittance is first (Villagecaremax, Aetna Better Health). Mix and match this flag with remittance and HIPAA independently per payer.

### Skip remittance when check and paid are --

When **Use remittance viewer for denials** is also on, skip the remittance viewer if **Check Number**, **Check Date**, and **Paid Amount** are all still `--`. Denial Reason is then written as **In Process**.

Turn this **on** for Villagecaremax. Leave it **off** for other payers. If only some of those fields are `--` (for example Paid Amount is `$0.00`), remittance still runs.

### Read Finalized Date from results

Read **Finalized Date** from the results table row (`#claimsTable`) **before** opening claim detail.

Turn this **on** for Villagecaremax. When **off**, Finalized Date comes from the claim-detail panel and/or is promoted from Check Date.

### Reset search via Edit Search

After finishing a claim, return to a blank search form with the iframe **Edit Search** control, then **Clear Form**, instead of reloading the whole Availity shell.

Turn this **on** for Healthfirst, Integra, Villagecaremax, Aetna Better Health, Anthem BCBS, and Empire. If Edit Search / Clear Form fails, the run falls back to a full page reload.

Villagecaremax, Integra, Anthem BCBS, and Empire also return to the claim-search results list with the iframe **Back to Results** link (not browser back or the Results breadcrumb). That is only for remaining Availity submissions of the **same invoice**. After the last (or only) submission is harvested, the run uses **Edit Search** + **Clear Form** for the next invoice. If Back to Results fails between submissions, the run re-runs the search.

### Read HIPAA status codes

Build Denial Reason from the line table’s HIPAA Codes column (first code only, for example `F2:585` → `585`), looked up in `#codesTable` as type Status.

The line is expanded for Reason/Remark Codes only when that Status is:

- `585` — Denied Charge or Non-covered Charge
- `101` — Claim was processed as adjustment to previous claim

Output is the **description only** (no `[CODE]` prefix).

Turn this **on** for Integra, Anthem BCBS, and Empire. This is different from **Denial description only**, which still expands Reason/Remark codes but strips the code prefix.

### Canned paid denial text

When Claim Status is **Paid**, write Denial Reason `Fully Paid`. When status is **Paid (Overpayment)**, write `Fully Paid (Overpayment)`. Other statuses still use Reason/Remark (or HIPAA) lookup.

Turn this **on** for Healthfirst.

### Detect adjustment pairs

Treat same-date, same-billed service lines with one positive Paid and one negative Paid as an original/adjustment pair:

- billed is the original amount once
- paid is the net
- Claim Status is **Adjusted**
- Reason/Remark Codes come from the first positive-paid line, falling back to the matching negative-paid line

Turn this **on** for Healthfirst.

### Denial description only

Write Denial Reason as description text only (no `[CODE]` prefix) while still expanding Reason/Remark codes. Distinct from **Read HIPAA status codes**, which also changes *where* the code is looked up.

Turn this **on** for Healthfirst.

### Trim denial to first sentence

Keep only the first sentence of Denial Reason and strip UI chrome such as `Show more...`.

Turn this **on** for Anthem BCBS.

### Match results by visit date and amount

Pick `#claimsTable` rows by **Service Dates + Billed Amount** matched to spreadsheet **VisitDate + Amount**, instead of invoice / bundle matching.

Does not fan harvested claims out to other invoice sibling rows. Requires an **Amount** column in the collection file.

Turn this **on** for Aetna Better Health.

### Reuse search across sibling rows

When consecutive work-queue rows share the same search criteria (member ID, name, DOB, and service-date range), run **one** Availity search and reuse the results table for those rows.

When **off**, every row submits its own search. Ineligible rows (missing invoice or DOB) are never grouped.

**Default:** on for all payers unless you turn it off.

### Filter lines by visit date

When **Use line-level amounts** is on, only process `#lineLevelTable` rows whose service dates match spreadsheet VisitDate (using **Line match mode**).

When **off**, every service line on the claim is used. **Default:** on.

### Fall back to sole service line

If VisitDate does not match any line, but the claim has **exactly one** service line, use that line so billed/paid/denial data is still captured.

Turn this **off** when a sole unmatched line would be the wrong visit (Empire’s built-in default). **Default:** on for other line-level payers.

### Filter results by visit date

After matching `#claimsTable` rows by invoice number, keep only rows whose Service Dates contain the spreadsheet VisitDate.

Invoice-level harvest still opens every submission of that invoice once; VisitDate is applied later against scraped service lines. Enable this when a payer’s results table lists many dates for the same invoice and you need an extra date filter at results time.

**Default:** off.

---

## Built-in payer defaults

These are the code defaults shown before you save overrides. **Clear** restores this row for the selected payer.

| Option | Healthfirst | Integra | Villagecaremax | Aetna Better Health | Anthem BCBS | Fidelis | Empire |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Selector profile | mui | mui | mui | aetna | mui | mui | mui |
| Use HIPAA Standard tab | on | on | off | off | on | on | on |
| Use line-level amounts | on | on | off | on | on | on | on |
| Use remittance viewer | on | on | on | on | on | off | on |
| Remittance only when remarks are -- | on | on | off | off | on | off | on |
| Skip remittance when check and paid are -- | off | off | on | off | off | off | off |
| Read Finalized Date from results | off | off | on | off | off | off | off |
| Reset via Edit Search | on | on | on | on | on | off | on |
| Read HIPAA status codes | off | on | off | off | on | off | on |
| Canned paid denial text | on | off | off | off | off | off | off |
| Detect adjustment pairs | on | off | off | off | off | off | off |
| Denial description only | on | off | off | off | off | off | off |
| Trim denial to first sentence | off | off | off | off | on | off | off |
| Match results by visit and amount | off | off | off | on | off | off | off |
| Reuse search across sibling rows | on | on | on | on | on | on | on |
| Filter lines by visit date | on | on | on* | on | on | on | on |
| Fall back to sole service line | on | on | on* | on | on | on | **off** |
| Line match mode | exact | exact | exact | exact | exact | exact | exact |

\* Villagecaremax does not use the line table for amounts (`Use line-level amounts` is off), so the line-filter flags have no effect unless you also turn line-level extraction on.

---

## Validation summary

Save is rejected (nothing written) if any of the following fail:

| Field | Rule |
| --- | --- |
| Default payer | Must be a payer that appears in the GUI dropdown |
| Default Provider NPI | Exactly 10 digits |
| Remit check-date grace days | Whole number ≥ 0 |
| Session / transient error limits | Whole number ≥ 1 |
| Medium / large save intervals | Whole number ≥ 1 |
| Search miss phrases | Optional; each phrase ≤ 200 characters; at most 30 rows; outcome is Claim Not Found or Invalid Member ID |
| OrgID / Payer ID | Required; letters, numbers, hyphens, underscores only |
| Provider NPI | 10 digits, or **Leave NPI blank** checked |
| Selector profile | `mui`, `legacy`, or `aetna` |
| Line match mode | `exact`, `within_range`, or `contains` |

---

## How settings apply at runtime

1. The main-screen **Select Payer** value chooses which payer config to load.
2. Built-in `PAYER_CONFIG` for that payer is copied.
3. Saved OrgID / Payer ID are written into the claim-search URL.
4. Saved NPI, selector profile, boolean flags, and line-level options overlay the copy.
5. The run uses that merged config. Other payers’ saved entries are ignored for that run.

Changing Settings after **Open Edge** but before **Start Automation** is picked up for the next run, as long as you clicked **Save**. Changing payer flags mid-run is not applied to the batch already in progress.

Do not turn unrelated flags on together unless you know the portal supports that combination. For example, HIPAA-tab + remittance + Aetna selectors on a Healthfirst search page will not find the expected fields.
