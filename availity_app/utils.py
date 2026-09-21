"""Retries, date/amount parsing, and CSV row helpers."""
import random
import re
import time
from collections import defaultdict

import pandas as pd

from availity_app.constants import (
    ADJUSTED_STATUS,
    LINE_LEVEL_CONFIG,
    NEED_MANUAL_REVIEW_STATUS,
    OUTPUT_COLUMNS,
    PAID_OVERPAYMENT_STATUS,
    REQUIRED_COLUMNS,
    resolved_output_columns,
)
from availity_app.logging_gui import log_to_gui

def retry_with_backoff(action_name, fn, attempts=3, base_delay=0.7, max_delay=4.0, jitter=0.2):
    """Run a transient browser action with bounded retries and exponential backoff.

    Delay formula: delay = min(max_delay, base_delay * 2^(attempt-1)) * jitter_factor
    Jitter (±20% by default) spreads retries to avoid synchronized spikes.
    Raises the last exception when all attempts are exhausted.

    Args:
        action_name: Human-readable label shown in retry log messages.
        fn:          Zero-argument callable to execute.
        attempts:    Maximum number of attempts (first try + retries).
        base_delay:  Wait in seconds before the second attempt.
        max_delay:   Cap on wait time between any two attempts.
        jitter:      Fractional spread added to each delay (e.g. 0.2 = ±20%).
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_error = e
            if getattr(e, 'fatal', False):
                raise
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if jitter > 0:
                delay *= random.uniform(1 - jitter, 1 + jitter)
            log_to_gui(
                f"  ⚠️ {action_name} failed (attempt {attempt}/{attempts}): {e}. "
                f"Retrying in {delay:.2f}s...\n",
                "error",
            )
            time.sleep(delay)
    raise last_error


# ============================================================================
# SECTION 4 — DATE AND AMOUNT UTILITIES
# ============================================================================

def normalize_date(date_str):
    """Zero-pad a date to MM/DD/YYYY for consistent string comparison.

    Availity renders dates as M/D/YYYY; CSVs may use either format.
    Normalising both sides before comparing prevents false mismatches.
    Returns the original string unchanged if it cannot be parsed.
    """
    try:
        parts = str(date_str).strip().split('/')
        if len(parts) == 3:
            return f"{parts[0].zfill(2)}/{parts[1].zfill(2)}/{parts[2]}"
        return date_str
    except Exception:
        return date_str


def scalar_cell(val, default=None):
    """Reduce a spreadsheet cell to a scalar safe for ``if`` checks and ``pd.isna``."""
    if val is None:
        return default
    if isinstance(val, pd.Series):
        if val.empty:
            return default
        val = val.iloc[0]
    if isinstance(val, (pd.DataFrame, pd.Index)):
        return default
    try:
        if pd.isna(val):
            return default
    except (ValueError, TypeError):
        pass
    return val


def coerce_visit_date(visit_date, default=''):
    """Return a scalar visit-date string safe for truthiness and normalization."""
    val = scalar_cell(visit_date, default=None)
    if val is None:
        return default
    text = str(val).strip()
    if not text or text.lower() == 'nan':
        return default
    return text


_SLASH_DATE_RE = re.compile(r'(\d{1,2}/\d{1,2}/\d{4})')


def parse_service_date_range(text):
    """Extract 'MM/DD/YYYY-MM/DD/YYYY' from Availity line-level cell text.

    Handles a single date, two slash-dates separated by hyphen/space/newline
    (e.g. ``12/25/2024-01/17/2025``), and ISO timestamps from Excel.
    """
    if text is None:
        return None
    s = str(scalar_cell(text, default='')).strip()
    if not s or s.lower() == 'nan':
        return None

    dates = _SLASH_DATE_RE.findall(s)
    if dates:
        start = normalize_date(dates[0])
        end = normalize_date(dates[-1])
        return f"{start}-{end}"

    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.notna(dt):
            d = dt.strftime('%m/%d/%Y')
            return f"{d}-{d}"
    except Exception:
        pass
    return None


def normalize_date_range(date_str):
    """Normalise a date or date-range string to 'MM/DD/YYYY-MM/DD/YYYY'.

    The line-level table uses ranges (e.g. '1/1/2026-1/31/2026').
    A single date is treated as a range where start == end.
    """
    try:
        date_str = scalar_cell(date_str, default='')
        s = str(date_str).strip()
        if not s:
            return s
        parsed = parse_service_date_range(s)
        if parsed:
            return parsed
        n = normalize_date(s)
        return f"{n}-{n}"
    except Exception:
        return date_str


def _parse_normalized_date(date_str):
    """Parse a normalised MM/DD/YYYY string to a comparable datetime."""
    try:
        bounds = _line_range_bounds(str(date_str))
        if bounds is None:
            return None
        start, _ = bounds
        dt = pd.to_datetime(start, format='%m/%d/%Y')
        if pd.notna(dt):
            return dt.to_pydatetime()
    except Exception:
        pass
    return None


def _line_range_bounds(line_range):
    """Return (start, end) MM/DD/YYYY strings parsed from a line date range."""
    parsed = parse_service_date_range(line_range)
    if not parsed:
        return None
    start, _, end = parsed.partition('-')
    if not start or not end:
        return None
    return start, end


def visit_date_matches_line(target_range, line_range, match_mode='exact'):
    """Return True when a CSV VisitDate range matches a line's service date range.

    Both arguments are normalised ``MM/DD/YYYY-MM/DD/YYYY`` strings.
    ``match_mode`` options:
      ``exact`` — normalised ranges must be equal (e.g. VisitDate ``01/03/2025``
                  matches line ``01/03/2025-01/03/2025``).
      ``within_range`` — the visit date may fall anywhere inside the line's
                         service range (e.g. VisitDate ``01/08/2025`` matches
                         bundle line ``12/25/2025-01/08/2025``).
      ``contains`` — legacy: visit date equals either bound of the line range.
    """
    target_norm = normalize_date_range(coerce_visit_date(target_range))
    line_norm = normalize_date_range(coerce_visit_date(line_range))
    if not line_norm or not target_norm:
        return False
    if match_mode == 'exact':
        return line_norm == target_norm
    if match_mode == 'within_range':
        visit_dt = _parse_normalized_date(target_norm)
        bounds = _line_range_bounds(line_norm)
        if visit_dt is None or bounds is None:
            return False
        line_start, line_end = bounds
        start_dt = pd.to_datetime(line_start, format='%m/%d/%Y').to_pydatetime()
        end_dt = pd.to_datetime(line_end, format='%m/%d/%Y').to_pydatetime()
        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt
        return start_dt <= visit_dt <= end_dt
    if line_norm == target_norm:
        return True
    target_bounds = _line_range_bounds(target_norm)
    line_bounds = _line_range_bounds(line_norm)
    if not target_bounds or not line_bounds:
        return False
    target_start, target_end = target_bounds
    line_start, line_end = line_bounds
    if target_start in (line_start, line_end):
        return True
    return target_end in (line_start, line_end)


def resolve_line_level_config(payer_config=None):
    """Merge global LINE_LEVEL_CONFIG with optional per-payer ``line_level`` overrides."""
    cfg = dict(LINE_LEVEL_CONFIG)
    if payer_config:
        cfg.update(payer_config.get('line_level', {}))
    return cfg


def day_offset_in_range(visit_date, line_date_range):
    """Return days from line range start to VisitDate, or None if not in range."""
    target = normalize_date_range(coerce_visit_date(visit_date))
    line_norm = normalize_date_range(coerce_visit_date(line_date_range))
    visit_dt = _parse_normalized_date(target)
    bounds = _line_range_bounds(line_norm)
    if visit_dt is None or bounds is None:
        return None
    line_start, line_end = bounds
    start_dt = pd.to_datetime(line_start, format='%m/%d/%Y').to_pydatetime()
    end_dt = pd.to_datetime(line_end, format='%m/%d/%Y').to_pydatetime()
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    if not (start_dt <= visit_dt <= end_dt):
        return None
    return (visit_dt.date() - start_dt.date()).days


def invoice_row_indices(df, invoice_number):
    """Return positional indices of dataframe rows sharing ``invoice_number``."""
    if df is None or invoice_number in (None, ''):
        return []
    target = format_invoice_number(invoice_number, '')
    if not target:
        return []
    from availity_app.invoice_index import get_invoice_index

    index = get_invoice_index(df)
    return list(index.invoice_to_indices.get(target, ()))


def resolve_invoice_line_context(df, row_index, row_data=None):
    """Build invoice/VisitDate context for day-position line selection.

    Returns dict with:
      invoice_row_count, invoice_row_occurrence, visit_date_duplicated
    """
    if row_data is None and df is not None and row_index is not None:
        row_data = df.iloc[row_index]
    invoice = safe_invoice_number(row_data, 'InvoiceNumber', '') if row_data is not None else ''
    indices = invoice_row_indices(df, invoice)
    occurrence = 0
    if row_index is not None and indices:
        try:
            occurrence = indices.index(row_index)
        except ValueError:
            occurrence = 0

    visit_target = normalize_date_range(
        coerce_visit_date(safe_field(row_data, 'VisitDate') if row_data is not None else '')
    )
    visit_count = 0
    if visit_target and df is not None:
        for i in indices:
            other = normalize_date_range(
                coerce_visit_date(safe_field(df.iloc[i], 'VisitDate'))
            )
            if other == visit_target:
                visit_count += 1

    return {
        'invoice_row_count': len(indices),
        'invoice_row_occurrence': occurrence,
        'visit_date_duplicated': visit_count > 1,
    }


def _select_day_position_line(
    line_rows, range_matches, target, *,
    invoice_row_count=None, invoice_row_occurrence=None,
    visit_date_duplicated=False,
):
    """Pick one within-range line by day offset or invoice order.

    Returns ``(matches, unresolved)`` where ``unresolved`` is True when the
    claim cannot be identified without guessing.
    """
    if invoice_row_count is None or invoice_row_occurrence is None:
        log_to_gui(
            "    ⚠️ Day-position fallback needs invoice row context — "
            "cannot identify line\n",
            "error",
        )
        return [], True

    if len(line_rows) != invoice_row_count:
        log_to_gui(
            f"    ⚠️ Line count ({len(line_rows)}) != invoice input rows "
            f"({invoice_row_count}) — cannot identify claim\n",
            "error",
        )
        return [], True

    if visit_date_duplicated:
        index = invoice_row_occurrence
        log_to_gui(
            f"    ℹ️ Duplicate VisitDate {target}; using invoice row "
            f"occurrence {index} of {invoice_row_count}\n",
            "info",
        )
    else:
        # Use the first candidate's range for day offset (all candidates that
        # contain the visit share a usable start for position mapping).
        index = day_offset_in_range(target, range_matches[0]['date_range'])
        if index is None:
            log_to_gui(
                f"    ⚠️ Could not compute day offset for VisitDate {target}\n",
                "error",
            )
            return [], True
        log_to_gui(
            f"    ℹ️ Within-range day-position: VisitDate {target} → "
            f"offset {index} ({len(range_matches)} candidate(s))\n",
            "info",
        )

    if index < 0 or index >= len(range_matches):
        if invoice_row_occurrence is not None and 0 <= invoice_row_occurrence < len(range_matches):
            index = invoice_row_occurrence
            log_to_gui(
                f"    ℹ️ Non-consecutive date offset out of bounds — "
                f"using invoice row occurrence {index} of {invoice_row_count}\n",
                "info",
            )
        else:
            log_to_gui(
                f"    ⚠️ Day-position index {index} out of bounds for "
                f"{len(range_matches)} within-range line(s) — not guessing\n",
                "error",
            )
            return [], True

    selected = range_matches[index]
    log_to_gui(
        f"    ✓ Day-position line: {selected['date_range']} "
        f"(Billed: {selected.get('billed', '--')}, "
        f"Paid: {selected.get('paid', '--')})\n",
        "success",
    )
    return [selected], False


def filter_line_rows_by_visit_date(
    line_rows, visit_date, cfg=None, payer_config=None, *,
    invoice_row_count=None, invoice_row_occurrence=None,
    visit_date_duplicated=False,
):
    """Filter scraped line-row dicts to those matching ``visit_date``.

    Each row dict must include a ``date_range`` key.  When
    ``filter_by_visit_date`` is False in config, returns all rows unchanged.

    Matching order when ``match_mode`` is ``exact`` (default):
      1. Exact service-date range match
      2. If ``fallback_within_range`` is True and step 1 found nothing,
         match VisitDate inside a multi-day line range (bundle claims).
         When ``range_match_by_day_position`` is also True, pick one line by
         day offset (or invoice-row order for duplicate VisitDates), gated by
         table-line count == invoice input-row count.
      3. If ``fallback_to_sole_line`` is True and only one line exists, use it

    Returns ``(matches, unresolved)``.  ``unresolved`` is True when
    day-position fallback cannot identify a line without guessing.
    """
    cfg = cfg or resolve_line_level_config(payer_config)
    if not cfg.get('filter_by_visit_date', True):
        return list(line_rows), False

    visit_date = coerce_visit_date(visit_date)
    if not visit_date:
        return list(line_rows), False

    target = normalize_date_range(visit_date)
    match_mode = cfg.get('match_mode', 'exact')
    if match_mode == 'within_range':
        matches = [
            entry for entry in line_rows
            if visit_date_matches_line(target, entry['date_range'], 'within_range')
        ]
    else:
        matches = [
            entry for entry in line_rows
            if visit_date_matches_line(target, entry['date_range'], 'exact')
        ]
        if not matches and cfg.get('fallback_within_range'):
            range_matches = [
                entry for entry in line_rows
                if visit_date_matches_line(target, entry['date_range'], 'within_range')
            ]
            if range_matches:
                log_to_gui(
                    f"    ℹ️ No exact line match for VisitDate {target}; "
                    f"using within-range fallback ({len(range_matches)} row(s))\n",
                    "info",
                )
                if cfg.get('range_match_by_day_position'):
                    return _select_day_position_line(
                        line_rows, range_matches, target,
                        invoice_row_count=invoice_row_count,
                        invoice_row_occurrence=invoice_row_occurrence,
                        visit_date_duplicated=visit_date_duplicated,
                    )
                matches = range_matches
            else:
                scraped = ', '.join(
                    normalize_date_range(row['date_range']) for row in line_rows
                )
                log_to_gui(
                    f"    ℹ️ Within-range fallback: no line contains VisitDate "
                    f"{target} (scraped: {scraped})\n",
                    "info",
                )
                if cfg.get('range_match_by_day_position'):
                    return [], True

    if not matches and cfg.get('fallback_to_sole_line') and len(line_rows) == 1:
        only = line_rows[0]
        log_to_gui(
            f"    ℹ️ VisitDate {target} did not match line dates "
            f"({only['date_range']}); using sole service line\n",
            "info",
        )
        return [only], False
    return matches, False


_INVALID_DATE_MARKERS = frozenset({
    '--', '-', 'n/a', 'na', 'none', 'null', 'unknown', 'tbd', '#n/a',
    'notfound', 'missing', 'dob nf', 'dob n/a',
})


def _is_invalid_date_marker(date_str):
    """True when a spreadsheet cell is a placeholder, not a real date."""
    s = re.sub(r'\s+', ' ', str(date_str).strip().lower())
    if not s or s == 'nan':
        return True
    if s in _INVALID_DATE_MARKERS:
        return True
    return 'not found' in s


def normalize_dob(dob_str):
    """Normalize DOB to MM/DD/YYYY for Availity form input.

    Handles common CSV quirks:
    - ISO/Timestamp-like values (e.g. 1969-05-11, 1969-05-11 00:00:00)
    - Excel serial dates
    - 3-digit years where leading '1' is dropped (e.g. 05/11/969)
    """
    s = str(dob_str).strip()
    if _is_invalid_date_marker(s):
        return ''

    # Excel date serial (days from 1899-12-30)
    if s.isdigit() and len(s) <= 5:
        try:
            dt = pd.to_datetime('1899-12-30') + pd.to_timedelta(int(s), unit='D')
            return dt.strftime('%m/%d/%Y')
        except Exception:
            pass

    # Heuristic for malformed year like 05/11/969 -> 05/11/1969
    try:
        parts = s.replace('-', '/').split('/')
        if len(parts) == 3:
            mm, dd, yy = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if len(yy) == 3 and yy.isdigit():
                s = f"{mm}/{dd}/1{yy}"
    except Exception:
        pass

    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.notna(dt):
            return dt.strftime('%m/%d/%Y')
    except Exception:
        pass

    result = normalize_date(s)
    if _is_invalid_date_marker(result) or not re.match(r'^\d{2}/\d{2}/\d{4}$', result):
        return ''
    return result


def normalize_search_date(date_str):
    """Normalize a service/visit date for Availity search; return '' if unusable."""
    if _is_invalid_date_marker(date_str):
        return ''
    result = normalize_dob(date_str)
    if not result or _is_invalid_date_marker(result):
        return ''
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', result):
        return ''
    return result


def row_has_valid_dob(row_data, safe_field_fn):
    """True when the row has a parseable DOB for the Availity search form."""
    raw = safe_field_fn(row_data, 'DOB')
    if _is_invalid_date_marker(raw):
        return False
    return bool(normalize_dob(raw))


def service_dates_cross_year(from_date, to_date):
    """True when service From and To fall in different calendar years."""
    from_parts = split_date_parts(from_date)
    to_parts = split_date_parts(to_date)
    if not from_parts or not to_parts:
        return False
    return from_parts[2] != to_parts[2]


def service_date_fill_plans(from_date, to_date):
    """Ordered fill plans for MUI service-date range pickers.

    Each plan is a dict with:
      mode  — 'robust' | 'year_first' | 'hidden_input'
      order — ('from', 'to') or ('to', 'from') field fill sequence
    """
    cross = service_dates_cross_year(from_date, to_date)
    if cross:
        plans = [
            {'mode': 'robust', 'order': ('to', 'from')},
            {'mode': 'robust', 'order': ('from', 'to')},
            {'mode': 'year_first', 'order': ('to', 'from')},
        ]
        hidden_orders = [('to', 'from'), ('from', 'to')]
    else:
        plans = [
            {'mode': 'robust', 'order': ('to', 'from')},
            {'mode': 'robust', 'order': ('from', 'to')},
        ]
        hidden_orders = [('to', 'from'), ('from', 'to')]
    for order in hidden_orders:
        plans.append({'mode': 'hidden_input', 'order': order})
    return plans


def resolve_service_search_dates(row_data, safe_field_fn):
    """Return (start_date, end_date) for the claim search form.

    Uses StartDate/EndDate when valid; falls back to VisitDate; mirrors a single
    date to both ends when only one bound is present.
    """
    start = normalize_search_date(safe_field_fn(row_data, 'StartDate'))
    end = normalize_search_date(safe_field_fn(row_data, 'EndDate'))
    visit = normalize_search_date(safe_field_fn(row_data, 'VisitDate'))
    if not start and visit:
        start = visit
    if not end and visit:
        end = visit
    if start and not end:
        end = start
    if end and not start:
        start = end
    return start, end


def split_date_parts(date_str):
    """Split a date into (mm, dd, yyyy) zero-padded strings for MUI spinbutton entry."""
    s = str(date_str).strip()
    if not s or s.lower() == 'nan' or _is_invalid_date_marker(s):
        return None

    # Always parse through pandas first — avoids YYYY-MM-DD read as MM/DD/YY.
    try:
        dt = pd.to_datetime(s, errors='coerce')
        if pd.notna(dt):
            return dt.strftime('%m'), dt.strftime('%d'), dt.strftime('%Y')
    except Exception:
        pass

    normalized = normalize_dob(s)
    if normalized and normalized != s:
        try:
            dt = pd.to_datetime(normalized, errors='coerce')
            if pd.notna(dt):
                return dt.strftime('%m'), dt.strftime('%d'), dt.strftime('%Y')
        except Exception:
            pass
        s = normalized

    if '/' in s:
        parts = s.split('/')
        if len(parts) == 3:
            mm, dd, yy = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if mm.isdigit() and dd.isdigit() and yy.isdigit() and int(mm) <= 12 and int(dd) <= 31:
                if len(yy) == 2:
                    yy = f'19{yy}' if int(yy) > 50 else f'20{yy}'
                return mm.zfill(2), dd.zfill(2), yy.zfill(4)

    digits = ''.join(c for c in s if c.isdigit())
    if len(digits) == 8:
        mm, dd, yyyy = digits[0:2], digits[2:4], digits[4:8]
        if int(mm) <= 12 and int(dd) <= 31:
            return mm, dd, yyyy
    return None


def parse_amount(amount_str):
    """Parse a currency string like '$1,234.50' to float, or None if blank.

    Returns None for empty cells or '--' so callers can distinguish
    "zero dollars paid" from "value not available".
    """
    try:
        cleaned = str(amount_str).strip().replace('$', '').replace(',', '')
        if cleaned in ('', '--'):
            return None
        return float(cleaned)
    except Exception:
        return None


def amounts_equal(left, right):
    """True when two currency values represent the same cent amount."""
    left_val = parse_amount(left)
    right_val = parse_amount(right)
    if left_val is None or right_val is None:
        return False
    return round(left_val, 2) == round(right_val, 2)


def results_row_matches_visit_and_amount(
    row_range, billed_text, visit_date, amount,
):
    """True when a #claimsTable Service Dates + Billed Amount pair matches.

    ``row_range`` is a normalised or raw service-date cell. ``billed_text``
    is the Billed Amount cell. Spreadsheet ``VisitDate`` must match the
    range exactly; ``Amount`` is compared as currency, not as a string.
    """
    visit_date = coerce_visit_date(visit_date)
    if not visit_date or not amounts_equal(billed_text, amount):
        return False
    target = normalize_date_range(visit_date)
    row_range_norm = normalize_date_range(row_range)
    return visit_date_matches_line(target, row_range_norm, 'exact')


def payer_matches_results_by_visit_and_amount(payer_config=None):
    """True when #claimsTable rows are picked by VisitDate + Amount."""
    return bool(
        payer_config and payer_config.get('match_results_by_visit_and_amount')
    )


def format_currency_amount(value):
    """Format a numeric amount as ``$X.XX``."""
    return f"${value:.2f}"


def payer_detects_adjustment_pairs(payer_config=None):
    """True when this payer pairs original + negative-paid adjustment lines."""
    return bool(payer_config and payer_config.get('detect_adjustment_pairs'))


def _line_paid_number(entry):
    """Paid amount as float; blank / ``--`` is 0.0."""
    val = parse_amount((entry or {}).get('paid'))
    return 0.0 if val is None else val


def _line_billed_number(entry):
    """Billed amount as float; blank / ``--`` is 0.0."""
    val = parse_amount((entry or {}).get('billed'))
    return 0.0 if val is None else val


def _line_billed_key(entry):
    """Rounded billed amount used to group original/adjustment pairs."""
    val = parse_amount((entry or {}).get('billed'))
    if val is None:
        return None
    return round(val, 2)


def adjustment_pair_group_key(entry):
    """Grouping key for original/adjustment pairs: ``(date_range, billed)``."""
    return ((entry or {}).get('date_range') or '', _line_billed_key(entry))


def classify_adjustment_pair_roles(line_rows):
    """Return a role per line: ``'original'``, ``'adjustment'``, or None.

    Lines are grouped by ``(date_range, billed)``. A group is a pair when it
    has at least one Paid > 0 and one Paid < 0. The first positive-paid line
    in scrape order is ``'original'``; every negative-paid line in that group
    is ``'adjustment'``. Unpaired lines stay None.
    """
    rows = list(line_rows or [])
    roles = [None] * len(rows)
    groups = {}
    for idx, entry in enumerate(rows):
        groups.setdefault(adjustment_pair_group_key(entry), []).append(idx)

    for indices in groups.values():
        has_pos = any(_line_paid_number(rows[i]) > 0 for i in indices)
        has_neg = any(_line_paid_number(rows[i]) < 0 for i in indices)
        if not (has_pos and has_neg):
            continue
        original_set = False
        for idx in indices:
            paid = _line_paid_number(rows[idx])
            if paid < 0:
                roles[idx] = 'adjustment'
            elif paid > 0 and not original_set:
                roles[idx] = 'original'
                original_set = True
    return roles


def lines_include_adjustment_pair(line_rows, payer_config=None):
    """True when Healthfirst-style original/adjustment pairs exist."""
    if not payer_detects_adjustment_pairs(payer_config):
        return False
    return any(role is not None for role in classify_adjustment_pair_roles(line_rows))


def _sum_line_amounts(matches):
    """Sum billed/paid across line dicts as ``$X.XX`` strings."""
    total_billed = 0.0
    total_paid = 0.0
    for entry in matches or []:
        total_billed += _line_billed_number(entry)
        total_paid += _line_paid_number(entry)
    return (
        format_currency_amount(total_billed),
        format_currency_amount(total_paid),
    )


def aggregate_line_amounts_for_payer(matches, payer_config=None):
    """Return ``(billed_str, paid_str, is_adjusted)`` for VisitDate matches.

    Without ``detect_adjustment_pairs``, billed and paid are summed (a single
    match keeps its original cell text). With pairing enabled, an
    original/adjustment group contributes billed once and net paid; unpaired
    groups on the same VisitDate still sum per line.
    """
    rows = list(matches or [])
    if not rows:
        return '--', '--', False

    if not payer_detects_adjustment_pairs(payer_config):
        if len(rows) == 1:
            return (
                rows[0].get('billed', '--'),
                rows[0].get('paid', '--'),
                False,
            )
        billed, paid = _sum_line_amounts(rows)
        return billed, paid, False

    groups = {}
    for entry in rows:
        groups.setdefault(adjustment_pair_group_key(entry), []).append(entry)

    total_billed = 0.0
    total_paid = 0.0
    is_adjusted = False
    unpaired = []
    for billed_key, entries in groups.items():
        _date_range, billed_val = billed_key
        paids = [_line_paid_number(entry) for entry in entries]
        if any(paid > 0 for paid in paids) and any(paid < 0 for paid in paids):
            is_adjusted = True
            total_billed += billed_val if billed_val is not None else 0.0
            total_paid += sum(paids)
        else:
            unpaired.extend(entries)

    if unpaired:
        for entry in unpaired:
            total_billed += _line_billed_number(entry)
            total_paid += _line_paid_number(entry)

    if not is_adjusted:
        if len(rows) == 1:
            return (
                rows[0].get('billed', '--'),
                rows[0].get('paid', '--'),
                False,
            )
        billed, paid = _sum_line_amounts(rows)
        return billed, paid, False

    return (
        format_currency_amount(total_billed),
        format_currency_amount(total_paid),
        True,
    )


def first_adjustment_original_line(matches, payer_config=None):
    """Return the first positive-paid original in a pair, else the first match."""
    rows = list(matches or [])
    if not rows:
        return None
    if payer_detects_adjustment_pairs(payer_config):
        for entry, role in zip(rows, classify_adjustment_pair_roles(rows)):
            if role == 'original':
                return entry
    return rows[0]


def denial_entries_preferring_original(matches, payer_config=None):
    """Prefer original-line denial pairs; fall back to the adjustment line.

    Unpaired and original rows are always kept. Adjustment rows are included
    only when that date+billed group has no original denial codes.
    """
    rows = list(matches or [])
    if not payer_detects_adjustment_pairs(payer_config):
        return rows
    roles = classify_adjustment_pair_roles(rows)
    groups_with_original_codes = set()
    selected = []
    for entry, role in zip(rows, roles):
        if role == 'adjustment':
            continue
        selected.append(entry)
        if role == 'original' and entry.get('denial_pairs'):
            groups_with_original_codes.add(adjustment_pair_group_key(entry))
    for entry, role in zip(rows, roles):
        if role != 'adjustment':
            continue
        key = adjustment_pair_group_key(entry)
        if key in groups_with_original_codes:
            continue
        if not entry.get('denial_pairs'):
            continue
        selected.append(entry)
        groups_with_original_codes.add(key)
    return selected


def normalize_results_cell_text(value):
    """Collapse whitespace in a results-table cell for stable fingerprinting."""
    if value is None:
        return ''
    text = str(value).strip()
    if not text or text.lower() == 'nan':
        return ''
    # Multi-line Service Dates cells become a single comparable token.
    return ' '.join(text.split())


def build_results_row_fingerprint(
    claim_number='',
    billed='',
    paid='',
    service_dates='',
    row_text='',
    idx=None,
):
    """Build a stable identity key for one #claimsTable results row.

    Prefer a composite of claim number + amounts (+ service dates) so two
    claims that share InvoiceNumber and Finalized Date still differ when
    claim/TCN ids or billed/paid amounts differ. When amounts are missing,
    append normalized row text so a shared account/claim column alone cannot
    collapse distinct rows. Fall back to row text, then to a positional idx key.
    """
    claim_number = normalize_results_cell_text(claim_number)
    billed = normalize_results_cell_text(billed)
    paid = normalize_results_cell_text(paid)
    service_dates = normalize_results_cell_text(service_dates)
    row_text = normalize_results_cell_text(row_text)

    if claim_number or billed or paid:
        key = f'c:{claim_number}|b:{billed}|p:{paid}|s:{service_dates}'
        # Claim/account columns are sometimes shared across result rows; without
        # amounts, keep row text so same-day duplicates remain distinct.
        if not (billed or paid) and row_text:
            return f'{key}|t:{row_text}'
        return key

    if row_text:
        return f'text:{row_text}'

    if idx is not None:
        return f'idx:{idx}'
    return ''


# ============================================================================
# SECTION 5 — DATA SAFETY HELPERS
# ============================================================================

def format_invoice_number(value, default=''):
    """Normalize InvoiceNumber from Excel (float/int/str) to an integer string.

    Excel often stores invoice numbers as floats (e.g. 12345.0); this converts
    them to '12345' for search matching and display.
    """
    if pd.isna(value):
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return default
        if value == int(value):
            return str(int(value))
        return str(value).strip().rstrip('0').rstrip('.') if '.' in str(value) else str(int(value))

    text = str(value).strip()
    if not text or text.lower() == 'nan':
        return default

    try:
        as_float = float(text.replace(',', ''))
        if as_float == int(as_float):
            return str(int(as_float))
    except ValueError:
        pass

    try:
        return str(int(text.replace(',', '')))
    except ValueError:
        return text


def safe_invoice_number(row_data, key='InvoiceNumber', default=''):
    """Return InvoiceNumber as an integer string suitable for claim search."""
    try:
        return format_invoice_number(row_data[key], default)
    except Exception:
        return default


def safe_alt_patient_id(row_data, key='AltPatientID', default=''):
    """Return AltPatientID without Excel float artifacts (e.g. 12345.0 → 12345)."""
    try:
        return format_invoice_number(row_data[key], default)
    except Exception:
        return default


def normalize_invoice_column(df):
    """Coerce InvoiceNumber and AltPatientID to clean strings after Excel load."""
    if 'InvoiceNumber' in df.columns:
        df['InvoiceNumber'] = df['InvoiceNumber'].apply(
            lambda v: format_invoice_number(v) if pd.notna(v) else v
        )
    if 'AltPatientID' in df.columns:
        df['AltPatientID'] = df['AltPatientID'].apply(
            lambda v: format_invoice_number(v) if pd.notna(v) else v
        )
    return df


def aggregate_invoice_visit_date_range(df, invoice_number):
    """Min–max VisitDate range for all collection rows sharing an invoice.

    Used when bundle claims omit the invoice number on the results table.
    Returns normalized ``MM/DD/YYYY-MM/DD/YYYY`` or None if no valid VisitDate.
    """
    if df is None or not invoice_number or 'InvoiceNumber' not in df.columns:
        return None
    if 'VisitDate' not in df.columns:
        return None

    target_invoice = format_invoice_number(invoice_number)
    if not target_invoice:
        return None

    from availity_app.invoice_index import get_invoice_index

    index = get_invoice_index(df)
    return index.invoice_date_range.get(target_invoice)


def resolve_bundle_search_dates(df, invoice_number):
    """Return (start_date, end_date) for claim search from invoice VisitDate bundle.

    Aggregates min–max VisitDate across all spreadsheet rows sharing the invoice.
    Returns ('', '') when no valid bundle range exists.
    """
    target_range = aggregate_invoice_visit_date_range(df, invoice_number)
    if not target_range:
        return '', ''
    parsed = parse_service_date_range(target_range)
    if not parsed:
        return '', ''
    start, _, end = parsed.partition('-')
    start = start.strip()
    end = end.strip()
    if not start or not end:
        return '', ''
    return start, end


def resolve_invoice_search_dates(df, row_data, safe_field_fn=None, payer_config=None):
    """Return (start_date, end_date) for claim search from VisitDate aggregation.

    When ``group_search_by_patient`` is on, prefers min–max VisitDate across
    the patient's 90-day cluster. Otherwise prefers min–max VisitDate across
    all spreadsheet rows sharing the row's InvoiceNumber. Falls back to row
    StartDate/EndDate/VisitDate when aggregation cannot produce a range.

    Returns ``(start, end, from_aggregation)`` where ``from_aggregation`` is
    True when dates came from patient-cluster or invoice VisitDate aggregation.
    """
    if safe_field_fn is None:
        safe_field_fn = safe_field
    from availity_app.search_grouping import (
        resolve_patient_cluster_search_dates,
        supports_patient_search_grouping,
    )
    if supports_patient_search_grouping(payer_config):
        start, end = resolve_patient_cluster_search_dates(
            df, row_data, safe_field_fn,
        )
        if start and end:
            return start, end, True
    invoice = safe_invoice_number(row_data, 'InvoiceNumber')
    if df is not None and invoice:
        start, end = resolve_bundle_search_dates(df, invoice)
        if start and end:
            return start, end, True
    start, end = resolve_service_search_dates(row_data, safe_field_fn)
    return start, end, False


def normalize_name_part(name_str):
    """Drop single-character tokens (e.g. middle initials) from a name part."""
    text = str(name_str).strip()
    if not text:
        return ''
    tokens = text.split()
    kept = [t for t in tokens if len(t.rstrip('.')) > 1]
    if not kept:
        return text
    return ' '.join(kept)


def normalize_patient_names(last_name, first_name):
    """Normalize last/first names for Availity claim search form fill."""
    return normalize_name_part(last_name), normalize_name_part(first_name)


def safe_field(row_data, key, default=''):
    """Return a stripped string for a pandas row field, converting NaN to default.

    Without this, missing Excel values become the literal string 'nan' which
    would be typed verbatim into Availity form fields and produce bad results.
    """
    try:
        val = scalar_cell(row_data[key])
        if val is None:
            return default
        return str(val).strip()
    except Exception:
        return default


def validate_dataframe(df, payer_config=None):
    """Return required column names that are absent in df.

    Always checks REQUIRED_COLUMNS. When the selected payer matches results
    by VisitDate + Amount (Aetna), ``Amount`` is also required. Payers can
    demand further columns via ``required_extra_columns`` (SWHNY needs Amount
    and Units to match service lines).

    An empty list means the dataframe is valid.  A non-empty list means
    the batch should be aborted with a clear error before any rows are processed.
    """
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if (
        payer_matches_results_by_visit_and_amount(payer_config)
        and 'Amount' not in df.columns
    ):
        missing.append('Amount')
    for col in (payer_config or {}).get('required_extra_columns') or ():
        if col not in df.columns and col not in missing:
            missing.append(col)
    return missing


def empty_claim_result(status='Not found', payer_config=None):
    """Return a dict with all output columns set to '--' except Claim Status.

    Used as a placeholder when a row cannot be processed, ensuring the
    output CSV always has a complete and consistent set of columns.  Any
    payer-declared extra columns are seeded too.
    """
    result = {
        'Claim ID':       '--',
        'Billed Amount':  '--',
        'Paid Amount':    '--',
        'Claim Status':   status,
        'Denial Reason':  '--',
        'Finalized Date': '--',
        'Check Number':   '--',
        'Check Date':     '--',
    }
    for col in resolved_output_columns(payer_config):
        if col not in result and col not in ('Last Action Taken', 'Recheck'):
            result[col] = '--'
    return result


# ============================================================================
# INVOICE-ALIGNMENT LINE MATCHING (pool tiers)
# ============================================================================
# Used by payers whose line_level config sets ``match_by_invoice_alignment``
# (SWHNY).  Instead of matching a portal service line to a spreadsheet row by
# VisitDate, every row on an invoice is matched at once against every portal
# line using (Units, Amount) signatures.  Payers without the flag never reach
# this code.

def get_sorted_invoice_rows(df, invoice_number):
    """Return ``(df_index, row)`` pairs for an invoice, sorted by VisitDate ascending.

    Ties keep original dataframe order (stable sort).  Rows without a parseable
    VisitDate are appended after dated rows, still in original order.
    """
    indices = invoice_row_indices(df, invoice_number)
    if not indices:
        return []

    dated = []
    undated = []
    for pos, i in enumerate(indices):
        row = df.iloc[i]
        normalized = normalize_search_date(safe_field(row, 'VisitDate'))
        if not normalized:
            undated.append((pos, i, row))
            continue
        try:
            dt = pd.to_datetime(normalized, format='%m/%d/%Y')
            if pd.isna(dt):
                undated.append((pos, i, row))
                continue
            dated.append((dt, pos, i, row))
        except Exception:
            undated.append((pos, i, row))

    dated.sort(key=lambda item: (item[0], item[1]))
    result = [(i, row) for _, _, i, row in dated]
    result.extend((i, row) for _, i, row in undated)
    return result


def parse_quantity(value):
    """Parse a Units/Quantity cell to float, or None if blank/unusable."""
    val = scalar_cell(value, default=None)
    if val is None:
        return None
    text = str(val).strip().replace(',', '')
    if not text or text.lower() in ('nan', '--', '-', 'n/a'):
        return None
    try:
        return float(text)
    except Exception:
        return None


def parse_service_dates_panel_text(text):
    """Parse claim-header Service Dates panel text into ``(start, end)``.

    Accepts forms like ``06/22/2026 - 06/26/2026`` or ``06/22/2026-06/26/2026``.
    Returns ``(None, None)`` when unparseable.
    """
    parsed = parse_service_date_range(text)
    if not parsed:
        return None, None
    start, _, end = parsed.partition('-')
    start = start.strip()
    end = end.strip()
    if not start or not end:
        return None, None
    return start, end


def invoice_service_dates_aligned(sorted_inputs, range_start, range_end):
    """True when panel start/end equal first/last sorted invoice VisitDates."""
    if not sorted_inputs or not range_start or not range_end:
        return False
    first_visit = normalize_search_date(safe_field(sorted_inputs[0][1], 'VisitDate'))
    last_visit = normalize_search_date(safe_field(sorted_inputs[-1][1], 'VisitDate'))
    start = normalize_date(range_start)
    end = normalize_date(range_end)
    return bool(first_visit and last_visit and first_visit == start and last_visit == end)


def _panel_range_day_strings(range_start, range_end):
    """Return inclusive MM/DD/YYYY day strings for a panel range, or None."""
    start = normalize_date(range_start) if range_start else None
    end = normalize_date(range_end) if range_end else None
    if not start or not end:
        return None
    try:
        start_dt = pd.to_datetime(start, format='%m/%d/%Y').to_pydatetime()
        end_dt = pd.to_datetime(end, format='%m/%d/%Y').to_pydatetime()
    except Exception:
        return None
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    days = []
    cur = start_dt
    while cur <= end_dt:
        days.append(cur.strftime('%m/%d/%Y'))
        cur += pd.Timedelta(days=1)
    return days


def invoice_visit_dates_cover_panel_range(sorted_inputs, range_start, range_end):
    """True when every calendar day in the Service Dates panel has a VisitDate.

    Returns ``False`` when panel bounds are missing/unparseable or any panel
    day is absent from the aggregated invoice VisitDates.
    """
    panel_days = _panel_range_day_strings(range_start, range_end)
    if not panel_days:
        return False
    visit_days = set()
    for _df_idx, row in sorted_inputs or []:
        visit = normalize_search_date(safe_field(row, 'VisitDate'))
        if visit:
            visit_days.add(visit)
    return all(day in visit_days for day in panel_days)


def _input_amount_units(row):
    """Return ``(amount_float_or_None, units_float_or_None)`` from an input row."""
    amount = parse_amount(safe_field(row, 'Amount'))
    units = parse_quantity(safe_field(row, 'Units'))
    return amount, units


def _line_status_is_denied(line):
    """True when a portal line Status indicates Denied."""
    return 'denied' in str((line or {}).get('status') or '').strip().lower()


def _input_pay_status_is_open(row):
    """True when spreadsheet Pay Status is Open (case-insensitive)."""
    for key in ('Pay. Status', 'Pay Status', 'Payment Status'):
        raw = safe_field(row, key, '')
        if raw and str(raw).strip().lower() == 'open':
            return True
    return False


def _open_denied_panel_gate(sorted_inputs, panel_range_start, panel_range_end):
    """True when panel is parseable and VisitDates miss at least one panel day."""
    panel_days = _panel_range_day_strings(panel_range_start, panel_range_end)
    if not panel_days:
        return False
    return not invoice_visit_dates_cover_panel_range(
        sorted_inputs, panel_range_start, panel_range_end,
    )


def _build_open_denied_match_entry(line, row):
    """Build a match-map entry for an Open input paired to a Denied portal line."""
    amount, units = _input_amount_units(row)
    line_qty = parse_quantity(line.get('quantity'))
    units_mismatch = (
        units is not None
        and line_qty is not None
        and abs(units - line_qty) >= 0.005
    )
    rate_mismatch = rate_check(
        amount, units, line.get('billed'), line.get('quantity'),
    )
    entry = _build_matched_line_entry(
        line, row,
        confidence=MATCH_CONFIDENCE_OPEN_DENIED,
        units_mismatch=units_mismatch,
        rate_mismatch=rate_mismatch,
    )
    entry['open_denied'] = True
    return entry


def _match_open_to_denied_direct(
    sorted_inputs, lines, panel_range_start, panel_range_end,
):
    """Match exactly one Open input to exactly one Denied line (full lines list).

    Used when ``n_lines > n_in`` before excess-line filtering would drop the
    Denied row.  Returns a one-entry map when ``n_in == 1``, else None.
    """
    if not _open_denied_panel_gate(sorted_inputs, panel_range_start, panel_range_end):
        return None

    open_inputs = [
        (i, df_idx, row)
        for i, (df_idx, row) in enumerate(sorted_inputs)
        if _input_pay_status_is_open(row)
    ]
    denied_lines = [
        (line_i, line)
        for line_i, line in enumerate(lines)
        if _line_status_is_denied(line)
    ]
    if len(open_inputs) != 1 or len(denied_lines) != 1 or len(sorted_inputs) != 1:
        return None

    _in_i, df_idx, row = open_inputs[0]
    line_i, line = denied_lines[0]
    log_to_gui(
        f"    ✓ Open Pay. Status row {df_idx} → Denied portal line "
        f"#{line_i + 1} (OPEN_DENIED_FALLBACK)\n",
        'success',
    )
    return {df_idx: _build_open_denied_match_entry(line, row)}


def _is_multi_day_span(date_range):
    """True when a normalised date range spans more than one calendar day."""
    bounds = _line_range_bounds(normalize_date_range(date_range or ''))
    if not bounds:
        return False
    return bounds[0] != bounds[1]


def _shared_bundle_span(line_rows):
    """Return the shared multi-day span when every line shows the same range."""
    if not line_rows:
        return None
    spans = []
    for ln in line_rows:
        dr = normalize_date_range(ln.get('date_range') or '')
        if not dr:
            return None
        spans.append(dr)
    if len(set(spans)) != 1:
        return None
    span = spans[0]
    if not _is_multi_day_span(span):
        return None
    return span


def _assign_line_by_bundle_day(input_row, line_rows, used_indices, sig=None):
    """Pick the portal line at the VisitDate day offset within a shared bundle span.

    SWHNY often repeats the claim-level service span on every line row; the
    correct line is the table row whose index equals the day offset from the
    span start to the spreadsheet VisitDate.
    """
    span = _shared_bundle_span(line_rows)
    if span is None:
        return None
    offset = day_offset_in_range(safe_field(input_row, 'VisitDate'), span)
    if offset is None or offset < 0 or offset >= len(line_rows):
        return None
    if offset in used_indices:
        return None
    line = line_rows[offset]
    if sig is not None:
        amount, units = _input_amount_units(input_row)
        if line_signature(units, amount) != sig:
            return None
    return offset, line


def _present_line_for_match(line, input_row):
    """Return line dict with ``date_range`` set to the input VisitDate when bundled."""
    if not line:
        return line
    visit_norm = normalize_date_range(
        coerce_visit_date(safe_field(input_row, 'VisitDate')),
    )
    if not visit_norm:
        return line
    if visit_date_matches_line(visit_norm, line.get('date_range'), 'exact'):
        return line
    span = _shared_bundle_span([line]) or normalize_date_range(line.get('date_range') or '')
    if span and visit_date_matches_line(visit_norm, span, 'within_range'):
        presented = dict(line)
        presented['date_range'] = visit_norm
        return presented
    return line


def _build_matched_line_entry(line, input_row, *, confidence, units_mismatch=False,
                              rate_mismatch=False, split_bill=False, extra=None):
    """Standard match-map entry with optional bundle VisitDate presentation."""
    entry = {
        'line': _present_line_for_match(line, input_row),
        'unresolved': False,
        'units_mismatch': units_mismatch,
        'split_bill': split_bill,
        'confidence': build_match_confidence(
            confidence,
            rate_mismatch=rate_mismatch,
            units_mismatch=units_mismatch,
        ),
    }
    if extra:
        entry.update(extra)
    return entry


# How a spreadsheet row was mapped to a portal service line.  Internal labels
# stay technical for code/tests; ``build_match_confidence`` writes only
# ``MATCH_FLAG_MANUAL_REVIEW`` (or blank) to the Excel column.
MATCH_CONFIDENCE_HIGH = 'HIGH'
MATCH_CONFIDENCE_LOW_ORDER = 'LOW_ORDER_BASED'
MATCH_CONFIDENCE_POSITIONAL = 'POSITIONAL'
MATCH_CONFIDENCE_AMOUNT_UNITS = 'AMOUNT_UNITS'
MATCH_CONFIDENCE_SPLIT_BILL = 'SPLIT_BILL'
MATCH_CONFIDENCE_SPLIT_VISIT = 'SPLIT_VISIT'
MATCH_CONFIDENCE_MERGED_LINE = 'MERGED_LINE'
MATCH_CONFIDENCE_DATA_MISMATCH = 'DATA_MISMATCH'
MATCH_CONFIDENCE_OPEN_DENIED = 'OPEN_DENIED_FALLBACK'
MATCH_CONFIDENCE_REVIEW = 'UNMATCHED_REVIEW'

# Sole auditor-facing Match Confidence value (critical cases only).
MATCH_FLAG_MANUAL_REVIEW = NEED_MANUAL_REVIEW_STATUS

# Internal checksum names (not written to Excel; see build_match_confidence).
MATCH_FLAG_RATE_MISMATCH = 'RATE_MISMATCH'
MATCH_FLAG_UNITS_MISMATCH = 'UNITS_MISMATCH'

_DANGEROUS_CONFIDENCE_LABELS = frozenset({
    MATCH_CONFIDENCE_LOW_ORDER,
    MATCH_CONFIDENCE_REVIEW,
    MATCH_CONFIDENCE_DATA_MISMATCH,
})

# Per-unit rate difference tolerated before a pair is flagged.
_RATE_EQ_TOL = 0.005

# Integer-cents tolerances for pool-tier matching decisions.
TOL_CENTS = 1
TOL_CENTS_SUM = 2
TOL_CENTS_HEADER = 3
_UNITS_EQ_TOL = 0.01


def to_cents(value):
    """Convert a currency value to integer cents, or None if unparseable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Already cents? Treat bare ints as dollars for consistency with floats.
        return int(round(value * 100))
    if isinstance(value, float):
        return int(round(value * 100))
    amount = parse_amount(value)
    if amount is None:
        return None
    return int(round(amount * 100))


def money_eq(a_cents, b_cents, tol_cents=TOL_CENTS):
    """True when both cent values are present and differ by at most ``tol_cents``."""
    if a_cents is None or b_cents is None:
        return False
    return abs(a_cents - b_cents) <= tol_cents


def units_eq(a, b, tol=_UNITS_EQ_TOL):
    """True when both quantities are present and differ by at most ``tol``."""
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def cents_to_dollars(cents):
    """Format integer cents as a float dollar amount for display."""
    if cents is None:
        return None
    return cents / 100.0


def _remit_join_signature(row, *, with_paid=True):
    """Hashable billed/paid/qty key for remittance-to-claim-line join."""
    billed_c = to_cents(row.get('billed'))
    paid_c = to_cents(row.get('paid')) if with_paid else None
    qty = parse_quantity(row.get('quantity'))
    return (billed_c, paid_c, qty)


def _remit_join_has_key(row):
    """True when a remittance/claim row has billed or quantity to join on."""
    billed_c, _paid_c, qty = _remit_join_signature(row)
    return billed_c is not None or qty is not None


def _usable_join_denial(value):
    """True when a remittance denial string is worth attaching to a line."""
    text = str(value or '').strip()
    if text in ('', '--', 'N/A', 'None'):
        return False
    return text.casefold() != 'remittance not found'


def join_remit_lines_to_claim_lines(claim_lines, remit_lines):
    """Attach remittance denials onto claim-detail lines (consume-once).

    Matching order:
      1. (billed cents, paid cents, quantity)
      2. (billed cents, quantity) when paid differs
      3. positional only when counts match and every signature is unique

    Returns ``(updated_lines, claim_level_denial, unresolved)``.
    ``claim_level_denial`` is the first usable CARC that did not join to a
    line (or the first usable row when remittance has no amount columns).
    ``unresolved`` is True when leftover remits and unmatched claims cannot
    be paired without guessing.
    """
    lines = [dict(entry or {}) for entry in (claim_lines or [])]
    remits = [dict(entry or {}) for entry in (remit_lines or [])]
    if not remits:
        return lines, None, False

    keyed = [row for row in remits if _remit_join_has_key(row)]
    unkeyed = [row for row in remits if not _remit_join_has_key(row)]

    def _first_denial(rows):
        for row in rows or []:
            denial = row.get('denial')
            if _usable_join_denial(denial):
                return denial
        return None

    if not keyed:
        return lines, _first_denial(remits), False

    used_ids = set()
    unresolved = False

    def _attach(line, pick):
        line['remittance_denial'] = pick.get('denial') or None
        if pick.get('date_range'):
            line['remit_date_range'] = pick.get('date_range')
        used_ids.add(id(pick))

    def _consume(key_fn):
        """Attach only 1:1 keys. Duplicate claim or remit signatures stay unused."""
        remits_by_key = defaultdict(list)
        for row in keyed:
            if id(row) in used_ids:
                continue
            remits_by_key[key_fn(row)].append(row)
        claims_by_key = defaultdict(list)
        for line in lines:
            if _usable_join_denial(line.get('remittance_denial')):
                continue
            key = key_fn(line)
            billed_c, _paid_c, qty = key
            if billed_c is None and qty is None:
                continue
            claims_by_key[key].append(line)
        for key, claim_group in claims_by_key.items():
            remit_group = remits_by_key.get(key) or []
            if len(claim_group) == 1 and len(remit_group) == 1:
                _attach(claim_group[0], remit_group[0])

    _consume(lambda row: _remit_join_signature(row, with_paid=True))
    _consume(lambda row: (
        to_cents(row.get('billed')), None, parse_quantity(row.get('quantity')),
    ))

    leftover = [row for row in keyed if id(row) not in used_ids]
    unmatched = [
        line for line in lines
        if not _usable_join_denial(line.get('remittance_denial'))
    ]

    if (
        not unresolved
        and leftover
        and unmatched
        and len(lines) == len(keyed)
    ):
        claim_sigs = [_remit_join_signature(line) for line in lines]
        remit_sigs = [_remit_join_signature(row) for row in keyed]
        unique_ok = (
            len(set(claim_sigs)) == len(claim_sigs)
            and len(set(remit_sigs)) == len(remit_sigs)
        )
        if unique_ok:
            for line, row in zip(lines, keyed):
                if id(row) in used_ids:
                    continue
                if _usable_join_denial(line.get('remittance_denial')):
                    continue
                line['remittance_denial'] = row.get('denial') or None
                if row.get('date_range'):
                    line['remit_date_range'] = row.get('date_range')
                used_ids.add(id(row))
            leftover = [row for row in keyed if id(row) not in used_ids]
            unmatched = [
                line for line in lines
                if not _usable_join_denial(line.get('remittance_denial'))
            ]
        else:
            unresolved = True
    elif leftover and unmatched:
        unresolved = True

    claim_level = _first_denial(leftover + unkeyed)
    return lines, claim_level, unresolved


def line_signature(units, amount):
    """Return a hashable ``(units, amount)`` fingerprint, or None if unusable.

    Both parts are scaled to integers (hundredths) so ``$1,169.92`` and
    ``1169.9200001`` collapse to the same key and float noise cannot split a
    duplicate group.  Returns None when either side does not parse, which makes
    the caller skip signature matching rather than guess.
    """
    units_val = parse_quantity(units)
    amount_c = to_cents(amount)
    if units_val is None or amount_c is None:
        return None
    return (int(round(units_val * 100)), amount_c)


def rate_check(input_amount, input_units, line_billed, line_qty):
    """True when the input and portal per-unit rates disagree.

    Amount / Units should equal the contract rate on both sides of a correct
    match.  A disagreement means the pair is wrong or the line is bundled, so
    the match is flagged for review.  Returns False when either rate cannot be
    computed (missing value or zero units) — absence of evidence, not a fault.
    """
    billed_val = parse_amount(line_billed)
    qty_val = parse_quantity(line_qty)
    if input_amount is None or not input_units:
        return False
    if billed_val is None or not qty_val:
        return False
    return abs(input_amount / input_units - billed_val / qty_val) > _RATE_EQ_TOL


def build_match_confidence(base_label, *, rate_mismatch=False,
                           units_mismatch=False):
    """Return the Match Confidence string written to Excel.

    Critical cases only → ``Need Manual Review``.  Safe mappings leave the
    column blank (no HIGH / POSITIONAL / SPLIT_* labels).

    Critical:
      - unproven order assignment (LOW_ORDER_BASED)
      - no usable match (UNMATCHED_REVIEW)
      - input Amount/Paid/Adj/Balance math failure (DATA_MISMATCH)
      - per-unit rate disagreement (RATE_MISMATCH)

    ``units_mismatch`` remains an internal entry boolean; alone it does not
    rewrite the column.
    """
    _ = units_mismatch  # internal signal only; not auditor-facing
    if rate_mismatch or base_label in _DANGEROUS_CONFIDENCE_LABELS:
        return MATCH_FLAG_MANUAL_REVIEW
    return ''


def humanize_match_confidence(label):
    """Map a stored/internal confidence string to the auditor-facing form.

    Critical → ``Need Manual Review`` (notes kept).  Everything else → blank.
    """
    if not label or label in ('--', '-', ''):
        return ''
    if label == MATCH_FLAG_MANUAL_REVIEW or label.startswith(
            f"{MATCH_FLAG_MANUAL_REVIEW};"):
        return label
    parts = [p.strip() for p in str(label).split(';') if p.strip()]
    base = parts[0] if parts else ''
    needs_review = (
        base in _DANGEROUS_CONFIDENCE_LABELS
        or MATCH_FLAG_RATE_MISMATCH in parts
        or MATCH_FLAG_MANUAL_REVIEW in parts
    )
    if not needs_review:
        return ''
    notes = [
        p for p in parts[1:]
        if p and p not in (
            MATCH_FLAG_RATE_MISMATCH,
            MATCH_FLAG_UNITS_MISMATCH,
            MATCH_FLAG_MANUAL_REVIEW,
        )
        and p not in _DANGEROUS_CONFIDENCE_LABELS
    ]
    if notes:
        return f"{MATCH_FLAG_MANUAL_REVIEW}; {'; '.join(notes)}"
    return MATCH_FLAG_MANUAL_REVIEW


def score_line_against_input(input_amount, input_units, line_billed, line_qty):
    """Score how well a portal line matches an input Amount/Units pair.

    Higher is better.  Exact Amount↔Billed is primary; Units↔Quantity is the
    secondary signal.  Returns a tuple suitable for ``max`` comparison:
    ``(amount_match, units_match, -abs_amount_diff, -abs_units_diff)``.
    """
    billed_val = parse_amount(line_billed)
    qty_val = parse_quantity(line_qty)

    amount_match = 0
    amount_diff = float('inf')
    if input_amount is not None and billed_val is not None:
        amount_diff = abs(input_amount - billed_val)
        if amount_diff < 0.005:
            amount_match = 2
        elif amount_diff < 0.05:
            amount_match = 1

    units_match = 0
    units_diff = float('inf')
    if input_units is not None and qty_val is not None:
        units_diff = abs(input_units - qty_val)
        if units_diff < 0.005:
            units_match = 2
        elif units_diff < 0.05:
            units_match = 1

    # Prefer exact amount, then exact units, then closer numeric diffs.
    return (
        amount_match,
        units_match,
        -amount_diff if amount_diff != float('inf') else float('-inf'),
        -units_diff if units_diff != float('inf') else float('-inf'),
    )


def _best_unused_line_index(input_row, line_rows, used_line_indices):
    """Return index into ``line_rows`` of the best unused line for ``input_row``."""
    bundle_pick = _assign_line_by_bundle_day(
        input_row, line_rows, used_line_indices,
    )
    if bundle_pick is not None:
        idx, line = bundle_pick
        amount, units = _input_amount_units(input_row)
        score = score_line_against_input(
            amount, units, line.get('billed'), line.get('quantity'),
        )
        return idx, score

    amount, units = _input_amount_units(input_row)
    best_idx = None
    best_score = None
    for i, line in enumerate(line_rows):
        if i in used_line_indices:
            continue
        score = score_line_against_input(
            amount, units, line.get('billed'), line.get('quantity'),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_idx = i
    return best_idx, best_score


def _best_unused_input_index(line, sorted_inputs, used_input_indices):
    """Return index into ``sorted_inputs`` of the best unused input for ``line``."""
    best_idx = None
    best_score = None
    for i, (_df_idx, row) in enumerate(sorted_inputs):
        if i in used_input_indices:
            continue
        amount, units = _input_amount_units(row)
        score = score_line_against_input(
            amount, units, line.get('billed'), line.get('quantity'),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_idx = i
    return best_idx, best_score


def filter_excess_portal_lines(sorted_inputs, line_rows):
    """Keep one portal line per input via Amount↔Billed then Units↔Quantity.

    Used when ``len(line_rows) > len(sorted_inputs)``.  Returns selected lines
    in portal-table order (original scrape order among kept rows).
    """
    if not sorted_inputs:
        return []
    if len(line_rows) <= len(sorted_inputs):
        return list(line_rows)

    used = set()
    selected_indices = []
    for _df_idx, row in sorted_inputs:
        best_idx, best_score = _best_unused_line_index(row, line_rows, used)
        if best_idx is None:
            continue
        # Require at least one meaningful match signal when Amount/Units exist.
        amount, units = _input_amount_units(row)
        if amount is None and units is None:
            # No criteria available — keep next unused line in order.
            for i in range(len(line_rows)):
                if i not in used:
                    best_idx = i
                    break
        elif best_score is not None and best_score[0] == 0 and best_score[1] == 0:
            # Neither amount nor units matched; still take best remaining guess
            # so counts can align, caller may mark verification warnings.
            pass
        used.add(best_idx)
        selected_indices.append(best_idx)

    selected_indices.sort()
    return [line_rows[i] for i in selected_indices]


_AMOUNT_EQ_TOL = 0.02
_QTY_EQ_TOL = 0.005


def _amounts_close(a, b, tol=_AMOUNT_EQ_TOL):
    """True when both amounts parse and differ by less than ``tol``."""
    if a is None or b is None:
        return False
    return abs(a - b) < tol


def _qty_close(a, b, tol=_QTY_EQ_TOL):
    """True when both quantities parse and differ by less than ``tol``."""
    if a is None or b is None:
        return False
    return abs(a - b) < tol


def _format_match_currency(value):
    """Format a numeric amount as ``$X.XX``."""
    return f"${value:.2f}"


def _sum_line_field_amounts(line_rows, key):
    """Sum parse_amount over ``key`` on each line (blank/``--`` as 0)."""
    total = 0.0
    for entry in line_rows:
        val = parse_amount(entry.get(key))
        total += val if val is not None else 0.0
    return total


def _sum_line_quantities(line_rows):
    """Sum parse_quantity over each line (blank as 0)."""
    total = 0.0
    for entry in line_rows:
        val = parse_quantity(entry.get('quantity'))
        total += val if val is not None else 0.0
    return total


def _split_bill_forced_status(sum_paid, header_billed):
    """Return ``(forced_status, needs_claim_comment)`` from summed paid vs header Billed.

    - header Billed < 0 → Adjusted + comment
    - sum paid ≈ 0 → Denied + comment
    - sum paid ≈ header Billed → Paid, no comment
    - 0 < sum paid < header Billed → Partially Paid + comment
    - sum paid > header Billed → Paid (Overpayment), no comment
    """
    if sum_paid is None:
        sum_paid = 0.0
    if header_billed is not None and header_billed < 0:
        return ADJUSTED_STATUS, True
    if abs(sum_paid) < _QTY_EQ_TOL:
        return 'DENIED', True
    if header_billed is not None and _amounts_close(sum_paid, header_billed):
        return 'PAID', False
    if header_billed is not None and sum_paid < header_billed:
        return 'PARTIALLY PAID', True
    if header_billed is not None and sum_paid > header_billed:
        return PAID_OVERPAYMENT_STATUS, False
    return 'PAID', False


def _match_single_input_multi_line(
    sorted_inputs, line_rows, header_billed, header_paid,
):
    """Exception path: 1 input row, multiple portal lines.

    1. Exact single line: billed ≈ header Billed AND qty ≈ input Units
    2. Else split-bill: sum billed ≈ header Billed AND sum qty ≈ input Units

    Returns a one-entry match map, or ``None`` to fall back to filter_excess.
    """
    if len(sorted_inputs) != 1 or len(line_rows) <= 1:
        return None

    df_idx, row = sorted_inputs[0]
    _amount, units = _input_amount_units(row)

    bundle_pick = _assign_line_by_bundle_day(row, line_rows, set())
    if bundle_pick is not None:
        line_idx, line = bundle_pick
        log_to_gui(
            f"    ✓ Bundle day-position (sole input): VisitDate "
            f"{safe_field(row, 'VisitDate')} → line #{line_idx + 1}\n",
            "success",
        )
        return {
            df_idx: _build_matched_line_entry(
                line, row, confidence=MATCH_CONFIDENCE_POSITIONAL,
            ),
        }

    # Step 1 — exact single-line match to header billed + input Units.
    for line in line_rows:
        line_billed = parse_amount(line.get('billed'))
        line_qty = parse_quantity(line.get('quantity'))
        if _amounts_close(line_billed, header_billed) and _qty_close(line_qty, units):
            log_to_gui(
                f"    ✓ Exact line match for sole input: "
                f"billed={line.get('billed')} qty={line.get('quantity')} "
                f"(header billed / input Units)\n",
                "success",
            )
            return {
                df_idx: {
                    'line': line,
                    'unresolved': False,
                    'units_mismatch': False,
                    'split_bill': False,
                    'confidence': build_match_confidence(MATCH_CONFIDENCE_HIGH),
                }
            }

    # Step 2 — split bill: sums of billed/qty match header billed + Units.
    sum_billed = _sum_line_field_amounts(line_rows, 'billed')
    sum_paid = _sum_line_field_amounts(line_rows, 'paid')
    sum_qty = _sum_line_quantities(line_rows)
    if not (
        _amounts_close(sum_billed, header_billed) and _qty_close(sum_qty, units)
    ):
        return None

    # Prefer header billed for status; fall back to summed line billed.
    billed_for_status = header_billed if header_billed is not None else sum_billed
    forced_status, needs_comment = _split_bill_forced_status(
        sum_paid, billed_for_status,
    )
    billed_str = _format_match_currency(sum_billed)
    paid_str = _format_match_currency(sum_paid)
    first = line_rows[0]
    synthetic = {
        'row': first.get('row'),
        'idx': first.get('idx', 0),
        'billed': billed_str,
        'paid': paid_str,
        'date_range': first.get('date_range'),
        'quantity': str(sum_qty),
        'status': forced_status,
    }
    log_to_gui(
        f"    ✓ Split-bill aggregate: {len(line_rows)} lines → "
        f"Billed {billed_str}, Paid {paid_str}, Qty {sum_qty:g} "
        f"(status: {forced_status})\n",
        "success",
    )
    return {
        df_idx: {
            'line': synthetic,
            'unresolved': False,
            'units_mismatch': False,
            'split_bill': True,
            'billed': billed_str,
            'paid': paid_str,
            'forced_status': forced_status,
            'needs_claim_comment': needs_comment,
            'confidence': build_match_confidence(MATCH_CONFIDENCE_SPLIT_BILL),
        }
    }


def _normalized_line_status(line):
    """Lowercased line status used for in-group status-diversity checks."""
    return (line.get('status') or '').strip().lower()


def _describe_signature(sig):
    """Render a signature key back as ``Units N, Billed $X.XX`` for logs."""
    units, amount = sig[0] / 100.0, sig[1] / 100.0
    return f"Units {units:g}, Billed {_format_match_currency(amount)}"


def match_by_signature_groups(sorted_inputs, line_rows):
    """Tier 1: map invoice rows to portal lines by ``(Units, Amount)`` signature.

    Both sides are grouped by signature and the groups are matched to each
    other, which is deterministic.  Within a group, assignment is by order:
    inputs are already VisitDate-ascending and portal lines keep table order,
    which payers adjudicate chronologically from the 837 claim.

    Because signature equality is exact, matched pairs always agree on Units
    and per-unit rate, so no checksum flag can fire here.  When every line in a
    duplicate group shares one status the within-group order does not change
    any outcome (HIGH); when statuses differ the order is a best guess and the
    row is flagged ``LOW_ORDER_BASED`` for manual audit.

    Returns a match map, or None when the two sides are not an exact signature
    bijection (or any value is unparseable) so the caller falls through to the
    count-based paths.
    """
    if not sorted_inputs or not line_rows:
        return None

    input_groups = defaultdict(list)
    for df_idx, row in sorted_inputs:
        amount, units = _input_amount_units(row)
        sig = line_signature(units, amount)
        if sig is None:
            return None
        input_groups[sig].append((df_idx, row))

    line_groups = defaultdict(list)
    for line in line_rows:
        sig = line_signature(line.get('quantity'), line.get('billed'))
        if sig is None:
            return None
        line_groups[sig].append(line)

    if set(input_groups) != set(line_groups):
        return None
    if any(len(input_groups[s]) != len(line_groups[s]) for s in input_groups):
        return None

    result = {}
    low_confidence = 0
    used_line_indices = set()
    bundle_span = _shared_bundle_span(line_rows)
    for sig, inputs in input_groups.items():
        lines = line_groups[sig]
        statuses = {_normalized_line_status(line) for line in lines}
        ambiguous = len(inputs) > 1 and len(statuses) > 1
        if ambiguous:
            low_confidence += len(inputs)
            log_to_gui(
                f"    ⚠️ {len(inputs)} row(s) share signature "
                f"({_describe_signature(sig)}) but their lines have differing "
                f"statuses — assigned by VisitDate order (LOW confidence)\n",
                "error",
            )
        confidence = (
            MATCH_CONFIDENCE_LOW_ORDER if ambiguous else MATCH_CONFIDENCE_HIGH
        )
        line_iter = iter(lines)
        for df_idx, row in inputs:
            pick = _assign_line_by_bundle_day(
                row, line_rows, used_line_indices, sig=sig,
            )
            if pick is not None:
                line_idx, line = pick
                used_line_indices.add(line_idx)
                if bundle_span:
                    log_to_gui(
                        f"    ✓ Bundle day-position: VisitDate "
                        f"{safe_field(row, 'VisitDate')} → line #{line_idx + 1}\n",
                        "success",
                    )
            else:
                line = next(line_iter, None)
                if line is None:
                    return None
                line_idx = line.get('idx')
                if line_idx is not None:
                    used_line_indices.add(line_idx)
            result[df_idx] = _build_matched_line_entry(
                line, row, confidence=confidence,
            )

    log_to_gui(
        f"    ✓ Signature-group match: {len(sorted_inputs)} input row(s) → "
        f"{len(line_rows)} line(s) across {len(input_groups)} signature "
        f"group(s){f'; {low_confidence} low-confidence' if low_confidence else ''}\n",
        "success",
    )
    return result


def _apply_open_denied_fallback(
    sorted_inputs, lines, result, line_to_input,
    panel_range_start, panel_range_end,
):
    """Match one Open leftover input to one unused Denied portal line.

    Gates (all required):
      - panel bounds parseable and VisitDates do not cover every panel day
      - exactly one unresolved input with Pay. Status == Open
      - exactly one portal line not in ``line_to_input`` whose Status is Denied

    Mutates ``result`` / ``line_to_input`` on success. Returns True when applied.
    """
    if not _open_denied_panel_gate(sorted_inputs, panel_range_start, panel_range_end):
        return False

    open_leftovers = []
    for i, (df_idx, row) in enumerate(sorted_inputs):
        entry = result.get(df_idx) or {}
        if not entry.get('unresolved'):
            continue
        if _input_pay_status_is_open(row):
            open_leftovers.append((i, df_idx, row))
    if len(open_leftovers) != 1:
        return False

    unused_denied = [
        (line_i, line)
        for line_i, line in enumerate(lines)
        if line_i not in line_to_input and _line_status_is_denied(line)
    ]
    if len(unused_denied) != 1:
        return False

    _in_i, df_idx, row = open_leftovers[0]
    line_i, line = unused_denied[0]
    result[df_idx] = _build_open_denied_match_entry(line, row)
    line_to_input[line_i] = _in_i
    log_to_gui(
        f"    ✓ Open Pay. Status row {df_idx} → Denied portal line "
        f"#{line_i + 1} (OPEN_DENIED_FALLBACK)\n",
        "success",
    )
    return True


def _greedy_assign_lines_to_inputs(lines, sorted_inputs, used_inputs, line_to_input,
                                   line_indices=None):
    """Greedy Amount/Units assign selected portal lines onto unused inputs."""
    indices = (
        list(line_indices)
        if line_indices is not None
        else list(range(len(lines)))
    )
    n_in = len(sorted_inputs)
    for line_i in indices:
        if line_i in line_to_input:
            continue
        line = lines[line_i]
        best_in = None
        if _shared_bundle_span(lines) is not None:
            for j, (_df_idx, row) in enumerate(sorted_inputs):
                if j in used_inputs:
                    continue
                offset = day_offset_in_range(
                    safe_field(row, 'VisitDate'),
                    _shared_bundle_span(lines),
                )
                if offset == line_i:
                    best_in = j
                    break
        if best_in is None:
            best_in, _best_score = _best_unused_input_index(
                line, sorted_inputs, used_inputs,
            )
        if best_in is None:
            continue
        amount, units = _input_amount_units(sorted_inputs[best_in][1])
        if amount is None and units is None:
            for j in range(n_in):
                if j not in used_inputs:
                    best_in = j
                    break
        used_inputs.add(best_in)
        line_to_input[line_i] = best_in


def _empty_match_entry(unresolved=True):
    """Standard unresolved / empty match-map entry."""
    return {
        'line': None,
        'unresolved': unresolved,
        'units_mismatch': False,
        'split_bill': False,
        'confidence': MATCH_FLAG_MANUAL_REVIEW,
    }


def _match_fallback_tiers(
    sorted_inputs, line_rows, *, dates_aligned,
    header_billed=None, header_paid=None,
    panel_range_start=None, panel_range_end=None,
):
    """Legacy count-based matching (post Tier-1). Used as Tier 4 and compat path.

    Internals intentionally unchanged from the pre-pool algorithm:
      Open→Denied direct, single-input split-bill, filter_excess, positional,
      greedy Amount/Units, Open→Denied leftover fallback.
    """
    result = {}
    if not sorted_inputs:
        return result

    lines = list(line_rows or [])
    n_in = len(sorted_inputs)
    n_lines = len(lines)

    if n_lines == 0:
        for df_idx, _row in sorted_inputs:
            result[df_idx] = _empty_match_entry(True)
        return result

    filtered_by_amount_units = False
    if n_lines > n_in:
        direct = _match_open_to_denied_direct(
            sorted_inputs, lines, panel_range_start, panel_range_end,
        )
        if direct is not None:
            return direct
        if n_in == 1:
            special = _match_single_input_multi_line(
                sorted_inputs, lines, header_billed, header_paid,
            )
            if special is not None:
                return special
        lines = filter_excess_portal_lines(sorted_inputs, lines)
        n_lines = len(lines)
        filtered_by_amount_units = True
        log_to_gui(
            f"    ℹ️ Filtered portal lines to {n_lines} "
            f"(matched Amount/Units against {n_in} input row(s))\n",
            "info",
        )

    if n_lines == n_in:
        if dates_aligned:
            log_to_gui(
                f"    ✓ Service Dates aligned; positional map "
                f"{n_in} input row(s) → {n_lines} line(s)\n",
                "success",
            )
        base_label = (
            MATCH_CONFIDENCE_AMOUNT_UNITS if filtered_by_amount_units
            else MATCH_CONFIDENCE_POSITIONAL
        )
        used_positions = set()
        for i, (df_idx, row) in enumerate(sorted_inputs):
            pick = _assign_line_by_bundle_day(row, lines, used_positions)
            if pick is not None:
                line_i, line = pick
                used_positions.add(line_i)
            else:
                line = lines[i]
            amount, units = _input_amount_units(row)
            line_qty = parse_quantity(line.get('quantity'))
            units_mismatch = (
                units is not None
                and line_qty is not None
                and abs(units - line_qty) >= 0.005
            )
            if units_mismatch:
                log_to_gui(
                    f"    ⚠️ Units/Quantity mismatch for row {df_idx}: "
                    f"Units={units} vs Quantity={line_qty} "
                    f"(using positional map)\n",
                    "info",
                )
            rate_mismatch = rate_check(
                amount, units, line.get('billed'), line.get('quantity'),
            )
            if rate_mismatch:
                log_to_gui(
                    f"    ⚠️ Per-unit rate mismatch for row {df_idx}: "
                    f"Amount/Units vs Billed/Quantity disagree — "
                    f"flagged for review\n",
                    "error",
                )
            result[df_idx] = _build_matched_line_entry(
                line, row,
                confidence=base_label,
                units_mismatch=units_mismatch,
                rate_mismatch=rate_mismatch,
            )
        return result

    if n_in > n_lines:
        used_inputs = set()
        line_to_input = {}
        non_denied_indices = [
            i for i, ln in enumerate(lines) if not _line_status_is_denied(ln)
        ]
        denied_indices = [
            i for i, ln in enumerate(lines) if _line_status_is_denied(ln)
        ]
        _greedy_assign_lines_to_inputs(
            lines, sorted_inputs, used_inputs, line_to_input,
            line_indices=non_denied_indices,
        )

        assigned_inputs = set(line_to_input.values())
        for i, (df_idx, row) in enumerate(sorted_inputs):
            if i not in assigned_inputs:
                result[df_idx] = _empty_match_entry(True)
                continue
            line_i = next(li for li, ii in line_to_input.items() if ii == i)
            line = lines[line_i]
            amount, units = _input_amount_units(row)
            line_qty = parse_quantity(line.get('quantity'))
            units_mismatch = (
                units is not None
                and line_qty is not None
                and abs(units - line_qty) >= 0.005
            )
            rate_mismatch = rate_check(
                amount, units, line.get('billed'), line.get('quantity'),
            )
            result[df_idx] = _build_matched_line_entry(
                line, row,
                confidence=MATCH_CONFIDENCE_AMOUNT_UNITS,
                units_mismatch=units_mismatch,
                rate_mismatch=rate_mismatch,
            )

        _apply_open_denied_fallback(
            sorted_inputs, lines, result, line_to_input,
            panel_range_start, panel_range_end,
        )
        used_inputs = set(line_to_input.values())

        _greedy_assign_lines_to_inputs(
            lines, sorted_inputs, used_inputs, line_to_input,
            line_indices=denied_indices,
        )

        assigned_inputs = set(line_to_input.values())
        open_denied_df_idxs = {
            df_idx
            for df_idx, entry in result.items()
            if (
                not entry.get('unresolved')
                and entry.get('open_denied')
            )
        }
        for i, (df_idx, row) in enumerate(sorted_inputs):
            if df_idx in open_denied_df_idxs:
                continue
            if i not in assigned_inputs:
                result[df_idx] = _empty_match_entry(True)
                continue
            line_i = next(li for li, ii in line_to_input.items() if ii == i)
            line = lines[line_i]
            amount, units = _input_amount_units(row)
            line_qty = parse_quantity(line.get('quantity'))
            units_mismatch = (
                units is not None
                and line_qty is not None
                and abs(units - line_qty) >= 0.005
            )
            rate_mismatch = rate_check(
                amount, units, line.get('billed'), line.get('quantity'),
            )
            result[df_idx] = _build_matched_line_entry(
                line, row,
                confidence=MATCH_CONFIDENCE_AMOUNT_UNITS,
                units_mismatch=units_mismatch,
                rate_mismatch=rate_mismatch,
            )
        return result

    for df_idx, _row in sorted_inputs:
        result[df_idx] = _empty_match_entry(True)
    return result


def match_invoice_lines_to_inputs(
    sorted_inputs, line_rows, *, dates_aligned,
    header_billed=None, header_paid=None,
    panel_range_start=None, panel_range_end=None,
    enable_pool_tiers=True, allow_split_tiebreak=False, max_combo=3,
):
    """Map sorted invoice input rows to portal line rows.

    Returns ``{df_index: {'line': dict|None, 'unresolved': bool, ...}}``.

    When ``enable_pool_tiers`` is True (default), runs the consume-once pool
    pipeline (P1/P2, Tier 1/1.5, SPLIT, MERGE, then legacy fallbacks as Tier 4).
    When False, restores the legacy Tier-1-then-count-based behaviour bit-identically.
    """
    if not sorted_inputs:
        return {}

    lines = list(line_rows or [])

    if not enable_pool_tiers:
        if not lines:
            return {
                df_idx: _empty_match_entry(True)
                for df_idx, _row in sorted_inputs
            }
        try:
            tier1 = match_by_signature_groups(sorted_inputs, lines)
        except Exception as exc:
            log_to_gui(f"    ℹ️ Signature grouping skipped ({exc})\n", "info")
            tier1 = None
        if tier1 is not None:
            return tier1
        return _match_fallback_tiers(
            sorted_inputs, lines, dates_aligned=dates_aligned,
            header_billed=header_billed, header_paid=header_paid,
            panel_range_start=panel_range_start,
            panel_range_end=panel_range_end,
        )

    from availity_app.pool_matching import match_with_pool_tiers
    return match_with_pool_tiers(
        sorted_inputs, lines,
        dates_aligned=dates_aligned,
        header_billed=header_billed,
        header_paid=header_paid,
        panel_range_start=panel_range_start,
        panel_range_end=panel_range_end,
        allow_split_tiebreak=allow_split_tiebreak,
        max_combo=max_combo,
        fallback_fn=_match_fallback_tiers,
    )
