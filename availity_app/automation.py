"""Excel batch run, navigation, claim extraction, and row pipeline."""
import errno
import os
import re
import stat
import tempfile
import threading
import time
import traceback
from datetime import datetime
from tkinter import messagebox

import pandas as pd
from playwright.sync_api import expect

from availity_app import state
from availity_app.constants import (
    AVAILITY_NAVIGATION_HOME,
    CLAIM_SPA_REMOUNT_EVERY,
    CLAIM_NOT_FOUND_DENIAL_REASON,
    CLAIM_NOT_FOUND_LEGACY_STATUS,
    CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS,
    CLAIM_NOT_FOUND_STATUS,
    DOB_NOT_FOUND_STATUS,
    FULLY_PAID_DENIAL_REASON,
    FULLY_PAID_OVERPAYMENT_DENIAL_REASON,
    IN_PROCESS_DENIAL_REASON,
    HIPAA_REMARK_FALLBACK_STATUSES,
    INVALID_MEMBER_ID_STATUS,
    IN_BILLING_REVIEW_STATUS,
    KNOWN_ERROR_STATUSES,
    ADJUSTED_STATUS,
    NEED_MANUAL_REVIEW_STATUS,
    NO_CLAIMS_FOUND_ADVISED_STATUS,
    PAID_OVERPAYMENT_STATUS,
    OUTPUT_COLUMNS,
    resolved_output_columns,
    PAYER_CONFIG,
    REMIT_CHECK_DATE_GRACE_DAYS,
    REMIT_NOT_FOUND_DENIAL,
    REMITTANCE_URLS,
    REQUIRED_COLUMNS,
    SELECTORS,
    STATUS_COLUMNS,
    get_selector_profile,
    resolve_provider_npi,
)
from availity_app.driver import (
    close_managed_edge_if_owned,
    ensure_edge_with_cdp,
    ensure_playwright_disconnected,
    get_live_page,
    setup_browser,
    validate_navigation_page_ready,
    _find_logged_in_page,
    _interruptible_sleep,
    is_claim_search_page,
)
from availity_app.logging_gui import log_to_gui
from availity_app.user_settings import get_search_miss_phrases
from availity_app.ui_control import (
    UI_STATE_IDLE,
    UI_STATE_READY,
    finish_automation_progress,
    reset_automation_progress,
    reset_ui_state,
    set_save_progress_status,
    set_save_progress_indeterminate,
    update_automation_progress,
    update_excel_row_stats,
    _prompt_user_relogin,
    _set_ui_state,
)
from availity_app.async_save import (
    flush_async_saves,
    save_sync_or_async,
    start_save_worker,
    stop_save_worker,
)
from availity_app.excel_export import (
    prepare_export_df,
    write_formatted_workbook,
)
from availity_app.discovery_cache import (
    get_line_table_columns,
    persist_line_table_columns,
)
from availity_app.user_settings import (
    get_large_file_save_interval,
    get_medium_file_save_interval,
    get_output_formatting_enabled,
    get_uppercase_export_enabled,
    get_remit_check_date_grace_days,
    parse_org_payer_ids,
    resolve_payer_config,
)
from availity_app.resilience import (
    BatchCircuitBreaker,
    capture_row_failure_artifact,
    classify_row_failure,
)
from availity_app.claim_snapshots import (
    attach_invoice_alignment,
    make_claim_snapshot,
    materialize_row_results,
    need_manual_review_result,
    pending_invoice_sibling_indices,
    unresolved_alignment_result,
    visit_date_for_row,
)
from availity_app.result_validation import (
    FIELD_FAILED,
    FIELD_FOUND,
    FieldRead,
    format_validation_errors,
    is_placeholder,
    validate_claim_results,
)
from availity_app.search_grouping import (
    build_consecutive_search_groups,
    supports_patient_search_grouping,
)
from availity_app.invoice_index import attach_invoice_index, _agent_log
from availity_app.progress_wal import append_wal_rows, clear_wal, replay_wal
from availity_app.utils import (
    aggregate_invoice_visit_date_range,
    aggregate_line_amounts_for_payer,
    adjustment_pair_group_key,
    build_results_row_fingerprint,
    classify_adjustment_pair_roles,
    coerce_visit_date,
    empty_claim_result,
    filter_line_rows_by_visit_date,
    first_adjustment_original_line,
    format_currency_amount,
    get_sorted_invoice_rows,
    humanize_match_confidence,
    invoice_service_dates_aligned,
    match_invoice_lines_to_inputs,
    MATCH_FLAG_MANUAL_REVIEW,
    join_remit_lines_to_claim_lines,
    parse_service_date_range,
    parse_service_dates_panel_text,
    payer_detects_adjustment_pairs,
    resolve_invoice_line_context,
    invoice_row_indices,
    normalize_date,
    normalize_date_range,
    normalize_dob,
    normalize_results_cell_text,
    normalize_search_date,
    parse_amount,
    payer_matches_results_by_visit_and_amount,
    resolve_line_level_config,
    resolve_invoice_search_dates,
    resolve_service_search_dates,
    row_has_valid_dob,
    service_date_fill_plans,
    service_dates_cross_year,
    retry_with_backoff,
    normalize_invoice_column,
    normalize_input_headers,
    normalize_patient_names,
    results_row_matches_visit_and_amount,
    safe_alt_patient_id,
    safe_field,
    safe_invoice_number,
    split_date_parts,
    validate_dataframe,
    visit_date_matches_line,
)


# ============================================================================
# SECTION 7 — EXCEL / FILE OPERATIONS
# ============================================================================

def _normalize_recheck_value(value):
    """Return 'yes', 'no', or 'empty' for a Recheck cell."""
    if pd.isna(value):
        return 'empty'
    text = str(value).strip().lower()
    if text in ('', 'nan'):
        return 'empty'
    if text == 'yes':
        return 'yes'
    if text == 'no':
        return 'no'
    return text


def compute_recheck_stats(df):
    """Return (total_rows, recheck_yes_count, recheck_empty_count) from the full sheet."""
    if getattr(df, 'is_run_store', False):
        return tuple(df.meta['stats'][key] for key in ('total', 'recheck_yes', 'recheck_empty'))
    total = len(df)
    if 'Recheck' not in df.columns:
        return total, 0, total
    kinds = df['Recheck'].map(_normalize_recheck_value)
    yes_count = int((kinds == 'yes').sum())
    empty_count = int((kinds == 'empty').sum())
    return total, yes_count, empty_count


def _is_recheck_yes(df, idx):
    """True when the row's Recheck cell is yes (case-insensitive)."""
    if 'Recheck' not in df.columns:
        return False
    return _normalize_recheck_value(df.at[idx, 'Recheck']) == 'yes'


def _is_recheck_no(df, idx):
    """True when the row's Recheck cell is no (case-insensitive)."""
    if 'Recheck' not in df.columns:
        return False
    return _normalize_recheck_value(df.at[idx, 'Recheck']) == 'no'


def _is_exact_in_billing_review(value):
    """True when a cell is exactly ``In Billing Review`` after trim."""
    if pd.isna(value):
        return False
    text = str(value).strip()
    if text.lower() in ('', 'nan'):
        return False
    return text == IN_BILLING_REVIEW_STATUS


_OPTIONAL_COL_CACHE = '_availity_optional_cols'


def _cached_optional_column(df, header):
    """Resolve ``header`` to a real column name once per dataframe."""
    try:
        cache = df.attrs.setdefault(_OPTIONAL_COL_CACHE, {})
    except Exception:
        cache = {}
    if header in cache:
        return cache[header]
    target = str(header).strip().lower()
    found = None
    for col in df.columns:
        if str(col).strip().lower() == target:
            found = col
            break
    try:
        cache[header] = found
        df.attrs[_OPTIONAL_COL_CACHE] = cache
    except Exception:
        pass
    return found


def _optional_spaced_column_value(df, idx, header):
    """Return the first column whose name matches ``header`` ignoring case.

    ``ClaimStatus`` (no space) is the collection field and is never matched
    by ``Claim Status``. Used to read the optional end-of-sheet columns.
    """
    col = _cached_optional_column(df, header)
    if col is None:
        return None
    return df.at[idx, col]


def _cell_is_blank(value):
    """True when a dataframe cell is empty for status/error checks."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text == '' or text.lower() == 'nan'


def _row_has_prior_error_values(claim_status, last_err, auto_status):
    """Same rules as ``_row_has_prior_error`` using already-read cell values."""
    if isinstance(claim_status, str) and _is_claim_not_found_status(
        claim_status.strip(),
    ):
        return False
    if not _cell_is_blank(last_err):
        return True
    status = str(auto_status).strip().lower()
    if status in ('error', 'inprogress'):
        return True
    if _claim_status_has_known_error(claim_status):
        return True
    return False


def _billing_review_skip_values(recheck, claim_status, denial):
    """True when Recheck=no and both optional columns are In Billing Review."""
    if _normalize_recheck_value(recheck) != 'no':
        return False
    return (
        _is_exact_in_billing_review(claim_status)
        and _is_exact_in_billing_review(denial)
    )


def row_is_in_billing_review_skip(df, idx):
    """True when Recheck=no and optional Claim Status and Denial Reason match.

    Both optional end-of-sheet columns must be exactly ``In Billing Review``.
    Recheck=yes or empty rows are searched even when those values are present.
    """
    recheck = df.at[idx, 'Recheck'] if 'Recheck' in df.columns else None
    status_val = _optional_spaced_column_value(df, idx, 'Claim Status')
    denial_val = _optional_spaced_column_value(df, idx, 'Denial Reason')
    return _billing_review_skip_values(recheck, status_val, denial_val)


def apply_in_billing_review_rows(df):
    """Complete Recheck=no In Billing Review rows without searching Availity."""
    if df is None or len(df) == 0:
        return df
    marked = 0
    for idx in range(len(df)):
        if not row_is_in_billing_review_skip(df, idx):
            continue
        df.at[idx, 'Claim Status'] = IN_BILLING_REVIEW_STATUS
        df.at[idx, 'Denial Reason'] = IN_BILLING_REVIEW_STATUS
        safe_mark_row_status(df, idx, 'Done', '')
        marked += 1
    if marked:
        log_to_gui(
            f"ℹ️ {marked} row(s) marked Done (In Billing Review, Recheck=no) "
            "— skipped search.\n",
            "info",
        )
    return df


_KNOWN_ERROR_STATUSES_LOWER = frozenset(
    status.lower() for status in KNOWN_ERROR_STATUSES
)


def _claim_status_has_known_error(status_val):
    """True when Claim Status (possibly multiline) contains a known error label."""
    if not isinstance(status_val, str):
        return False
    for line in status_val.split('\n'):
        val = line.split('. ', 1)[-1].strip().lower()
        if val in _KNOWN_ERROR_STATUSES_LOWER:
            return True
    return False


def apply_claim_not_found_completed_rows(df):
    """Claim Not Found is a completed outcome: Done with an empty LastError."""
    if df is None or len(df) == 0 or 'Claim Status' not in df.columns:
        return df
    statuses = df['Claim Status'].to_numpy()
    autos = (
        df['AutomationStatus'].to_numpy()
        if 'AutomationStatus' in df.columns else None
    )
    errors = df['LastError'].to_numpy() if 'LastError' in df.columns else None
    for idx, status in enumerate(statuses):
        if not isinstance(status, str):
            continue
        if not _is_claim_not_found_status(status.strip()):
            continue
        auto = str(autos[idx]).strip().lower() if autos is not None else ''
        err = errors[idx] if errors is not None else ''
        if auto == 'done' and _cell_is_blank(err):
            continue
        safe_mark_row_status(df, idx, 'Done', '')
    return df


def _row_has_prior_error(df, idx):
    """True when the row should be retried due to a prior failure."""
    claim_status = df.at[idx, 'Claim Status'] if 'Claim Status' in df.columns else None
    last_err = df.at[idx, 'LastError'] if 'LastError' in df.columns else None
    auto_status = (
        df.at[idx, 'AutomationStatus'] if 'AutomationStatus' in df.columns else ''
    )
    return _row_has_prior_error_values(claim_status, last_err, auto_status)


def queue_error_rows_for_retry(df):
    """Reset prior-error rows to Pending so they re-enter the batch queue."""
    n = 0 if df is None else len(df)
    if n == 0:
        return df
    recheck = df['Recheck'].to_numpy() if 'Recheck' in df.columns else None
    claims = df['Claim Status'].to_numpy() if 'Claim Status' in df.columns else None
    denial_col = _cached_optional_column(df, 'Denial Reason')
    denials = df[denial_col].to_numpy() if denial_col else None
    errors = df['LastError'].to_numpy() if 'LastError' in df.columns else None
    autos = (
        df['AutomationStatus'].to_numpy()
        if 'AutomationStatus' in df.columns else None
    )
    pending_idxs = []
    for idx in range(n):
        rec = recheck[idx] if recheck is not None else None
        claim = claims[idx] if claims is not None else None
        denial = denials[idx] if denials is not None else None
        if _billing_review_skip_values(rec, claim, denial):
            continue
        last_err = errors[idx] if errors is not None else None
        auto = autos[idx] if autos is not None else ''
        if not _row_has_prior_error_values(claim, last_err, auto):
            continue
        if str(auto).strip().lower() == 'pending':
            continue
        pending_idxs.append(idx)
    if pending_idxs and 'AutomationStatus' in df.columns:
        df.iloc[pending_idxs, df.columns.get_loc('AutomationStatus')] = 'Pending'
    return df


def _dedupe_preserve_order(indices):
    """Return indices in first-seen order with duplicates removed."""
    seen = set()
    out = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def row_should_process(df, idx):
    """True if this row should be searched/processed in the current batch.

    Recheck=no rows whose optional Claim Status and Denial Reason are both
    exactly In Billing Review are never searched. Prior errors otherwise
    qualify (even when Recheck=no). Recheck=yes qualifies Done rows for
    manual re-run. Recheck=no skips non-error Done/Skipped rows.
    """
    recheck = df.at[idx, 'Recheck'] if 'Recheck' in df.columns else None
    claim_status = _optional_spaced_column_value(df, idx, 'Claim Status')
    denial = _optional_spaced_column_value(df, idx, 'Denial Reason')
    if _billing_review_skip_values(recheck, claim_status, denial):
        return False
    last_err = df.at[idx, 'LastError'] if 'LastError' in df.columns else None
    auto_status = (
        df.at[idx, 'AutomationStatus'] if 'AutomationStatus' in df.columns else ''
    )
    if _row_has_prior_error_values(claim_status, last_err, auto_status):
        return True
    kind = _normalize_recheck_value(recheck)
    if kind == 'yes':
        return True
    if kind == 'no':
        return False
    status = str(auto_status).strip().lower()
    return status not in ('done', 'skipped')


def _collect_pending_indices(df, limit=None):
    """Return pending row indexes, stopping after ``limit`` matches when set."""
    if getattr(df, 'is_run_store', False):
        return df.pending_indices(limit)
    n = 0 if df is None else len(df)
    if n == 0:
        return []
    recheck = df['Recheck'].to_numpy() if 'Recheck' in df.columns else None
    claims = df['Claim Status'].to_numpy() if 'Claim Status' in df.columns else None
    denial_col = _cached_optional_column(df, 'Denial Reason')
    denials = df[denial_col].to_numpy() if denial_col else None
    errors = df['LastError'].to_numpy() if 'LastError' in df.columns else None
    autos = (
        df['AutomationStatus'].to_numpy()
        if 'AutomationStatus' in df.columns else None
    )
    pending = []
    for idx in range(n):
        rec = recheck[idx] if recheck is not None else None
        claim = claims[idx] if claims is not None else None
        denial = denials[idx] if denials is not None else None
        if _billing_review_skip_values(rec, claim, denial):
            continue
        last_err = errors[idx] if errors is not None else None
        auto = autos[idx] if autos is not None else ''
        if _row_has_prior_error_values(claim, last_err, auto):
            pending.append(idx)
        else:
            kind = _normalize_recheck_value(rec)
            if kind == 'yes':
                pending.append(idx)
            elif kind != 'no' and str(auto).strip().lower() not in ('done', 'skipped'):
                pending.append(idx)
        if limit is not None and len(pending) >= limit:
            break
    return pending


def mark_recheck_skipped_rows(df):
    """Mark rows with Recheck=no as Skipped so they are not processed."""
    if 'Recheck' not in df.columns:
        return df
    for idx in range(len(df)):
        if not _is_recheck_no(df, idx):
            continue
        if row_is_in_billing_review_skip(df, idx):
            continue
        if not _row_has_prior_error(df, idx):
            df.at[idx, 'AutomationStatus'] = 'Skipped'
    return df


def queue_recheck_yes_rows(df):
    """Reset Done/Skipped rows with Recheck=yes so they re-enter the claim queue."""
    if df is None or len(df) == 0 or 'Recheck' not in df.columns:
        return df
    kinds = df['Recheck'].map(_normalize_recheck_value).to_numpy()
    autos = (
        df['AutomationStatus'].to_numpy()
        if 'AutomationStatus' in df.columns else None
    )
    reset_idxs = []
    for idx, kind in enumerate(kinds):
        if kind != 'yes':
            continue
        status = str(autos[idx]).strip().lower() if autos is not None else ''
        if status in ('done', 'skipped'):
            reset_idxs.append(idx)
    if reset_idxs and 'AutomationStatus' in df.columns:
        df.iloc[reset_idxs, df.columns.get_loc('AutomationStatus')] = 'Pending'
    return df


def _show_excel_load_error(message):
    """Show an Excel load error dialog on the Tk main thread."""
    def _dialog():
        messagebox.showerror("Error", message)

    if state.root is not None:
        state.root.after(0, _dialog)
    else:
        log_to_gui(f"❌ {message}\n", "error")


def load_excel(file_path):
    """Load the input Excel workbook and split PatientName into Last/First.

    PatientName is expected in 'LastName, FirstName' format.
    Returns ``(dataframe, None)`` on success or ``(None, error_message)`` on failure.
    """
    try:
        with pd.ExcelFile(file_path, engine='openpyxl') as xl:
            sheet_name = xl.sheet_names[0] if xl.sheet_names else 'Sheet1'
            state.loaded_sheet_name = sheet_name
            df = pd.read_excel(xl, sheet_name=sheet_name)
        df.columns = normalize_input_headers(df.columns)
        if 'PatientName' in df.columns:
            parts = df['PatientName'].str.split(',', n=1, expand=True)
            df['Last_Name']  = parts[0].str.strip()
            df['First_Name'] = parts[1].str.strip() if len(parts.columns) > 1 else ''
        df = normalize_invoice_column(df)
        return df, None
    except Exception as e:
        return None, f"Failed to load Excel file: {e}"


def load_and_prepare_excel(file_path, payer_config=None):
    """Load Excel, validate columns, add output fields, and apply Recheck rules.

    Recheck=no rows whose optional Claim Status and Denial Reason are both
    exactly ``In Billing Review`` are marked Done (no Availity search).

    Returns (dataframe, stats_dict) on success, or (None, None) on failure.
    stats_dict has keys: total, recheck_yes, recheck_empty.
    ``payer_config`` adds payer-specific required columns (e.g. Amount for Aetna).
    """
    df, load_error = load_excel(file_path)
    if df is None:
        if load_error:
            _show_excel_load_error(load_error)
        return None, None

    missing = validate_dataframe(df, payer_config)
    if missing:
        log_to_gui(f"❌ Missing columns: {', '.join(missing)}\n", "error")
        log_to_gui("   Check your Excel headers and try again.\n", "error")
        return None, None

    df = add_output_columns(df, payer_config)
    df = apply_in_billing_review_rows(df)
    df = mark_recheck_skipped_rows(df)
    df = apply_claim_not_found_completed_rows(df)
    df = queue_recheck_yes_rows(df)
    df = queue_error_rows_for_retry(df)
    total, recheck_yes, recheck_empty = compute_recheck_stats(df)
    stats = {
        'total': total,
        'recheck_yes': recheck_yes,
        'recheck_empty': recheck_empty,
    }
    attach_invoice_index(df)
    return df, stats


def add_output_columns(df, payer_config=None):
    """Append the payer's output columns to df with empty defaults."""
    # Backward compatibility: normalize old header casing if present.
    if 'Last action taken' in df.columns and 'Last Action Taken' not in df.columns:
        df = df.rename(columns={'Last action taken': 'Last Action Taken'})
    for col in list(df.columns):
        if col != 'Recheck' and str(col).strip().lower() == 'recheck':
            df = df.rename(columns={col: 'Recheck'})
            break
    # Optional end-of-sheet "Claim status" is the automation Claim Status column.
    # Only rename when collection ClaimStatus already exists (or this is a
    # prior Automated.xlsx) so a fresh collection "Claim Status" is still
    # preserved as ClaimStatus.
    if 'ClaimStatus' in df.columns or 'AutomationStatus' in df.columns:
        for col in list(df.columns):
            if col != 'Claim Status' and str(col).strip().lower() == 'claim status':
                if 'Claim Status' not in df.columns:
                    df = df.rename(columns={col: 'Claim Status'})
                break

    # Fresh collection reports already include an input "Claim Status" column.
    # Rename it so OUTPUT_COLUMNS can add the automation "Claim Status".
    # Skip on subsequent loads of Automated.xlsx (already has AutomationStatus
    # and/or ClaimStatus from the first run).
    is_fresh_collection = (
        'ClaimStatus' not in df.columns
        and 'AutomationStatus' not in df.columns
    )
    if 'Claim Status' in df.columns and is_fresh_collection:
        df = df.rename(columns={'Claim Status': 'ClaimStatus'})

    output_cols = resolved_output_columns(payer_config)
    if 'Match Confidence' in df.columns and 'Match Confidence' not in output_cols:
        df = df.drop(columns=['Match Confidence'])
    for col in output_cols + STATUS_COLUMNS:
        if col not in df.columns:
            df[col] = ''
        else:
            # Empty Excel cells are read as float64 NaN; coerce so text results can be written.
            df[col] = df[col].fillna('').astype(object)
    df['AutomationStatus'] = df['AutomationStatus'].replace('', 'Pending').fillna('Pending')
    df['LastError'] = df['LastError'].fillna('')
    return df


def get_output_file_path(output_folder):
    """Return the single fixed output file path for all progress/final saves."""
    return os.path.join(output_folder, "Automated.xlsx")


_OUTPUT_FORMATTING_ENABLED = True
_SMALL_FILE_SAVE_EVERY_ROW = 100
_MEDIUM_FILE_ROW_THRESHOLD = 500
_MEDIUM_FILE_SAVE_INTERVAL = 10
_MEDIUM_FILE_SAVE_SECONDS = 20.0
_LARGE_FILE_SAVE_INTERVAL = 100
_LARGE_FILE_SAVE_SECONDS = 120.0


def _acquire_save_lock():
    """Block until the save lock is free; show status if another save is in progress."""
    if state.save_lock.locked():
        set_save_progress_status("Waiting for save to finish…")
    state.save_lock.acquire()


def _should_save_progress(row_count, rows_since_last_save, seconds_since_last_save=0.0):
    """Return True when a mid-batch progress save should run."""
    if row_count <= _SMALL_FILE_SAVE_EVERY_ROW:
        return True
    if row_count <= _MEDIUM_FILE_ROW_THRESHOLD:
        return (
            rows_since_last_save >= get_medium_file_save_interval()
            or seconds_since_last_save >= _MEDIUM_FILE_SAVE_SECONDS
        )
    return (
        rows_since_last_save >= get_large_file_save_interval()
        or seconds_since_last_save >= _LARGE_FILE_SAVE_SECONDS
    )


def save_progress_results(df, output_folder, *, force=False):
    """Flush dirty rows to JSONL; compile Excel when the throttle allows.

    ``force`` is accepted for call-site compatibility but no longer bypasses
    the Excel interval. Stop and final saves use ``save_results``.
    """
    if getattr(df, 'is_run_store', False):
        df.commit()
        return
    del force
    raw_dirty = getattr(state, 'dirty_row_indices', None)
    if isinstance(raw_dirty, set) and raw_dirty:
        dirty = sorted(raw_dirty)
        raw_dirty.clear()
        append_wal_rows(df, output_folder, dirty)
    now = time.monotonic()
    last = getattr(state, 'last_save_monotonic', 0.0) or 0.0
    elapsed = (now - last) if last else float('inf')
    if _should_save_progress(len(df), state.rows_since_last_save, elapsed):
        # #region agent log
        if len(df) > 100:
            _agent_log(
                'E',
                'automation.py:save_progress_results',
                'excel compile enqueued',
                {
                    'nrows': len(df),
                    'rows_since': state.rows_since_last_save,
                    'elapsed_s': round(elapsed if elapsed != float('inf') else -1, 1),
                },
            )
        # #endregion
        save_sync_or_async(df, output_folder, finalize=False, force_async=True)
        state.rows_since_last_save = 0
        state.last_save_monotonic = now
    else:
        state.rows_since_last_save += 1


_SAVE_REPLACE_ATTEMPTS = 8
_SAVE_REPLACE_RETRY_SECONDS = 0.4
_LOCKED_OUTPUT_FILENAME = "Automated_autosave.xlsx"


def _is_file_lock_error(exc):
    """True when Windows/Excel is holding the destination workbook."""
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, 'winerror', None)
    if winerror in (5, 32):
        return True
    return getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM)


def _clear_readonly_attribute(path):
    """Best-effort clear of the Windows read-only bit before overwrite."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _replace_results_file(temp_path, dest_path):
    """Atomically replace dest with temp, retrying Excel/AV file locks.

    When Automated.xlsx stays locked, write Automated_autosave.xlsx in the
    same folder so progress is not lost.
    """
    last_error = None
    for attempt in range(_SAVE_REPLACE_ATTEMPTS):
        try:
            if os.path.exists(dest_path):
                _clear_readonly_attribute(dest_path)
            os.replace(temp_path, dest_path)
            return dest_path
        except OSError as e:
            last_error = e
            if not _is_file_lock_error(e):
                raise
            time.sleep(_SAVE_REPLACE_RETRY_SECONDS * (attempt + 1))

    fallback = os.path.join(
        os.path.dirname(dest_path) or '.', _LOCKED_OUTPUT_FILENAME,
    )
    if os.path.normcase(os.path.abspath(fallback)) == os.path.normcase(
        os.path.abspath(dest_path)
    ):
        raise last_error
    try:
        if os.path.exists(fallback):
            _clear_readonly_attribute(fallback)
        os.replace(temp_path, fallback)
    except OSError:
        raise last_error
    log_to_gui(
        f"  ⚠️ Could not overwrite {os.path.basename(dest_path)} "
        f"(file is open or locked).\n"
        f"     Saved progress to: {fallback}\n"
        f"     Close {os.path.basename(dest_path)} in Excel to write there "
        "on the next save.\n",
        "error",
    )
    return fallback


def _write_results_file(df, output_folder, *, finalize=True):
    """Write df to Automated.xlsx (used by sync and async save paths)."""
    if getattr(df, 'is_run_store', False):
        from availity_app.run_export import export_run
        saved_path = export_run(df.path, output_folder)
        log_to_gui(f"  💾 Saved: {saved_path}\n", "info")
        return saved_path
    _acquire_save_lock()
    if finalize:
        status = (
            "Saving and formatting results…"
            if get_output_formatting_enabled()
            else "Saving results…"
        )
        set_save_progress_status(status)
    else:
        set_save_progress_status("Saving progress…")
    path = get_output_file_path(output_folder)
    temp_dir = output_folder if os.path.isdir(output_folder) else None
    fd, temp_path = tempfile.mkstemp(
        suffix='.xlsx', prefix='Automated_', dir=temp_dir,
    )
    os.close(fd)
    try:
        export_df = prepare_export_df(df)
        sheet_name = getattr(state, 'loaded_sheet_name', None) or 'Sheet1'
        write_formatted_workbook(
            export_df,
            temp_path,
            sheet_name,
            formatting_enabled=get_output_formatting_enabled(),
            uppercase_enabled=get_uppercase_export_enabled(),
        )
        saved_path = _replace_results_file(temp_path, path)
        temp_path = None
        clear_wal(output_folder)
        log_to_gui(f"  💾 Saved: {saved_path}\n", "info")
        return saved_path
    except OSError as e:
        log_to_gui(
            f"  ❌ Save error: {e}\n"
            "     Close Automated.xlsx if it is open in Excel, then try again.\n",
            "error",
        )
        return None
    except Exception as e:
        log_to_gui(f"  ❌ Save error: {e}\n", "error")
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        state.save_lock.release()


def save_results(df, output_folder, *, finalize=True):
    """Write df to one fixed Excel workbook in output_folder.

    Last_Name and First_Name are excluded from the output because they are
    derived from PatientName and are not needed by downstream consumers.
    Serialized via ``save_lock`` so the GUI-thread Stop save and the worker
    row save can't interleave the same write call.

    Progress, Stop, and final saves all use the same formatted xlsxwriter
    writer when output formatting is enabled. ``finalize`` only
    controls whether the caller waits for the write (Stop / batch complete).

    Returns the saved file path, or None on failure.
    """
    flush_async_saves(timeout=120.0)
    return _write_results_file(df, output_folder, finalize=finalize)


def _run_finalize_save(df, output_folder, *, timeout=None):
    """Run formatted save on a background thread; block caller until done."""
    if getattr(df, 'is_run_store', False):
        df.commit()
    done = threading.Event()
    result = {'path': None}

    def _worker():
        try:
            set_save_progress_indeterminate(True)
            set_save_progress_status(
                "Saving and formatting results…"
                if get_output_formatting_enabled()
                else "Saving results…"
            )
            result['path'] = save_results(df, output_folder, finalize=True)
        except Exception as exc:
            log_to_gui(f"  ❌ Final export failed: {exc}\n", "error")
        finally:
            set_save_progress_indeterminate(False)
            done.set()

    thread = threading.Thread(
        target=_worker, name='availity-final-save', daemon=True,
    )
    thread.start()
    if not done.wait(timeout=timeout):
        log_to_gui(
            "  ⚠️ Final save did not finish in time — check Automated.xlsx.\n",
            "error",
        )
    thread.join(timeout=1.0)
    return result['path']


def _mark_row_dirty(row_index):
    """Record ``row_index`` so the next progress flush writes it to the WAL."""
    dirty = getattr(state, 'dirty_row_indices', None)
    if dirty is None:
        state.dirty_row_indices = set()
        dirty = state.dirty_row_indices
    try:
        dirty.add(int(row_index))
    except (TypeError, ValueError):
        pass


def write_row_result(df, row_index, result_dict):
    """Apply every key/value in result_dict to the given dataframe row in-place."""
    if getattr(df, 'is_run_store', False):
        for field, value in result_dict.items():
            if field in df.columns:
                df.at[row_index, field] = value
        return
    with state.df_mutate_lock:
        for field, value in result_dict.items():
            if field in df.columns and pd.api.types.is_numeric_dtype(df[field].dtype):
                df[field] = df[field].astype(object)
            df.at[row_index, field] = value
    _mark_row_dirty(row_index)


def safe_mark_row_status(df, row_index, status, err=''):
    """Safely update row processing status columns."""
    if getattr(df, 'is_run_store', False):
        df.at[row_index, 'AutomationStatus'] = status
        df.at[row_index, 'LastError'] = err
        return
    with state.df_mutate_lock:
        for col in STATUS_COLUMNS:
            if col in df.columns and pd.api.types.is_numeric_dtype(df[col].dtype):
                df[col] = df[col].astype(object)
        df.at[row_index, 'AutomationStatus'] = status
        df.at[row_index, 'LastError'] = err
    _mark_row_dirty(row_index)


# ============================================================================
# SECTION 8 — PAGE NAVIGATION
# ============================================================================

_PLAIN_INPUT_DATE_MODES = frozenset({'legacy_input', 'plain_input'})

_PAGE_READY_TIMEOUT_MS = 30000
_LOGIN_NEEDED_POLL_MS = 1000
_IFRAME_LOGIN_NEEDED_SELECTORS = (
    '#loginNeeded',
    '#loginNeeded .card-title:has-text("You must login to proceed.")',
    'nav[aria-label="breadcrumbs"] p:has-text("LoginError")',
)
IFRAME_LOGIN_NEEDED_REASON = (
    'Claim Status iframe shows LoginError (You must login to proceed.)'
)


def _uses_plain_date_inputs(sel):
    """True when search dates are filled via plain text inputs (not MUI pickers)."""
    return sel.get('date_fill_mode') in _PLAIN_INPUT_DATE_MODES


def _resolve_page(page):
    """Return ``page`` when still open, else fall back to any live browser tab."""
    try:
        if page is not None and not page.is_closed():
            return page
    except Exception:
        pass
    return get_live_page(page)


def _claim_search_iframe(page, payer_config=None):
    """Return a FrameLocator for the claim-search iframe after attach check."""
    page = _resolve_page(page)
    sel = get_selector_profile(payer_config)
    page.wait_for_selector(
        sel['iframe'], state='attached', timeout=_PAGE_READY_TIMEOUT_MS,
    )
    return page.frame_locator(sel['iframe'])


def wait_for_claim_results_ready(page, payer_config=None, quiet=False,
                                 timeout_ms=None):
    """Block until the claim-search iframe and #claimsTable are visible.

    Use after the Results breadcrumb — not the search form.
    """
    timeout_ms = timeout_ms or _PAGE_READY_TIMEOUT_MS
    page = _resolve_page(page)
    if not is_claim_search_page(page):
        raise RuntimeError(
            "Browser tab is not on claim search — navigation shell only "
            f"({page.url})"
        )
    if not quiet:
        log_to_gui("  → Waiting for results table...\n")
    iframe = _claim_search_iframe(page, payer_config)
    iframe.locator(SELECTORS['results_table']).wait_for(
        state='visible', timeout=timeout_ms,
    )
    if not quiet:
        log_to_gui("  ✓ Results table ready\n", "success")
    return iframe


def _is_on_claim_details_page(page):
    """True when the outer URL is on a claim detail route."""
    url = (page.url or '').lower()
    return 'standard-details' in url or 'claim-details' in url


def _results_table_is_visible(page, payer_config=None, timeout_ms=5000):
    """True when #claimsTable is visible inside the search iframe."""
    try:
        iframe = _claim_search_iframe(page, payer_config)
        iframe.locator(SELECTORS['results_table']).first.wait_for(
            state='visible', timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _back_to_results_link_is_visible(page, payer_config=None, timeout_ms=2000):
    """True when the iframe Back to Results control is on screen.

    That link exists on claim detail, not on search alerts / portal errors.
    """
    try:
        sel = get_selector_profile(payer_config)
        iframe = _claim_search_iframe(page, payer_config)
        back_selectors = (
            sel.get('back_to_results'),
            'a:has-text("Back to Results")',
        )
        seen = []
        for selector in back_selectors:
            if not selector or selector in seen:
                continue
            seen.append(selector)
            try:
                iframe.locator(selector).first.wait_for(
                    state='visible', timeout=timeout_ms,
                )
                return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _rerun_claim_search(page, row_data, payer_config, df=None):
    """Return to the search form and re-submit to rebuild #claimsTable."""
    log_to_gui("  → Re-running claim search to restore results...\n", "info")
    # MUI service-date range pickers reject in-place edits after a prior search;
    # always unload/reload so the next fill starts on a clean form.
    page = _reload_and_navigate(page, payer_config)
    iframe = wait_for_page_ready(page, payer_config=payer_config)
    if not fill_search_form(page, iframe, row_data, payer_config, df=df):
        raise RuntimeError('Re-search form fill failed')
    outcome = submit_search_and_wait(iframe)
    if outcome != 'results':
        raise RuntimeError(f'Re-search did not return results: {outcome}')
    return page, iframe


def _return_to_results_via_back_link(page, payer_config, results_timeout_ms):
    """Return to #claimsTable by clicking the iframe Back to Results link."""
    sel = get_selector_profile(payer_config)
    iframe = _claim_search_iframe(page, payer_config)
    back_selectors = (
        sel.get('back_to_results'),
        'a:has-text("Back to Results")',
    )
    log_to_gui("  → Back to Results...\n")
    _click_first_visible(iframe, back_selectors, timeout_ms=8000)
    if _results_table_is_visible(page, payer_config, timeout_ms=results_timeout_ms):
        return wait_for_claim_results_ready(
            page, payer_config=payer_config, quiet=True,
            timeout_ms=results_timeout_ms,
        )
    raise RuntimeError(
        f'Results table did not appear after Back to Results ({page.url})'
    )


def _return_to_results_view(page, payer_config=None):
    """Return to the results list from claim detail.

    Payers with ``return_via_back_to_results`` (Villagecaremax, Integra,
    Anthem, Empire) use only the iframe Back to Results link. Other payers
    try browser back, then the Results breadcrumb.
    """
    page = _resolve_page(page)
    results_timeout_ms = 12000

    if _payer_returns_via_back_to_results(payer_config):
        return _return_to_results_via_back_link(
            page, payer_config, results_timeout_ms,
        )

    if _is_on_claim_details_page(page):
        try:
            log_to_gui("  → Browser back to results list...\n")
            page.go_back(wait_until='domcontentloaded', timeout=20000)
            if _results_table_is_visible(page, payer_config, timeout_ms=results_timeout_ms):
                return wait_for_claim_results_ready(
                    page, payer_config=payer_config, quiet=True,
                    timeout_ms=results_timeout_ms,
                )
        except Exception:
            pass

    sel = get_selector_profile(payer_config)
    iframe = page.frame_locator(sel['iframe'])
    results_link = iframe.locator(sel.get('breadcrumb_results') or SELECTORS['breadcrumb_results'])
    if results_link.first.is_visible(timeout=5000):
        results_link.first.click(timeout=15000)
        page.wait_for_load_state('domcontentloaded', timeout=45000)
        if _results_table_is_visible(page, payer_config, timeout_ms=results_timeout_ms):
            return wait_for_claim_results_ready(
                page, payer_config=payer_config, quiet=True,
                timeout_ms=results_timeout_ms,
            )

    raise RuntimeError(
        f'Results table did not appear after back navigation ({page.url})'
    )


def _iframe_shows_login_needed(iframe):
    """True when the claim-status iframe is the LoginError / must-login page."""
    for selector in _IFRAME_LOGIN_NEEDED_SELECTORS:
        try:
            loc = iframe.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _raise_if_iframe_login_needed(iframe):
    """Raise immediately when the iframe is asking the user to log in."""
    if _iframe_shows_login_needed(iframe):
        raise AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON)


def _wait_locator_visible_unless_login_needed(iframe, locator, timeout_ms):
    """Wait for ``locator`` unless the iframe swaps to the login-needed page."""
    deadline = time.time() + (timeout_ms / 1000.0)
    last_error = None
    while True:
        _raise_if_iframe_login_needed(iframe)
        remaining_ms = int((deadline - time.time()) * 1000)
        if remaining_ms <= 0:
            break
        try:
            locator.first.wait_for(
                state='visible', timeout=min(_LOGIN_NEEDED_POLL_MS, remaining_ms),
            )
            return
        except AvailitySessionInactiveError:
            raise
        except Exception as e:
            last_error = e
    _raise_if_iframe_login_needed(iframe)
    if last_error:
        raise last_error
    raise TimeoutError('Timed out waiting for iframe locator')


def _wait_for_search_form_in_iframe(iframe, sel, timeout_ms=15000):
    """Wait until any known search-form control from ``sel`` is visible."""
    probes = [sel['member_id']]
    if _uses_plain_date_inputs(sel):
        probes.extend(k for k in (sel.get('dob'), sel.get('date_from')) if k)
    probes.append(sel['submit_btn'])

    combined = iframe.locator(probes[0])
    for selector in probes[1:]:
        combined = combined.or_(iframe.locator(selector))

    _wait_locator_visible_unless_login_needed(
        iframe, combined, timeout_ms=timeout_ms,
    )
    for selector in probes:
        try:
            if iframe.locator(selector).first.is_visible():
                return selector
        except Exception:
            continue
    return probes[0]


def wait_for_page_ready(page, payer_config=None, quiet=False):
    """Block until the claim-search iframe and form fields are visible.

    Uses the payer's ``selector_profile`` (legacy vs MUI) when probing for
    readiness.  Raises on failure so retry_with_backoff() can retry the step.
    Returns the FrameLocator for the search iframe.

    ``quiet``: when True, skip routine log lines (used during session probes).
    """
    page = _resolve_page(page)
    sel = get_selector_profile(payer_config)
    if not quiet:
        log_to_gui("  → Waiting for iframe...\n")
    if not is_claim_search_page(page):
        raise RuntimeError(
            "Browser tab is not on claim search — navigation shell only "
            f"({page.url})"
        )
    try:
        page.wait_for_selector(
            sel['iframe'], state='attached', timeout=_PAGE_READY_TIMEOUT_MS,
        )
    except Exception as e:
        raise RuntimeError(
            f"Claim search iframe not attached ({page.url}): {e}"
        ) from e
    iframe = page.frame_locator(sel['iframe'])
    _raise_if_iframe_login_needed(iframe)
    time.sleep(1)
    _raise_if_iframe_login_needed(iframe)
    try:
        matched = _wait_for_search_form_in_iframe(
            iframe, sel, timeout_ms=_PAGE_READY_TIMEOUT_MS,
        )
    except AvailitySessionInactiveError:
        raise
    except Exception as e:
        _raise_if_iframe_login_needed(iframe)
        raise RuntimeError(
            f"Claim search form did not become ready ({page.url}): {e}"
        ) from e
    if not quiet:
        log_to_gui(f"  ✓ Page ready ({matched})\n", "success")
    return iframe


def navigate_to_payer_page(page, payer_config):
    """Navigate to the payer's claim-search URL and click the HIPAA tab if required.

    Separating navigation from page-readiness lets each step use its own
    retry budget independently.

    Args:
        page:         Playwright Page object.
        payer_config: Entry from PAYER_CONFIG for the selected payer.
    """
    page = retry_with_backoff(
        "Acquire live page",
        lambda: get_live_page(page),
        attempts=3,
    )
    retry_with_backoff(
        "Navigate to search page",
        lambda: page.goto(payer_config['url'], wait_until='domcontentloaded', timeout=45000),
        attempts=3,
    )
    # The SPA's client-side routing settles implicitly: every branch below (and
    # the caller's wait_for_page_ready) blocks on a visible form control.
    sel = get_selector_profile(payer_config)
    if payer_config['uses_hipaa_tab']:
        def _click_hipaa():
            live_page = get_live_page(page)
            live_page.wait_for_selector(
                sel['iframe'], state='attached', timeout=20000,
            )
            iframe = live_page.frame_locator(sel['iframe'])
            hipaa_tab = iframe.locator(sel['hipaa_tab'])
            _wait_locator_visible_unless_login_needed(
                iframe, hipaa_tab, timeout_ms=20000,
            )
            hipaa_tab.first.click(timeout=15000)
            return live_page

        page = retry_with_backoff("Select HIPAA tab", _click_hipaa, attempts=3)
        log_to_gui("  ✓ HIPAA tab selected\n", "success")
        iframe = get_live_page(page).frame_locator(sel['iframe'])
        _wait_for_search_form_in_iframe(iframe, sel, timeout_ms=20000)
    elif _uses_plain_date_inputs(sel):
        # Plain-input payers without a separate HIPAA tab still need the form.
        iframe = get_live_page(page).frame_locator(sel['iframe'])
        _wait_for_search_form_in_iframe(iframe, sel, timeout_ms=20000)
    else:
        iframe = get_live_page(page).frame_locator(sel['iframe'])
        _wait_for_search_form_in_iframe(iframe, sel, timeout_ms=20000)
    return page


def _verify_payer_search_session(page, payer_config, *, quiet=False):
    """Confirm the Availity session can open the payer claim search (not a stale nav tab).

    Returns ``(True, page)`` when the search iframe and member-id field are reachable,
    else ``(False, page)`` (``page`` is best-effort live page for continued waiting).
    """
    if payer_config is None:
        return True, page
    try:
        wp = navigate_to_payer_page(page, payer_config)
        wait_for_page_ready(wp, payer_config=payer_config, quiet=quiet)
        return True, wp
    except AvailitySessionInactiveError:
        raise
    except Exception as e:
        log_to_gui(f"  ⚠️ Session check failed: {e}\n", "error")
        try:
            return False, get_live_page(page)
        except Exception:
            return False, page


def _await_user_session_recovery(page, payer_config, circuit_breaker, reason):
    """Prompt for re-login, then re-verify the payer claim-search session.

    Pauses the Playwright CDP connection while the user signs in on the
    Availity login page. Resume stays disabled until the homepage loads, then
    clicking Resume re-opens the CDP connection.

    Returns ``(recovered, page)``. ``recovered`` is False when the circuit breaker
    trips, the user cancels, the batch is stopped, or re-verify fails.
    """
    circuit_breaker.record_session_failure()
    if circuit_breaker.tripped:
        log_to_gui(
            f"⚠ Circuit breaker tripped: {circuit_breaker.trip_reason}\n",
            "error",
        )
        return False, page

    log_to_gui(
        "⚠ Session appears logged out. Awaiting user re-login...\n",
        "error",
    )
    log_to_gui(f"    Session check reason: {reason}\n", "error")
    log_to_gui(
        "  → Pausing CDP connection. Sign in until the Availity homepage loads.\n",
        "info",
    )
    ensure_playwright_disconnected()

    state.resume_event.clear()
    state.resume_decision = "cancel"
    if state.root is not None:
        state.root.after(0, lambda r=reason: _prompt_user_relogin(r))
    state.resume_event.wait()

    if state.resume_decision != "resume" or not state.is_running:
        log_to_gui(
            "⚠ User cancelled after session expiry.\n",
            "error",
        )
        return False, page

    log_to_gui("  → Resuming CDP connection...\n", "info")
    try:
        page = setup_browser()
    except Exception as e:
        log_to_gui(f"❌ Could not resume CDP connection: {e}\n", "error")
        return False, page

    log_to_gui("  → Re-verifying Availity session...\n", "info")
    try:
        ok, page = _verify_payer_search_session(page, payer_config, quiet=True)
    except AvailitySessionInactiveError:
        ok = False
    if not ok:
        log_to_gui(
            "❌ Session still not usable; stopping batch.\n",
            "error",
        )
        return False, page

    log_to_gui("✓ Session re-verified — resuming.\n", "success")
    if payer_config.get('uses_remittance'):
        try:
            _ensure_remit_page(page)
        except Exception as e:
            log_to_gui(
                f"  ⚠️ Could not reopen remittance viewer: {e}\n",
                "error",
            )
    circuit_breaker.record_session_recovery()
    return True, page


# ============================================================================
# SECTION 9 — FORM INTERACTION
# ============================================================================

def _ensure_checkbox_checked(iframe, selector, label='checkbox'):
    """Check a form checkbox when present and not already selected."""
    box = iframe.locator(selector).first
    box.wait_for(state='visible', timeout=10000)
    if not box.is_checked():
        box.check(force=True)
        log_to_gui(f"  ✓ Checked {label}\n", "success")


def _fill_plain_date_inputs(iframe, sel, dob, start_date, end_date):
    """Fill plain text date inputs (legacy HIPAA-tab or Aetna DateInput fields)."""
    if dob:
        dob_input = iframe.locator(sel['dob'])
        dob_input.click()
        dob_input.fill(dob)
    if start_date:
        from_input = iframe.locator(sel['date_from'])
        from_input.click()
        from_input.fill(start_date)
    if end_date:
        to_input = iframe.locator(sel['date_to'])
        to_input.click()
        to_input.fill(end_date)


# Wider StartDate/EndDate retry is for a known search miss or unmatched table,
# not a blank Search and not an in-flight wait banner.
_PORTAL_SEARCH_DATE_FALLBACK_OUTCOMES = frozenset({
    'invalid_member_id', 'not_found',
})

# Wait banners must not end the search. Cap each attempt so a hung portal
# still trips the existing blank-search → session-expired path.
SEARCH_RESPONSE_TIMEOUT_MS = 30000


class AvailitySessionInactiveError(Exception):
    """Availity session is no longer usable — prompt for re-login."""

    fatal = True

    def __init__(self, reason=''):
        self.reason = reason or (
            'Search returned no results table or payer alert after retries'
        )
        super().__init__(self.reason)


def should_retry_search_with_row_dates(
    from_aggregation, searched_start, searched_end, row_start, row_end,
):
    """True when a second search should use row StartDate/EndDate.

    Retry only after an invoice-aggregation search whose date window differs
    from the spreadsheet StartDate/EndDate (VisitDate fills a missing bound).
    """
    if not from_aggregation:
        return False
    row_start = normalize_search_date(row_start)
    row_end = normalize_search_date(row_end)
    if not row_start and not row_end:
        return False
    searched_start = normalize_search_date(searched_start)
    searched_end = normalize_search_date(searched_end)
    return (searched_start, searched_end) != (row_start, row_end)


def refill_service_search_dates(
    page, iframe, start_date, end_date, payer_config=None,
):
    """Replace Service From/To in place. Does not touch NPI or member fields."""
    start_date = normalize_search_date(start_date)
    end_date = normalize_search_date(end_date)
    if not start_date and not end_date:
        return False
    sel = get_selector_profile(payer_config)
    log_to_gui(
        f"  → Retrying search with row StartDate/EndDate: "
        f"{start_date} → {end_date}\n",
        "info",
    )
    if _uses_plain_date_inputs(sel):
        _fill_plain_date_inputs(iframe, sel, '', start_date, end_date)
        return True
    if start_date and end_date:
        fill_date_range(
            page, iframe, start_date, end_date,
            from_label=sel['service_from_date_label'],
            to_label=sel['service_to_date_label'],
        )
        return True
    if start_date:
        fill_mui_date_picker(
            page, iframe, sel['service_from_date_label'], start_date,
        )
        return True
    fill_mui_date_picker(
        page, iframe, sel['service_to_date_label'], end_date,
    )
    return True


_form_fill_last_error = ''


def _is_service_date_form_error(error_msg):
    """True when a form-fill failure is likely caused by service-date entry."""
    markers = (
        'Service To read-back',
        'Service From read-back',
        'Failed to fill Service',
        'Service date',
        'date group with',
    )
    return any(marker in (error_msg or '') for marker in markers)


def fill_search_form(page, iframe, row_data, payer_config=None, *, df=None):
    """Populate the claim search form with data from a CSV row.

    Uses safe_field() throughout to guard against NaN values.
    Date fields use the payer's selector profile (MUI spinbuttons or legacy inputs).

    Service From/To dates come from patient-cluster or invoice VisitDate
    aggregation when ``df`` is provided; otherwise row StartDate/EndDate/VisitDate
    is used.

    Returns True on success, False if a required field is empty or a
    Playwright action raises.
    """
    global _form_fill_last_error
    _form_fill_last_error = ''
    try:
        log_to_gui("  → Filling search form...\n")
        sel = get_selector_profile(payer_config)
        if (
            _payer_resets_via_edit_search(payer_config)
            and not _search_form_core_fields_empty(iframe, payer_config)
        ):
            log_to_gui(
                "  → Form still had prior values — clicking Clear Form...\n",
                "info",
            )
            _clear_search_form_and_wait(iframe, payer_config)
        member_id = safe_alt_patient_id(row_data, 'AltPatientID')
        raw_last = safe_field(row_data, 'Last_Name')
        raw_first = safe_field(row_data, 'First_Name')
        last_name, first_name = normalize_patient_names(raw_last, raw_first)
        if last_name != raw_last or first_name != raw_first:
            log_to_gui(
                f"  ℹ️ Name normalized: {raw_last!r}, {raw_first!r} → "
                f"{last_name!r}, {first_name!r}\n",
                "info",
            )
        dob = normalize_dob(safe_field(row_data, 'DOB'))
        start_date, end_date, from_aggregation = resolve_invoice_search_dates(
            df, row_data, safe_field, payer_config=payer_config,
        )
        if not start_date and not end_date:
            visit_raw = safe_field(row_data, 'VisitDate')
            start_raw = safe_field(row_data, 'StartDate')
            end_raw = safe_field(row_data, 'EndDate')
            invoice_raw = safe_invoice_number(row_data, 'InvoiceNumber', '')
            raise ValueError(
                "No valid service dates: invoice VisitDate aggregation and "
                "row StartDate/EndDate/VisitDate are missing or invalid "
                f"(InvoiceNumber={invoice_raw!r}, StartDate={start_raw!r}, "
                f"EndDate={end_raw!r}, VisitDate={visit_raw!r})"
            )
        if from_aggregation:
            from availity_app.search_grouping import (
                resolve_patient_cluster_search_dates,
                supports_patient_search_grouping,
            )
            cluster_dates = (
                resolve_patient_cluster_search_dates(df, row_data, safe_field)
                if supports_patient_search_grouping(payer_config)
                else ('', '')
            )
            source = (
                'patient cluster'
                if cluster_dates == (start_date, end_date)
                else 'invoice aggregation'
            )
            log_to_gui(
                f"  ℹ️ Service search dates ({source}): "
                f"{start_date} → {end_date}\n",
                "info",
            )
        elif not normalize_search_date(safe_field(row_data, 'StartDate')) or not normalize_search_date(
            safe_field(row_data, 'EndDate')
        ):
            log_to_gui(
                f"  ℹ️ Service search dates: {start_date} → {end_date} "
                f"(invalid StartDate/EndDate — using VisitDate when available)\n",
                "info",
            )

        # AltPatientID and Last_Name are required for a meaningful search
        if not member_id:
            raise ValueError("AltPatientID is empty or missing")
        if not last_name:
            raise ValueError("Last_Name is empty or missing")
        if not _uses_plain_date_inputs(sel) and not dob:
            raise ValueError("DOB is empty or missing")

        if sel.get('patient_is_subscriber'):
            _ensure_checkbox_checked(
                iframe, sel['patient_is_subscriber'], 'Patient is subscriber',
            )

        iframe.locator(sel['member_id']).fill(member_id)
        iframe.locator(sel['last_name']).fill(last_name)
        iframe.locator(sel['first_name']).fill(first_name)
        npi = resolve_provider_npi(payer_config)
        if npi:
            iframe.locator(sel['provider_npi']).fill(npi)
        if _uses_plain_date_inputs(sel):
            _fill_plain_date_inputs(iframe, sel, dob, start_date, end_date)
        else:
            if dob:
                fill_mui_date_picker(page, iframe, sel['patient_dob_label'], dob)
            if start_date and end_date:
                fill_date_range(
                    page, iframe, start_date, end_date,
                    from_label=sel['service_from_date_label'],
                    to_label=sel['service_to_date_label'],
                )
            elif start_date:
                fill_mui_date_picker(
                    page, iframe, sel['service_from_date_label'], start_date
                )
            elif end_date:
                fill_mui_date_picker(
                    page, iframe, sel['service_to_date_label'], end_date
                )
        return True
    except Exception as e:
        _form_fill_last_error = str(e)
        log_to_gui(f"  ❌ Form fill error: {e}\n", "error")
        return False


def _handle_form_fill_failure(
    page, iframe, df, row_index, row_data, output_folder, payer_config,
):
    """Handle form fill failure; reload once on service-date errors.

    Returns ``(outcome, page, iframe)`` where outcome is:
      ``continue`` — retry succeeded, proceed to search
      ``row_done`` — row handled (e.g. DOB skip)
      ``failed``   — row marked error
    """
    if not row_has_valid_dob(row_data, safe_field):
        page = _finish_row_without_claims(
            page, df, row_index, output_folder, payer_config,
            claim_status=DOB_NOT_FOUND_STATUS,
            automation_status='Skipped',
            success_log=f"  ✓ Row {row_index + 1} skipped ({DOB_NOT_FOUND_STATUS})",
        )
        return 'row_done', page, iframe

    error = _form_fill_last_error
    if _is_service_date_form_error(error):
        start_date, end_date, _ = resolve_invoice_search_dates(
            df, row_data, safe_field, payer_config=payer_config,
        )
        log_to_gui(
            "  → Form fill date error — reloading search form and retrying once...\n",
            "info",
        )
        try:
            page = _reload_and_navigate(page, payer_config)
            iframe = retry_with_backoff(
                "Wait for page ready (form date retry)",
                lambda: wait_for_page_ready(page, payer_config=payer_config),
                attempts=3,
            )
            if fill_search_form(page, iframe, row_data, payer_config, df=df):
                return 'continue', page, iframe
            error = _form_fill_last_error or error
        except Exception as retry_error:
            error = str(retry_error)
            log_to_gui(f"  ❌ Form date retry failed: {retry_error}\n", "error")

        capture_row_failure_artifact(
            page, output_folder, row_index,
            {
                'error': error,
                'stage': 'form_fill_date',
                'payer': state.current_payer_name or '',
                'invoice': safe_invoice_number(row_data, 'InvoiceNumber', ''),
                'start_date': start_date,
                'end_date': end_date,
                'cross_year': service_dates_cross_year(start_date, end_date),
            },
        )

    write_row_result(df, row_index, empty_claim_result('Form error'))
    safe_mark_row_status(df, row_index, 'Error', error or 'Form error')
    save_progress_results(df, output_folder, force=True)
    return 'failed', page, iframe


# Populated during submit_search_and_wait so row completion never re-queries a busy iframe.
_last_search_alert_message = ''

# Availity info banners that are always on the form — not a search result.
_INFORMATIONAL_ALERT_PHRASES = (
    'supports only commercial',
    'please use the hipaa',
    'lines of business',
)


def _is_informational_payer_alert(message):
    """True for static guidance banners that must not end the search wait."""
    if not message:
        return False
    lower = message.lower()
    phrases = _INFORMATIONAL_ALERT_PHRASES
    payer_config = state.selected_payer_config
    if payer_config:
        extra = payer_config.get('informational_alert_phrases') or ()
        phrases = phrases + tuple(extra)
    return any(phrase in lower for phrase in phrases)


def _iter_visible_payer_alert_texts(iframe, *, skip_informational=False):
    """Yield visible MUI alert messages inside the search iframe."""
    alert = iframe.locator(SELECTORS['payer_search_alert'])
    try:
        count = int(alert.count())
    except Exception:
        return
    if count <= 0:
        return
    for i in range(count):
        item = alert.nth(i)
        try:
            if not item.is_visible():
                continue
            msg = item.locator('.MuiAlert-message').first.text_content(timeout=500)
            if not msg:
                continue
            msg = msg.strip()
            if skip_informational and _is_informational_payer_alert(msg):
                continue
            yield msg
        except Exception:
            continue


def _read_visible_payer_alert_text(iframe, *, skip_informational=False):
    """Return the first visible MUI alert message inside the search iframe."""
    for msg in _iter_visible_payer_alert_texts(
        iframe, skip_informational=skip_informational,
    ):
        return msg
    return ''


def _classify_payer_alert_text(message):
    """Map known miss copy to a search outcome. Unrecognized text is ignored."""
    if not message:
        return None
    lower = message.lower()
    if 'subscriber' in lower and 'not found' in lower:
        return 'invalid_member_id'
    if 'member' in lower and ('not found' in lower or 'invalid' in lower):
        return 'invalid_member_id'
    if 'acknowledgement/not found' in lower:
        return 'not_found'
    if 'could not find any results' in lower:
        return 'not_found'
    if 'not found' in lower:
        return 'not_found'
    return None


def _gui_search_miss_outcome(message):
    """Return a GUI miss outcome when ``message`` contains a saved phrase."""
    if not message:
        return None
    haystack = message.casefold()
    for entry in get_search_miss_phrases():
        phrase = (entry.get('phrase') or '').strip()
        outcome = entry.get('outcome')
        if not phrase or outcome not in ('not_found', 'invalid_member_id'):
            continue
        if phrase.casefold() in haystack:
            return outcome
    return None


def _alert_search_miss_outcome(message):
    """GUI phrase first (all payers), then built-in known-miss copy."""
    return _gui_search_miss_outcome(message) or _classify_payer_alert_text(message)


def _known_search_miss_from_alerts(iframe):
    """Return ``invalid_member_id`` / ``not_found`` or None. Does not log."""
    for selector, outcome in (
        (SELECTORS['invalid_member_id_alert'], 'invalid_member_id'),
        (SELECTORS['claim_not_found_ack'], 'not_found'),
        (SELECTORS['claim_not_found_warning'], 'not_found'),
    ):
        try:
            loc = iframe.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                return outcome
        except Exception:
            continue
    for message in _iter_visible_payer_alert_texts(iframe, skip_informational=True):
        outcome = _alert_search_miss_outcome(message)
        if outcome:
            return outcome
    return None


def _log_payer_alert(message):
    if message:
        log_to_gui(f"  ℹ️ Payer alert: {message}\n", "info")


def _remember_search_miss_alert(iframe):
    global _last_search_alert_message
    matched = ''
    first = ''
    for message in _iter_visible_payer_alert_texts(iframe, skip_informational=True):
        if not first:
            first = message
        if _alert_search_miss_outcome(message):
            matched = message
            break
    message = matched or first
    if message:
        _last_search_alert_message = message
        _log_payer_alert(message)


def is_invalid_member_id_alert(iframe):
    """True when Availity shows subscriber/member id not found instead of results."""
    if _known_search_miss_from_alerts(iframe) != 'invalid_member_id':
        return False
    _remember_search_miss_alert(iframe)
    return True


def is_claim_not_found_alert(iframe):
    """True when Availity shows payer acknowledgement that claim is not in adjudication."""
    if _known_search_miss_from_alerts(iframe) != 'not_found':
        return False
    _remember_search_miss_alert(iframe)
    return True


def _claims_table_has_results(iframe):
    """True when #claimsTable is visible and has at least one data row."""
    try:
        if not iframe.locator(SELECTORS['results_table']).is_visible():
            return False
        count = _claims_table_body_rows(iframe).count()
        return isinstance(count, int) and count > 0
    except Exception:
        return False


def _resolve_search_outcome(iframe):
    """Return search outcome: invalid_member_id, not_found, results, or failed."""
    if _claims_table_has_results(iframe):
        return 'results'
    if is_invalid_member_id_alert(iframe):
        return 'invalid_member_id'
    if is_claim_not_found_alert(iframe):
        return 'not_found'
    try:
        if iframe.locator(SELECTORS['results_table']).is_visible():
            return 'results'
    except Exception:
        pass
    return 'failed'


def _search_response_snapshot(iframe):
    """Capture alert text and results-table identity so a re-search can wait for change."""
    alert = _read_visible_payer_alert_text(iframe, skip_informational=True)
    table_visible = False
    row_count = 0
    first_row_text = ''
    try:
        table_visible = bool(iframe.locator(SELECTORS['results_table']).is_visible())
    except Exception:
        table_visible = False
    if table_visible:
        try:
            rows = _claims_table_body_rows(iframe)
            row_count = rows.count()
            if row_count:
                first_row_text = normalize_results_cell_text(
                    rows.nth(0).inner_text(timeout=1000) or '',
                )
        except Exception:
            pass
    return (alert, table_visible, row_count, first_row_text)


def _search_response_ready(iframe):
    """True when the search has produced results or a known miss."""
    try:
        if iframe.locator(SELECTORS['results_table']).is_visible():
            return True
    except Exception:
        pass
    return _known_search_miss_from_alerts(iframe) is not None


def _wait_for_stale_search_to_clear(iframe, previous_snapshot, timeout_s=3.0):
    """Wait until a prior table/alert is gone or replaced after a new Search click."""
    if previous_snapshot is None:
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not state.is_running:
            return
        try:
            if not _search_response_ready(iframe):
                return
            if _search_response_snapshot(iframe) != previous_snapshot:
                return
        except Exception:
            return
        _interruptible_sleep(0.1)


def _wait_for_search_response(iframe, timeout_ms=SEARCH_RESPONSE_TIMEOUT_MS):
    """Poll until results or a known miss appear, up to ``timeout_ms``.

    Unrecognized MUI alerts (including wait banners) do not end the wait.
    Search-button disabled state is not used; it is not reliable on Availity.
    """
    deadline = time.time() + (timeout_ms / 1000.0)

    while time.time() < deadline:
        if not state.is_running:
            raise TimeoutError('Search wait interrupted by stop')
        if _search_response_ready(iframe):
            return

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        _interruptible_sleep(min(0.25, remaining))


def submit_search_and_wait(iframe, *, expect_fresh=None):
    """Click Search and wait for results table or a known miss.

    Wrapped in retry_with_backoff() because the response sometimes
    takes an extra moment to render after a fast network response.
    Each attempt waits up to SEARCH_RESPONSE_TIMEOUT_MS and returns as
    soon as a table or known miss appears. Unrecognized alerts are
    ignored. Three blank attempts return 'failed' without a wider-date
    retry.

    Returns 'results', 'not_found', 'invalid_member_id', or 'failed'.

    When ``expect_fresh`` is True (or auto-detected because a table/known
    miss is already visible), wait until that stale outcome clears or
    changes before classifying the new response.
    """
    global _last_search_alert_message
    try:
        log_to_gui("  → Submitting search...\n")
        previous_snapshot = None
        if expect_fresh is None:
            try:
                expect_fresh = _search_response_ready(iframe)
            except Exception:
                expect_fresh = False
        if expect_fresh:
            previous_snapshot = _search_response_snapshot(iframe)

        def _submit_once():
            global _last_search_alert_message
            _last_search_alert_message = ''
            iframe.locator(SELECTORS['submit_btn']).click()
            if previous_snapshot is not None:
                _wait_for_stale_search_to_clear(iframe, previous_snapshot)
            _wait_for_search_response(iframe, timeout_ms=SEARCH_RESPONSE_TIMEOUT_MS)
            outcome = _resolve_search_outcome(iframe)
            if outcome == 'failed':
                raise RuntimeError('Search response did not resolve to table or alert')
            return outcome

        outcome = retry_with_backoff("Submit search", _submit_once, attempts=3)
        if outcome == 'invalid_member_id':
            log_to_gui("  ℹ️ Invalid member / subscriber id\n", "info")
        elif outcome == 'not_found':
            log_to_gui("  ℹ️ Claim not found in adjudication system\n", "info")
        elif outcome == 'results':
            log_to_gui("  ✓ Search results loaded\n", "success")
            # Sort is applied once in process_one_row / callers — do not sort here.
            # aria-sort is often empty on Availity, so a second click toggles
            # back to descending and breaks multi-claim row indices.
        return outcome
    except Exception as e:
        log_to_gui(f"  ❌ Search failed: {e}\n", "error")
        return 'failed'


def apply_start_end_search_fallback(
    page, iframe, row_data, payer_config,
    *, from_aggregation, searched_start, searched_end,
):
    """Retry claim search in place with row StartDate/EndDate.

    Returns ``(did_retry, outcome)``. ``outcome`` is None when no retry ran.
    Does not fill NPI or member fields.
    """
    row_start, row_end = resolve_service_search_dates(row_data, safe_field)
    if not should_retry_search_with_row_dates(
        from_aggregation, searched_start, searched_end, row_start, row_end,
    ):
        return False, None
    try:
        refill_service_search_dates(
            page, iframe, row_start, row_end, payer_config,
        )
    except Exception as e:
        log_to_gui(f"  ⚠️ StartDate/EndDate refill failed: {e}\n", "error")
        return False, None
    outcome = submit_search_and_wait(iframe, expect_fresh=True)
    if _claims_table_has_results(iframe):
        outcome = 'results'
    if outcome == 'results':
        sort_claim_results_by_finalized_date(iframe)
    return True, outcome


def _dismiss_payer_search_alerts(iframe):
    """Close leftover MUI search alerts so the next submit is not already-ready."""
    alert = iframe.locator(SELECTORS['payer_search_alert'])
    try:
        count = alert.count()
    except Exception:
        return
    for i in range(count):
        item = alert.nth(i)
        try:
            if not item.is_visible():
                continue
            closer = item.locator(
                'button[aria-label="Close"], button.MuiAlert-closeButton, '
                '.MuiAlert-action button'
            )
            if closer.count() > 0:
                closer.first.click(timeout=1000)
        except Exception:
            continue


def _clear_service_search_dates(page, iframe, payer_config=None):
    """Clear Service From/To only. Leaves Provider NPI and member fields."""
    sel = get_selector_profile(payer_config)
    if _uses_plain_date_inputs(sel):
        try:
            if sel.get('date_from'):
                iframe.locator(sel['date_from']).fill('')
            if sel.get('date_to'):
                iframe.locator(sel['date_to']).fill('')
        except Exception:
            pass
        return
    try:
        from_group = _resolve_mui_date_group(iframe, sel['service_from_date_label'])
        to_group = _resolve_mui_date_group(iframe, sel['service_to_date_label'])
        _reset_stale_service_date_pickers(page, iframe, from_group, to_group)
    except Exception as e:
        log_to_gui(f"  ⚠️ Could not clear service dates: {e}\n", "error")


def _prepare_search_form_for_next_row(page, iframe, payer_config=None):
    """Clear stale search output in place. Does not reload or touch NPI."""
    log_to_gui(
        "  → Clearing stale search output for next row (keeping NPI)...\n",
        "info",
    )
    try:
        _dismiss_payer_search_alerts(iframe)
    except Exception:
        pass
    _clear_service_search_dates(page, iframe, payer_config)


# ============================================================================
# SECTION 10 — CLAIM LIST EXTRACTION
# ============================================================================

def _finalized_dates_look_ascending(iframe):
    """True when #claimsTable Finalized Date values appear in ascending order.

    Used when ``aria-sort`` is missing/empty (common on Availity) so we do not
    click the sort header a second time and toggle back to descending.
    """
    col_idx = _claims_table_finalized_date_col_index(iframe)
    if col_idx is None:
        return False
    rows = _claims_table_body_rows(iframe)
    count = rows.count()
    if count < 2:
        return True

    prev = None
    checked = 0
    for i in range(count):
        if checked >= 8:
            break
        try:
            raw = _read_finalized_date_from_results_row_data_field(rows.nth(i))
            if not raw:
                raw = _read_claims_table_row_cell(rows.nth(i), col_idx)
            if not raw or raw in ('--', 'N/A'):
                continue
            current = normalize_date(raw)
            if not current:
                continue
            try:
                current_ts = pd.to_datetime(current, errors='coerce')
            except Exception:
                continue
            if pd.isna(current_ts):
                continue
            if prev is not None and current_ts < prev:
                return False
            prev = current_ts
            checked += 1
        except Exception:
            continue
    return checked >= 2


def sort_claim_results_by_finalized_date(iframe):
    """Ensure the claim results table is sorted by Finalized Date ascending.

    Availity loads results in descending order. Returning from claim detail
    resets that order, so row indices must be read only after ascending sort.
    Clicks the header only when not already ascending. ``aria-sort`` is often
    empty on Availity, so date order is used as a fallback guard (a second
    click would toggle back to descending).
    """
    try:
        header = iframe.locator(SELECTORS['claims_finalized_date_header']).first
        header.wait_for(state='visible', timeout=10000)
        sort_attr = (header.get_attribute('aria-sort') or '').lower()
        dates_ascending = False
        if sort_attr != 'ascending':
            dates_ascending = _finalized_dates_look_ascending(iframe)
        already_ascending = sort_attr == 'ascending' or dates_ascending
        if already_ascending:
            return True

        log_to_gui("  → Sorting results by Finalized Date (ascending)...\n")

        def _click_sort_header():
            header.click(timeout=10000)
            iframe.locator('#claimsTable tbody tr').first.wait_for(
                state='visible', timeout=10000
            )

        retry_with_backoff("Sort by Finalized Date", _click_sort_header, attempts=2)
        log_to_gui("  ✓ Results sorted ascending\n", "success")
        return True
    except Exception as e:
        log_to_gui(f"  ⚠️ Could not sort results: {e}\n", "error")
        return False


def _claims_table_finalized_date_col_index(iframe):
    """Return 0-based #claimsTable column index for Finalized Date, or None."""
    try:
        header = iframe.locator(SELECTORS['claims_finalized_date_header']).first
        if header.count() > 0:
            idx = header.evaluate(
                """el => {
                    const row = el.closest('tr');
                    if (!row) return -1;
                    const headers = Array.from(
                        row.querySelectorAll('th[role="columnheader"], th')
                    );
                    return headers.indexOf(el);
                }"""
            )
            if isinstance(idx, int) and idx >= 0:
                return idx
    except Exception:
        pass

    for header_sel in (
        '#claimsTable thead tr[role="row"] th[role="columnheader"]',
        '#claimsTable thead th[role="columnheader"]',
        '#claimsTable thead th',
    ):
        try:
            headers = iframe.locator(header_sel)
            for i in range(headers.count()):
                label = (headers.nth(i).inner_text(timeout=2000) or '').strip().lower()
                if 'finalized' in label:
                    return i
        except Exception:
            continue
    return None


def _read_claims_table_row_cell(row_locator, col_idx):
    """Read visible text from a results-table body cell (td or gridcell)."""
    for cell_sel in (
        ':scope > td',
        ':scope > [role="gridcell"]',
        ':scope > [role="cell"]',
        'td',
        '[role="gridcell"]',
        '[role="cell"]',
    ):
        try:
            cells = row_locator.locator(cell_sel)
            if cells.count() > col_idx:
                raw = (cells.nth(col_idx).inner_text(timeout=3000) or '').strip()
                if raw:
                    return raw
        except Exception:
            continue
    return ''


def _read_finalized_date_from_results_row_data_field(row_locator):
    """MUI DataGrid rows may expose finalizedDate via data-field on cells."""
    for field in ('finalizedDate', 'finalized_date'):
        try:
            cell = row_locator.locator(f'[data-field="{field}"]')
            if cell.count() > 0:
                raw = (cell.first.inner_text(timeout=3000) or '').strip()
                if raw and raw not in ('--', 'N/A'):
                    return raw
        except Exception:
            continue
    return ''


def read_finalized_date_from_results_row(iframe, row_locator):
    """Read and normalize Finalized Date from one #claimsTable tbody row."""
    try:
        raw = _read_finalized_date_from_results_row_data_field(row_locator)
        if not raw:
            col_idx = _claims_table_finalized_date_col_index(iframe)
            if col_idx is None:
                log_to_gui(
                    "    ⚠️ Finalized Date column not found in results table\n",
                    "error",
                )
                return None
            raw = _read_claims_table_row_cell(row_locator, col_idx)
        if not raw or raw in ('--', 'N/A'):
            log_to_gui(
                "    ⚠️ Finalized Date empty in results table row\n",
                "error",
            )
            return None
        normalized = normalize_date(raw)
        log_to_gui(f"    • Results Finalized Date: {normalized}\n", "info")
        return normalized
    except Exception as e:
        log_to_gui(f"    ⚠️ Could not read results Finalized Date: {e}\n", "error")
        return None


def _peek_results_row_finalized_date(iframe, row_locator):
    """Quietly read Finalized Date from a results row (no GUI log)."""
    try:
        raw = _read_finalized_date_from_results_row_data_field(row_locator)
        if not raw:
            col_idx = _claims_table_finalized_date_col_index(iframe)
            if col_idx is None:
                return None
            raw = _read_claims_table_row_cell(row_locator, col_idx)
        if not raw or raw in ('--', 'N/A'):
            return None
        return normalize_date(raw)
    except Exception:
        return None


_RESULTS_CLAIM_NUMBER_DATA_FIELDS = (
    'claimNumber',
    'claim_number',
    'claimId',
    'claim_id',
    'patientControlNumber',
    'patient_control_number',
    'patientAccountNumber',
    'patient_account_number',
)
_RESULTS_BILLED_DATA_FIELDS = (
    'billedAmount',
    'billed_amount',
    'totalBilledAmount',
    'total_billed_amount',
)
_RESULTS_PAID_DATA_FIELDS = (
    'paidAmount',
    'paid_amount',
    'totalPaidAmount',
    'total_paid_amount',
)


def _claims_table_header_labels(iframe):
    """Return ordered header label strings for #claimsTable, or []."""
    for header_sel in (
        '#claimsTable thead tr[role="row"] th[role="columnheader"]',
        '#claimsTable thead th[role="columnheader"]',
        '#claimsTable thead th',
    ):
        try:
            headers = iframe.locator(header_sel)
            count = headers.count()
            if count <= 0:
                continue
            labels = []
            for i in range(count):
                label = (headers.nth(i).inner_text(timeout=2000) or '').strip().lower()
                labels.append(label)
            if any(labels):
                return labels
        except Exception:
            continue
    return []


def _claims_table_col_index_by_keywords(iframe, keywords, *, exclude_keywords=None):
    """Return first 0-based header index matching any keyword substring."""
    exclude_keywords = tuple(exclude_keywords or ())
    labels = _claims_table_header_labels(iframe)
    for i, label in enumerate(labels):
        if not label:
            continue
        if any(ex in label for ex in exclude_keywords):
            continue
        if any(kw in label for kw in keywords):
            return i
    return None


def _claims_table_identity_columns(iframe):
    """Resolve #claimsTable columns used for multi-claim row identity."""
    return {
        'claim_number': _claims_table_col_index_by_keywords(
            iframe,
            (
                'claim number',
                'claim #',
                'claim id',
                'patient control',
                'patient account',
                'account number',
                'transaction',
            ),
            exclude_keywords=('invoice', 'finalized', 'service date'),
        ),
        'billed': _claims_table_col_index_by_keywords(
            iframe,
            ('billed amount', 'billed', 'charge'),
            exclude_keywords=('paid', 'allowed', 'patient'),
        ),
        'paid': _claims_table_col_index_by_keywords(
            iframe,
            ('paid amount', 'paid', 'payment'),
            exclude_keywords=('billed', 'allowed', 'patient', 'copay', 'coinsurance'),
        ),
        'service_dates': _claims_table_service_dates_col_index(iframe),
    }


def _read_results_row_data_field(row_locator, field_names):
    """Read the first non-empty MUI data-field cell matching field_names."""
    for field in field_names:
        try:
            cell = row_locator.locator(f'[data-field="{field}"]')
            if cell.count() > 0:
                raw = (cell.first.inner_text(timeout=3000) or '').strip()
                if raw and raw not in ('--', 'N/A'):
                    return raw
        except Exception:
            continue
    return ''


def _read_results_row_identity_value(row_locator, col_idx, data_fields):
    """Read one identity value from data-field first, then column index."""
    raw = _read_results_row_data_field(row_locator, data_fields)
    if raw:
        return normalize_results_cell_text(raw)
    if col_idx is not None:
        return normalize_results_cell_text(
            _read_claims_table_row_cell(row_locator, col_idx)
        )
    return ''


def _peek_results_row_claim_number(iframe, row_locator, col_map=None):
    """Quietly read the Claim ID / TCN from a results row, or '' if absent."""
    col_map = col_map or _claims_table_identity_columns(iframe)
    try:
        return _read_results_row_identity_value(
            row_locator,
            col_map.get('claim_number'),
            _RESULTS_CLAIM_NUMBER_DATA_FIELDS,
        )
    except Exception:
        return ''


def _count_unique_matching_claims(iframe, row_indices):
    """Count distinct Claim IDs, treating duplicate result rows as one claim."""
    identities = set()
    for index in row_indices:
        row = _claim_row_locator_at_index(iframe, index)
        claim_id = _peek_results_row_claim_number(iframe, row)
        identities.add(f"claim:{claim_id}" if claim_id else f"row:{index}")
    return len(identities)


def _wait_for_claim_detail_identity(
    iframe, expected_claim_id, payer_config=None, timeout_ms=None,
):
    """Block until the detail panel shows the Claim ID of the row we clicked.

    Availity can leave the previous claim rendered for a moment after a row
    click, so validated payers confirm identity before scraping anything.
    """
    expected = str(expected_claim_id or '').strip()
    if not expected:
        raise RuntimeError("Results row did not expose a Claim ID")

    policy = _result_validation_policy(payer_config)
    timeout_ms = timeout_ms or policy.get('detail_identity_timeout_ms', 15000)
    selector = get_selector_profile(payer_config)['claim_id_panel']
    locator = iframe.locator(selector).first

    deadline = time.time() + (float(timeout_ms) / 1000.0)
    last_seen = ''
    last_error = None
    while time.time() < deadline:
        try:
            locator.wait_for(state='attached', timeout=min(1500, timeout_ms))
            last_seen = (locator.text_content(timeout=1500) or '').strip()
            if last_seen == expected:
                return last_seen
        except Exception as e:
            last_error = e
        time.sleep(0.05)

    detail = f"last seen {last_seen!r}" if last_seen else str(last_error or 'no value')
    raise RuntimeError(
        f"Claim detail identity mismatch: expected {expected!r}; {detail}"
    )


def _results_row_fingerprint(iframe, row_locator, idx, col_map=None):
    """Stable fingerprint for one matched #claimsTable row.

    Uses claim/TCN + billed/paid (+ service dates) when columns are available,
    otherwise normalized full-row text. Finalized Date is intentionally not
    used — same-day duplicate invoices share it and would collapse.
    """
    col_map = col_map or {}
    claim_number = _read_results_row_identity_value(
        row_locator,
        col_map.get('claim_number'),
        _RESULTS_CLAIM_NUMBER_DATA_FIELDS,
    )
    billed = _read_results_row_identity_value(
        row_locator,
        col_map.get('billed'),
        _RESULTS_BILLED_DATA_FIELDS,
    )
    paid = _read_results_row_identity_value(
        row_locator,
        col_map.get('paid'),
        _RESULTS_PAID_DATA_FIELDS,
    )
    service_dates = ''
    svc_col = col_map.get('service_dates')
    if svc_col is not None:
        service_dates = normalize_results_cell_text(
            _results_date_range_from_row(row_locator, svc_col)
            or _read_claims_table_row_cell(row_locator, svc_col)
        )

    row_text = ''
    # Always capture row text when amounts are unavailable so a shared
    # claim/account column cannot collapse distinct result rows.
    if not (billed or paid):
        try:
            row_text = row_locator.inner_text(timeout=3000) or ''
        except Exception:
            row_text = ''

    return build_results_row_fingerprint(
        claim_number=claim_number,
        billed=billed,
        paid=paid,
        service_dates=service_dates,
        row_text=row_text,
        idx=idx,
    )


def _claims_table_body_rows(iframe):
    """Locator for data rows in the claim search results table."""
    # Prefer direct tbody children so nested tables are not double-counted.
    for sel in (
        '#claimsTable > tbody > tr[role="row"]',
        '#claimsTable > tbody > tr',
        '#claimsTable tbody tr[role="row"]',
        '#claimsTable tbody tr',
    ):
        rows = iframe.locator(sel)
        if rows.count() > 0:
            return rows
    return iframe.locator('#claimsTable tbody tr')


def _claims_table_service_dates_col_index(iframe):
    """Return 0-based #claimsTable column index for Service Dates, or None."""
    idx = _claims_table_col_index_by_keywords(
        iframe,
        (
            'service date',
            'dates of service',
            'date of service',
            'date(s) of service',
            'from/to',
        ),
        exclude_keywords=('finalized', 'received', 'paid date'),
    )
    if idx is not None:
        return idx
    for header_sel in (
        '#claimsTable thead tr[role="row"] th[role="columnheader"]',
        '#claimsTable thead th[role="columnheader"]',
        '#claimsTable thead th',
    ):
        try:
            headers = iframe.locator(header_sel)
            for i in range(headers.count()):
                label = (headers.nth(i).inner_text(timeout=2000) or '').strip().lower()
                if 'service date' in label or 'of service' in label:
                    return i
        except Exception:
            continue
    return None


def _results_date_range_from_row(row_locator, service_dates_col):
    """Return normalised 'MM/DD/YYYY-MM/DD/YYYY' from a results-table row."""
    try:
        raw = _read_results_row_data_field(
            row_locator,
            (
                'serviceDates',
                'serviceDate',
                'service_dates',
                'dateOfService',
                'datesOfService',
            ),
        )
        if raw:
            parsed = parse_service_date_range(raw)
            if parsed:
                return parsed
        cell_text = ''
        if service_dates_col is not None:
            cell_text = _read_claims_table_row_cell(row_locator, service_dates_col)
        if cell_text:
            parsed = parse_service_date_range(cell_text)
            if parsed:
                return parsed
        for cell_sel in (':scope > td', 'td', '[role="gridcell"]', '[role="cell"]'):
            if service_dates_col is None:
                break
            date_cell = row_locator.locator(cell_sel).nth(service_dates_col)
            paragraphs = date_cell.locator('p')
            p_count = paragraphs.count()
            if p_count >= 2:
                from_d = normalize_date(
                    paragraphs.nth(0).text_content(timeout=3000).strip()
                )
                to_d = normalize_date(
                    paragraphs.nth(1).text_content(timeout=3000).strip()
                )
                return f"{from_d}-{to_d}"
            if p_count == 1:
                single_text = paragraphs.nth(0).text_content(timeout=3000).strip()
                parsed = parse_service_date_range(single_text)
                if parsed:
                    return parsed
                single = normalize_date(single_text)
                return f"{single}-{single}"
    except Exception:
        pass
    return None


def _should_filter_results_by_visit_date(payer_config):
    """True when results rows should be narrowed to the spreadsheet VisitDate."""
    cfg = resolve_line_level_config(payer_config)
    return bool(cfg.get('filter_results_by_visit_date'))


def _narrow_claim_indices_by_visit_date(iframe, indices, visit_date, payer_config):
    """Keep only #claimsTable rows whose Service Dates contain VisitDate."""
    if not indices or not _should_filter_results_by_visit_date(payer_config):
        return indices

    visit_date = coerce_visit_date(visit_date)
    if not visit_date:
        return indices

    col_idx = _claims_table_service_dates_col_index(iframe)
    if col_idx is None:
        log_to_gui(
            "  ⚠️ Cannot filter results by VisitDate — Service Dates column not found\n",
            "error",
        )
        return indices

    target = normalize_date_range(visit_date)
    rows = _claims_table_body_rows(iframe)
    narrowed = []

    for i in indices:
        row_range = _results_date_range_from_row(rows.nth(i), col_idx)
        if not row_range:
            continue
        row_range_norm = normalize_date_range(row_range)
        if visit_date_matches_line(target, row_range_norm, 'exact'):
            narrowed.append(i)
            log_to_gui(
                f"  ✓ Results row {i + 1} exact VisitDate match: {row_range_norm}\n",
                "success",
            )

    if not narrowed:
        scraped = []
        for i in indices:
            row_range = _results_date_range_from_row(rows.nth(i), col_idx)
            if row_range:
                scraped.append(normalize_date_range(row_range))
        log_to_gui(
            f"  ⚠️ No results row contains VisitDate {target} "
            f"({len(indices)} invoice match(es); scraped: {', '.join(scraped)})\n",
            "error",
        )
    elif len(narrowed) < len(indices):
        log_to_gui(
            f"  ℹ️ Narrowed {len(indices)} invoice match(es) to {len(narrowed)} "
            f"by VisitDate {target}\n",
            "info",
        )
    return narrowed


def _service_date_range_matches(target_range, row_range):
    """True when results Service Dates equal the aggregated bundle range."""
    if not target_range or not row_range:
        return False
    target = normalize_date_range(target_range)
    row = normalize_date_range(row_range)
    return target == row


def _results_billed_amount_from_row(row_locator, billed_col):
    """Return Billed Amount text from a #claimsTable data row."""
    return _read_results_row_identity_value(
        row_locator, billed_col, _RESULTS_BILLED_DATA_FIELDS,
    )


def _find_claim_row_indices_by_visit_date_and_amount(
    iframe, visit_date, amount,
):
    """0-based indices matching spreadsheet VisitDate and Amount.

    Service Dates must equal VisitDate (exact normalised range). Billed
    Amount is compared as currency so ``$109.44`` matches ``109.44``.
    """
    visit_date = coerce_visit_date(visit_date)
    if not visit_date or parse_amount(amount) is None:
        return []

    col_idx = _claims_table_service_dates_col_index(iframe)
    billed_col = _claims_table_identity_columns(iframe).get('billed')
    if col_idx is None or billed_col is None:
        log_to_gui(
            "    ⚠️ Service Dates or Billed Amount column not found "
            "in results table\n",
            "error",
        )
        return []

    indices = []
    rows = _claims_table_body_rows(iframe)
    for i in range(rows.count()):
        row = rows.nth(i)
        row_range = _results_date_range_from_row(row, col_idx)
        billed = _results_billed_amount_from_row(row, billed_col)
        if results_row_matches_visit_and_amount(
            row_range, billed, visit_date, amount,
        ):
            indices.append(i)
            log_to_gui(
                f"    ✓ VisitDate+Amount match row {i + 1}: "
                f"{normalize_date_range(row_range)} / {billed}\n",
                "success",
            )
    return indices


def _find_claim_row_indices_by_invoice(iframe, invoice_number):
    """0-based indices of results rows whose text contains invoice_number."""
    indices = []
    rows = _claims_table_body_rows(iframe)
    for i in range(rows.count()):
        try:
            text = rows.nth(i).inner_text(timeout=3000) or ''
            if invoice_number in text:
                indices.append(i)
        except Exception:
            continue
    return indices


def _find_claim_row_indices_by_service_dates(iframe, target_range):
    """0-based indices of results rows whose Service Dates match target_range."""
    col_idx = _claims_table_service_dates_col_index(iframe)
    if col_idx is None:
        log_to_gui(
            "    ⚠️ Service Dates column not found in results table\n",
            "error",
        )
        return []

    indices = []
    rows = _claims_table_body_rows(iframe)
    for i in range(rows.count()):
        row = rows.nth(i)
        row_range = _results_date_range_from_row(row, col_idx)
        if _service_date_range_matches(target_range, row_range):
            indices.append(i)
            log_to_gui(
                f"    ✓ Bundle match row {i + 1}: {row_range}\n",
                "success",
            )
    return indices


def _find_claim_row_indices_by_visit_date(iframe, visit_date):
    """0-based indices of results rows whose Service Dates cover VisitDate."""
    visit_date = coerce_visit_date(visit_date)
    if not visit_date:
        return []
    col_idx = _claims_table_service_dates_col_index(iframe)
    if col_idx is None:
        log_to_gui(
            "    ⚠️ Service Dates column not found in results table\n",
            "error",
        )
        return []

    target = normalize_date_range(visit_date)
    indices = []
    rows = _claims_table_body_rows(iframe)
    for i in range(rows.count()):
        row_range = _results_date_range_from_row(rows.nth(i), col_idx)
        if not row_range:
            continue
        row_range_norm = normalize_date_range(row_range)
        if visit_date_matches_line(target, row_range_norm, 'exact') or (
            visit_date_matches_line(target, row_range_norm, 'within_range')
        ):
            indices.append(i)
            log_to_gui(
                f"    ✓ VisitDate match row {i + 1}: {row_range_norm}\n",
                "success",
            )
    return indices


def _claim_row_locator_at_index(iframe, index):
    """Single results-table row locator by 0-based index."""
    return _claims_table_body_rows(iframe).nth(index)


def resolve_matching_claim_indices(iframe, df, invoice_number,
                                   visit_date=None, payer_config=None,
                                   narrow_by_visit_date=True, amount=None):
    """Find results rows by invoice text, else by aggregated Service Dates.

    When ``match_results_by_visit_and_amount`` is set (Aetna), match only
    Service Dates + Billed Amount against spreadsheet VisitDate + Amount.

    When ``filter_results_by_visit_date`` is enabled and
    ``narrow_by_visit_date`` is True, invoice matches are narrowed to rows
    whose Service Dates contain the spreadsheet VisitDate.

    Invoice-level harvest passes ``narrow_by_visit_date=False`` so every
    submission of the invoice is opened once; VisitDate is applied later
    against scraped service lines.

    If invoice text and the aggregated bundle range miss, match rows whose
    Service Dates equal or contain the spreadsheet VisitDate.

    Returns (indices, match_mode) where match_mode is 'invoice',
    'service_dates', 'visit_date', 'visit_amount', or '' when nothing matched.
    """
    try:
        if iframe.locator('#claimsTable tbody').count() == 0:
            raise Exception("Results table body not found")

        if payer_matches_results_by_visit_and_amount(payer_config):
            log_to_gui(
                f"  → Matching results by VisitDate {visit_date} "
                f"and Amount {amount}\n",
                "info",
            )
            indices = _find_claim_row_indices_by_visit_date_and_amount(
                iframe, visit_date, amount,
            )
            if indices:
                log_to_gui(
                    f"  ✓ Found {len(indices)} claim(s) by VisitDate + Amount\n",
                    "success",
                )
                return indices, 'visit_amount'
            log_to_gui(
                "  ⚠️ No results row matches VisitDate and Amount\n",
                "error",
            )
            return [], ''

        indices = _find_claim_row_indices_by_invoice(iframe, invoice_number)
        if indices:
            log_to_gui(
                f"  ✓ Found {len(indices)} claim(s) by invoice\n",
                "success",
            )
            if narrow_by_visit_date:
                indices = _narrow_claim_indices_by_visit_date(
                    iframe, indices, visit_date, payer_config,
                )
            if indices:
                return indices, 'invoice'
            return [], ''

        target_range = aggregate_invoice_visit_date_range(df, invoice_number)
        if target_range:
            visit_rows = len(invoice_row_indices(df, invoice_number))
            log_to_gui(
                f"  → Bundle claim fallback: Service Dates {target_range} "
                f"({visit_rows} collection row(s))\n",
                "info",
            )
            indices = _find_claim_row_indices_by_service_dates(iframe, target_range)
            if indices:
                log_to_gui(
                    f"  ✓ Found {len(indices)} claim(s) by service dates\n",
                    "success",
                )
                return indices, 'service_dates'

        if visit_date:
            log_to_gui(
                f"  → Matching results by VisitDate {visit_date}\n",
                "info",
            )
            indices = _find_claim_row_indices_by_visit_date(iframe, visit_date)
            if indices:
                log_to_gui(
                    f"  ✓ Found {len(indices)} claim(s) by VisitDate\n",
                    "success",
                )
                return indices, 'visit_date'

        log_to_gui(
            f"  ⚠️ No results row matches invoice, bundle range, or VisitDate\n",
            "error",
        )
        return [], ''
    except Exception as e:
        log_to_gui(f"  ⚠️ {e}\n", "error")
        return [], ''


def find_matching_claims(iframe, invoice_number):
    """Locate all result rows whose visible text contains invoice_number.

    Returns (locator, count).  count == 0 means the invoice was not found.
    Prefer resolve_matching_claim_indices() for bundle fallback.
    """
    try:
        if iframe.locator('#claimsTable tbody').count() == 0:
            raise Exception("Results table body not found")

        indices = _find_claim_row_indices_by_invoice(iframe, invoice_number)
        if not indices:
            raise Exception(f"Invoice {invoice_number} not found in results")

        rows = _claims_table_body_rows(iframe)
        count = len(indices)
        log_to_gui(f"  ✓ Found {count} claim(s)\n", "success")
        return rows, count
    except Exception as e:
        log_to_gui(f"  ⚠️ {e}\n", "error")
        return None, 0


# ============================================================================
# SECTION 11 — CLAIM DETAIL EXTRACTION
# ============================================================================

def _read_selector(iframe, selector, label, timeout=5000, *, quiet=False):
    """Read visible text from a single selector.  Returns '--' if absent.

    Prefixed with _ because it is an implementation detail used only by
    the extraction functions in this section.
    """
    try:
        return iframe.locator(selector).text_content(timeout=timeout).strip()
    except Exception:
        if not quiet:
            log_to_gui(f"    ⚠️ {label} not found\n", "error")
        return '--'


def _read_selector_checked(
    iframe, selector, label, timeout=5000, *, quiet=False,
    attempts=3, retry_delay=0.35,
):
    """Read one field, distinguishing found, explicitly blank, and failed.

    Only payers that declare a ``result_validation`` block use this; every
    other payer keeps the single-shot ``_read_selector`` above.
    """
    last_error = None
    saw_blank = False
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            locator = iframe.locator(selector).first
            attempt_timeout = timeout if attempt == 0 else min(timeout, 2000)
            locator.wait_for(state='attached', timeout=attempt_timeout)
            value = (locator.text_content(timeout=attempt_timeout) or '').strip()
            if is_placeholder(value):
                saw_blank = True
            else:
                return FieldRead.found(value)
        except Exception as e:
            last_error = e

        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_delay)))

    if saw_blank:
        return FieldRead.absent()
    error = str(last_error or f"{label} selector was unavailable")
    if not quiet:
        log_to_gui(f"    ⚠️ {label} read failed: {error}\n", "error")
    return FieldRead.failed(error)


def _visible_mui_date_group(iframe, picker_label):
    """Return the first visible MUI date picker group for the given accessible name."""
    candidates = iframe.get_by_role('group', name=picker_label)
    try:
        count = candidates.count()
    except Exception:
        count = 0
    for i in range(count):
        group = candidates.nth(i)
        try:
            if group.is_visible():
                return group
        except Exception:
            continue
    return _mui_date_group_by_legend(iframe, picker_label)


def _mui_date_group_by_legend(iframe, legend_text):
    """Resolve a date picker group scoped to its fieldset legend (From vs To)."""
    for root_sel in (
        'div.MuiPickersInputBase-root[role="group"]',
        '[role="group"].MuiPickersInputBase-root',
    ):
        group = iframe.locator(
            f'fieldset:has(legend:has-text("{legend_text}")) {root_sel}'
        ).first
        try:
            if group.count() > 0 and group.is_visible():
                return group
        except Exception:
            continue
    group = iframe.locator(
        f'fieldset:has(legend:has-text("{legend_text}")) '
        'div.MuiPickersInputBase-root[role="group"]'
    ).first
    group.wait_for(state='visible', timeout=5000)
    return group


def _resolve_mui_date_group(iframe, picker_label):
    """Resolve picker group: accessible role first, then fieldset legend."""
    try:
        group = _visible_mui_date_group(iframe, picker_label)
        group.wait_for(state='visible', timeout=5000)
        return group
    except Exception:
        return _mui_date_group_by_legend(iframe, picker_label)


def _commit_mui_date_group(page, iframe):
    """Blur the active date picker so React commits before touching another picker."""
    member = iframe.locator(SELECTORS['member_id'])
    member.click()
    member.wait_for(state='visible', timeout=3000)


def _normalize_mui_date_str(date_str):
    """Normalize and validate a date string as MM/DD/YYYY for MUI pickers."""
    parts = split_date_parts(date_str)
    if parts:
        date_str = f'{parts[0]}/{parts[1]}/{parts[2]}'
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
        raise ValueError(f'Invalid date format. Expected MM/DD/YYYY, got: {date_str}')
    return date_str


def _focus_mui_date_group_year(page, group):
    """Click the Year spinbutton to isolate focus in this picker group."""
    group.get_by_role('spinbutton', name='Year').click()
    time.sleep(0.05)


def _wait_for_patient_dob_picker(iframe, timeout=15000):
    """Wait until the Patient DOB picker group is visible."""
    _visible_mui_date_group(iframe, 'Patient Date of Birth').wait_for(
        state='visible', timeout=timeout
    )


def _mui_date_group_matches(group, date_str):
    """True when Month/Day/Year spinbuttons match the intended date."""
    month, day, year = date_str.split('/')
    for label, want in [('Month', month), ('Day', day), ('Year', year)]:
        try:
            shown = group.get_by_role('spinbutton', name=label).inner_text(timeout=2000).strip()
            if not shown.isdigit() or int(shown) != int(want):
                return False
        except Exception:
            return False
    return True


_MUI_DATE_TYPE_DELAY = 80


def _read_mui_date_group_value(group):
    """Read visible Month/Day/Year segments as MM/DD/YYYY, or None if unreadable."""
    parts = []
    for label in ('Month', 'Day', 'Year'):
        try:
            parts.append(
                group.get_by_role('spinbutton', name=label).inner_text(timeout=2000).strip()
            )
        except Exception:
            return None
    if len(parts) == 3:
        return f'{parts[0]}/{parts[1]}/{parts[2]}'
    return None


def _mui_date_group_is_empty(group):
    """True when Month/Day/Year segments are blank or the MM/DD/YYYY placeholder."""
    value = _read_mui_date_group_value(group)
    if not value:
        return True
    parts = value.split('/')
    if len(parts) != 3:
        return True
    return not all(part.isdigit() for part in parts)


def _clear_mui_date_section(page, section, name):
    """Backspace-clear one spinbutton section (Year, Day, or Month)."""
    width = _MUI_SECTION_WIDTHS.get(name, 4)
    section.click()
    time.sleep(0.05)
    for _ in range(width):
        page.keyboard.press('Backspace')
        time.sleep(0.02)


def _reset_mui_date_group(page, iframe, group):
    """Clear all spinbutton segments in a MUI date picker group."""
    for name in ('Year', 'Day', 'Month'):
        section = group.get_by_role('spinbutton', name=name)
        _clear_mui_date_section(page, section, name)
    _commit_mui_date_group(page, iframe)


def _service_date_pickers_need_reset(from_group, to_group, from_date, to_date):
    """True when coupled pickers still hold values from a prior search attempt."""
    if _mui_date_group_is_empty(from_group) and _mui_date_group_is_empty(to_group):
        return False
    from_shown = _read_mui_date_group_value(from_group)
    to_shown = _read_mui_date_group_value(to_group)
    return from_shown != from_date or to_shown != to_date


def _reset_stale_service_date_pickers(page, iframe, from_group, to_group):
    """Clear stale Service To / From pickers before refill (To first)."""
    log_to_gui('  → Clearing stale service dates before refill...\n', 'info')
    if not _mui_date_group_is_empty(to_group):
        _reset_mui_date_group(page, iframe, to_group)
    if not _mui_date_group_is_empty(from_group):
        _reset_mui_date_group(page, iframe, from_group)
    _commit_mui_date_group(page, iframe)


def _hidden_mui_date_input(group):
    """Return the hidden backing input for a MUI date picker group."""
    hidden = group.locator('input[aria-hidden="true"]')
    if hidden.count() == 0:
        hidden = group.locator('input.MuiPickersInputBase-input')
    return hidden.first


def _validate_mui_date_filled(group, date_str):
    """Confirm spinbuttons or hidden input reflect the intended date."""
    if _mui_date_group_matches(group, date_str):
        return
    expect(_hidden_mui_date_input(group)).to_have_value(date_str, timeout=3000)


def _wait_for_mui_date_group_match(group, date_str, timeout_ms=1500):
    """Poll spinbuttons until the intended date is visible or timeout."""
    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline:
        if _mui_date_group_matches(group, date_str):
            return True
        time.sleep(0.05)
    return _mui_date_group_matches(group, date_str)


_MUI_DATE_SEGMENT_ORDER = ('Year', 'Day', 'Month')


def _section_value_matches(section, expected):
    """True when the spinbutton section's visible text equals expected (numeric)."""
    try:
        shown = section.inner_text(timeout=1500).strip()
        return shown.isdigit() and int(shown) == int(expected)
    except Exception:
        return False


_MUI_SECTION_WIDTHS = {'Year': 4, 'Day': 2, 'Month': 2}


def _clear_and_fill_section(page, section, name, value, delay=_MUI_DATE_TYPE_DELAY):
    """Clear a pre-populated section with Backspace, then type fresh digits.

    Why Backspace-clear:
    - When a section holds a value (e.g. To's Year = today's year 2026), MUI
      enters replace-mode on the first keystroke and frequently drops the
      remaining digits, leaving the section showing just the first digit
      zero-padded (e.g. typing 2026 over 2026 lands on 0002).
    - Backspacing the section's width clears its value cleanly. Typing then
      fills the empty section digit-by-digit via MUI's shift behavior, the
      same path that works for empty pickers like Patient DOB.
    """
    _clear_mui_date_section(page, section, name)
    section.click()
    time.sleep(0.05)
    page.keyboard.type(value, delay=delay)
    time.sleep(0.08)


def _fill_mui_date_segments(page, group, month, day, year, delay=_MUI_DATE_TYPE_DELAY):
    """Fill Year → Day → Month, clearing each section with Backspace first."""
    year_section = group.get_by_role('spinbutton', name='Year')
    day_section = group.get_by_role('spinbutton', name='Day')
    month_section = group.get_by_role('spinbutton', name='Month')

    _clear_and_fill_section(page, year_section, 'Year', year, delay=delay)
    _clear_and_fill_section(page, day_section, 'Day', day, delay=delay)
    _clear_and_fill_section(page, month_section, 'Month', month, delay=delay)

    # Per-section verify+retry for any section that still doesn't match.
    for section, value, name in (
        (year_section, year, 'Year'),
        (day_section, day, 'Day'),
        (month_section, month, 'Month'),
    ):
        if _section_value_matches(section, value):
            continue
        for _ in range(2):
            _clear_and_fill_section(page, section, name, value, delay=delay + 20)
            if _section_value_matches(section, value):
                break


def _fill_mui_date_continuous(page, group, month, day, year, delay=_MUI_DATE_TYPE_DELAY):
    """Retry variant: same Backspace-clear flow with longer per-key delay."""
    _fill_mui_date_segments(page, group, month, day, year, delay=delay + 30)


def _fill_mui_date_group_year_only(page, group, year):
    """Set only the Year spinbutton (used before cross-year range completion)."""
    year_section = group.get_by_role('spinbutton', name='Year')
    _clear_and_fill_section(page, year_section, 'Year', year)


def _fill_mui_date_group_via_input(page, group, date_str, *, validate_after=True):
    """Fill a resolved picker group via its hidden backing input."""
    date_str = _normalize_mui_date_str(date_str)
    hidden = _hidden_mui_date_input(group)
    hidden.wait_for(state='attached', timeout=5000)
    hidden.click(force=True)
    time.sleep(0.05)
    page.keyboard.press('Control+a')
    page.keyboard.press('Delete')
    time.sleep(0.05)
    page.keyboard.type(date_str, delay=_MUI_DATE_TYPE_DELAY)
    hidden.evaluate(
        """(el, val) => {
            el.value = val;
            el.dispatchEvent(new InputEvent('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        date_str,
    )
    page.keyboard.press('Tab')
    _wait_for_mui_date_group_match(group, date_str)
    if validate_after:
        _validate_mui_date_filled(group, date_str)


def _fill_mui_date_group_robust(
    page,
    iframe,
    group,
    date_str,
    *,
    commit=True,
    commit_mode='blur',
    retries=2,
    validate_after=True,
):
    """Fill a pre-resolved MUI date group with escalating strategies."""
    date_str = _normalize_mui_date_str(date_str)
    month, day, year = date_str.split('/')
    strategies = ('segments', 'continuous', 'via_input')
    last_error = None

    for attempt in range(retries + 1):
        strategy = strategies[min(attempt, len(strategies) - 1)]
        try:
            if strategy == 'via_input':
                _fill_mui_date_group_via_input(
                    page, group, date_str, validate_after=validate_after,
                )
                return

            if strategy == 'segments':
                _fill_mui_date_segments(page, group, month, day, year)
            else:
                _fill_mui_date_continuous(page, group, month, day, year)

            if commit:
                if commit_mode == 'blur':
                    _commit_mui_date_group(page, iframe)
                else:
                    page.keyboard.press('Tab')
                    _wait_for_mui_date_group_match(group, date_str)

            if validate_after:
                _validate_mui_date_filled(group, date_str)
            return
        except Exception as e:
            last_error = e
            if attempt >= retries:
                break
            shown = _read_mui_date_group_value(group)
            detail = f' (shown: {shown})' if shown else ''
            log_to_gui(
                f'  ⚠️ Date group {strategy} attempt {attempt + 1} failed{detail}, '
                f'retrying...\n',
                'error',
            )

    shown = _read_mui_date_group_value(group)
    detail = f' (shown: {shown})' if shown else ''
    raise RuntimeError(
        f'Failed to fill date group with {date_str}{detail}: {last_error}'
    )


def fill_mui_date_picker(
    page,
    iframe,
    picker_label,
    date_str,
    retries=2,
    validate_after=True,
    commit_mode='tab',
    group=None,
):
    """Fill a MUI X DatePicker via Year/Day/Month spinbuttons.

    Retries: segment-by-segment, then YYYYMMDD continuous, then hidden-input fallback.
    commit_mode: 'tab' for single pickers (DOB), 'blur' to avoid focus bleed in ranges.
    """
    date_str = _normalize_mui_date_str(date_str)
    log_to_gui(f'  → {picker_label}: {date_str}\n', 'info')

    page.bring_to_front()
    resolved_group = group or _resolve_mui_date_group(iframe, picker_label)
    resolved_group.wait_for(state='visible', timeout=5000)
    resolved_group.scroll_into_view_if_needed()

    try:
        _fill_mui_date_group_robust(
            page,
            iframe,
            resolved_group,
            date_str,
            commit=True,
            commit_mode=commit_mode,
            retries=retries,
            validate_after=validate_after,
        )
    except RuntimeError as e:
        raise RuntimeError(
            f'Failed to fill {picker_label} with {date_str}: {e}'
        ) from e


def fill_mui_date_picker_via_input(
    page,
    iframe,
    picker_label,
    date_str,
    retries=2,
    validate_after=True,
    log_header=True,
):
    """Last-resort fill via the MUI hidden input inside the picker group.

    The input id is dynamic (e.g. :r2b:, :r2e:) — locate via group label, not by id.
    """
    parts = split_date_parts(date_str)
    if parts:
        date_str = f'{parts[0]}/{parts[1]}/{parts[2]}'
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
        raise ValueError(f'Invalid date format. Expected MM/DD/YYYY, got: {date_str}')

    if log_header:
        log_to_gui(f'  → {picker_label}: {date_str} (input fallback)\n', 'info')
    last_error = None
    group = None
    for attempt in range(retries + 1):
        try:
            page.bring_to_front()
            group = _visible_mui_date_group(iframe, picker_label)
            group.wait_for(state='visible', timeout=5000)
            group.scroll_into_view_if_needed()
            _fill_mui_date_group_via_input(
                page, group, date_str, validate_after=validate_after,
            )
            return
        except Exception as e:
            last_error = e
            if attempt >= retries:
                break
            log_to_gui(
                f'  ⚠️ DatePicker input fill attempt {attempt + 1} failed, retrying...\n',
                'error',
            )
            time.sleep(0.5)

    shown = _read_mui_date_group_value(group) if group is not None else None
    detail = f' (shown: {shown})' if shown else ''
    raise RuntimeError(
        f'Failed to fill {picker_label} via input with {date_str}{detail}: {last_error}'
    )


def fill_service_date_range(
    page,
    iframe,
    from_date,
    to_date,
    from_label=None,
    to_label=None,
):
    """Fill coupled MUI service-date range pickers with adaptive cross-year plans."""
    from_label = from_label or SELECTORS['service_from_date_label']
    to_label = to_label or SELECTORS['service_to_date_label']
    from_date = _normalize_mui_date_str(from_date)
    to_date = _normalize_mui_date_str(to_date)
    cross = service_dates_cross_year(from_date, to_date)

    log_to_gui(
        f'  → Service dates: {from_date} → {to_date}'
        f'{" (cross-year)" if cross else ""}\n',
        'info',
    )
    page.bring_to_front()

    from_group = _resolve_mui_date_group(iframe, from_label)
    to_group = _resolve_mui_date_group(iframe, to_label)
    to_group.scroll_into_view_if_needed()

    if _service_date_pickers_need_reset(from_group, to_group, from_date, to_date):
        _reset_stale_service_date_pickers(page, iframe, from_group, to_group)

    plans = service_date_fill_plans(from_date, to_date)
    for plan_idx, plan in enumerate(plans):
        order_label = ' then '.join(plan['order'])
        log_to_gui(
            f'  → Service date plan {plan_idx + 1}/{len(plans)}: '
            f'{plan["mode"]} ({order_label})\n',
            'info',
        )
        try:
            _execute_service_date_fill_plan(
                page, iframe, from_group, to_group, from_date, to_date, plan,
            )
        except Exception as e:
            log_to_gui(f'  ⚠️ Service date plan error: {e}\n', 'error')

        if _service_date_range_valid(from_group, to_group, from_date, to_date):
            log_to_gui(
                f'  ✓ Service dates set — From: '
                f'{_read_mui_date_group_value(from_group)} | To: '
                f'{_read_mui_date_group_value(to_group)} '
                f'(plan {plan_idx + 1})\n',
                'success',
            )
            return

        from_shown = _read_mui_date_group_value(from_group)
        to_shown = _read_mui_date_group_value(to_group)
        log_to_gui(
            f'  ⚠️ Service date read-back: From {from_shown} (want {from_date}), '
            f'To {to_shown} (want {to_date}) — trying next plan...\n',
            'error',
        )

    from_shown = _read_mui_date_group_value(from_group)
    to_shown = _read_mui_date_group_value(to_group)
    raise RuntimeError(
        f'Service From read-back: {from_shown} (want {from_date}); '
        f'Service To read-back: {to_shown} (want {to_date})'
    )


def _service_date_range_valid(from_group, to_group, from_date, to_date):
    """True when both service-date picker groups match the intended range."""
    return (
        _mui_date_group_matches(from_group, from_date)
        and _mui_date_group_matches(to_group, to_date)
    )


def _service_date_fields_ordered(order, from_group, to_group, from_date, to_date):
    """Return (group, date_str) pairs in the requested fill order."""
    field_map = {
        'from': (from_group, from_date),
        'to': (to_group, to_date),
    }
    return [field_map[name] for name in order]


def _execute_service_date_fill_plan(
    page, iframe, from_group, to_group, from_date, to_date, plan,
):
    """Run one service-date fill plan (robust segments, year-first, or hidden input)."""
    mode = plan['mode']
    fields = _service_date_fields_ordered(
        plan['order'], from_group, to_group, from_date, to_date,
    )

    if mode == 'year_first':
        for group, date_str in fields:
            year = date_str.split('/')[2]
            _focus_mui_date_group_year(page, group)
            _fill_mui_date_group_year_only(page, group, year)
        _commit_mui_date_group(page, iframe)
        for group, date_str in fields:
            _fill_mui_date_group_robust(
                page, iframe, group, date_str, commit_mode='blur',
            )
        return

    if mode == 'robust':
        for group, date_str in fields:
            _focus_mui_date_group_year(page, group)
            _fill_mui_date_group_robust(
                page, iframe, group, date_str, commit_mode='blur',
            )
        return

    if mode == 'hidden_input':
        for group, date_str in fields:
            _fill_mui_date_group_via_input(page, group, date_str)
            _commit_mui_date_group(page, iframe)
        return

    raise ValueError(f'Unknown service date fill plan mode: {mode!r}')


def fill_date_range(
    page,
    iframe,
    from_date,
    to_date,
    from_label=None,
    to_label=None,
):
    """Fill Service From / To without focus bleed between coupled MUI pickers."""
    fill_service_date_range(
        page, iframe, from_date, to_date, from_label=from_label, to_label=to_label
    )


def _read_first_selector(selectors, iframe, label, timeout=3000):
    """Try each selector in order and return the first non-empty result.

    Used for fields (Billed/Paid Amount) where Availity uses different
    attribute casing across environments — we try all known variants.
    """
    for sel in selectors:
        try:
            val = iframe.locator(sel).text_content(timeout=timeout).strip()
            if val:
                return val
        except Exception:
            continue
    log_to_gui(f"    ⚠️ {label} not found (tried {len(selectors)} selectors)\n", "error")
    return '--'


def _read_first_selector_checked(
    selectors, iframe, label, timeout=3000, *, attempts=3, retry_delay=0.35,
):
    """Fallback-selector read returning a structured outcome (validated payers)."""
    last_error = None
    saw_blank = False
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        attempt_timeout = timeout if attempt == 0 else min(timeout, 1500)
        for selector in selectors:
            try:
                locator = iframe.locator(selector).first
                locator.wait_for(state='attached', timeout=attempt_timeout)
                value = (locator.text_content(timeout=attempt_timeout) or '').strip()
                if is_placeholder(value):
                    saw_blank = True
                    continue
                return FieldRead.found(value)
            except Exception as e:
                last_error = e
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_delay)))

    if saw_blank:
        return FieldRead.absent()
    error = str(last_error or f"{label} selectors were unavailable")
    log_to_gui(
        f"    ⚠️ {label} read failed (tried {len(selectors)} selectors): "
        f"{error}\n",
        "error",
    )
    return FieldRead.failed(error)


def _read_claim_detail_status_checked(
    iframe, selectors, timeout=3000, *, attempts=3, retry_delay=0.35,
):
    """Read a visible, stable status from outside the mounted results table."""
    last_error = None
    saw_blank = False
    attempts = max(1, int(attempts))

    for attempt in range(attempts):
        deadline = time.time() + (float(timeout) / 1000.0)
        while time.time() < deadline:
            for selector in selectors:
                try:
                    candidates = iframe.locator(selector)
                    for index in range(candidates.count()):
                        candidate = candidates.nth(index)
                        if not candidate.is_visible():
                            continue
                        if candidate.evaluate(
                            "el => Boolean(el.closest('#claimsTable'))"
                        ):
                            continue
                        value = (candidate.text_content(timeout=1000) or '').strip()
                        if is_placeholder(value):
                            saw_blank = True
                            continue

                        # React may update the status panel just after Claim ID.
                        # Accept it only when two consecutive reads agree.
                        time.sleep(0.15)
                        confirmed = (
                            candidate.text_content(timeout=1000) or ''
                        ).strip()
                        if confirmed == value:
                            return FieldRead.found(value)
                        last_error = RuntimeError(
                            f"Claim Status changed from {value!r} "
                            f"to {confirmed!r} while loading"
                        )
                except Exception as e:
                    last_error = e
            time.sleep(0.1)
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(retry_delay)))

    if saw_blank:
        return FieldRead.absent()
    error = str(last_error or 'Claim Status selectors were unavailable')
    log_to_gui(f"    ⚠️ Claim Status read failed: {error}\n", "error")
    return FieldRead.failed(error)


def _result_validation_policy(payer_config=None):
    """Return extraction/validation settings for the active payer, if any."""
    return dict((payer_config or {}).get('result_validation', {}))


def _add_extraction_error(claim_data, message):
    """Attach a reliability error without overwriting extracted output values."""
    if not message:
        return
    errors = claim_data.setdefault('_extraction_errors', [])
    text = str(message).strip()
    if text and text not in errors:
        errors.append(text)


def _set_claim_field_read(claim_data, field, read):
    """Apply a ``FieldRead`` to output and its internal reliability metadata."""
    claim_data[field] = read.value
    claim_data.setdefault('_field_states', {})[field] = read.state
    errors = claim_data.setdefault('_field_errors', {})
    if read.state == FIELD_FAILED:
        errors[field] = read.error or 'portal field read failed'
    else:
        errors.pop(field, None)


def _set_denial_read(claim_data, read, *, required):
    """Apply a denial read and record whether strict validation requires it."""
    claim_data['Denial Reason'] = read.value
    claim_data['_denial_required'] = bool(required)
    claim_data['_denial_state'] = read.state
    claim_data['_denial_error'] = read.error


def _usable_finalized_date(value):
    """Return a normalized date string, or None when empty/placeholder."""
    raw = (value or '').strip()
    if not raw or raw in ('--', 'N/A'):
        return None
    return normalize_date(raw)


def resolve_finalized_date(
    effective_date=None, results_finalized_date=None, check_date=None,
):
    """Pick Finalized Date: Effective Date → results table → Check Date."""
    for candidate in (effective_date, results_finalized_date, check_date):
        resolved = _usable_finalized_date(candidate)
        if resolved:
            return resolved
    return '--'


def _reconcile_finalized_date_conflict(claim_data, results_finalized_date):
    """Prefer the detail Finalized Date, falling back to the results row.

    Only runs for payers with ``reconcile_finalized_date_conflicts`` (Molina),
    where the results table and the detail panel can legitimately disagree and
    the mismatch must be recorded rather than silently resolved.
    """
    results_value = _usable_finalized_date(results_finalized_date)
    detail_value = _usable_finalized_date(claim_data.get('Finalized Date'))

    if detail_value:
        _set_claim_field_read(
            claim_data, 'Finalized Date', FieldRead.found(detail_value),
        )
        if results_value and detail_value != results_value:
            _add_extraction_error(
                claim_data,
                f"Finalized Date conflict: detail {detail_value!r}, "
                f"results {results_value!r}",
            )
        return

    if results_value:
        _set_claim_field_read(
            claim_data, 'Finalized Date', FieldRead.found(results_value),
        )
        log_to_gui(
            f"    ℹ️ Detail Finalized Date unavailable; "
            f"using results row: {results_value}\n",
            "info",
        )


def _apply_finalized_date_priority(claim_data, results_finalized_date, payer_config):
    """Set Finalized Date for uses_results_finalized_date payers (Villagecaremax)."""
    if not payer_config:
        return
    if not payer_config.get('uses_results_finalized_date'):
        if payer_config.get('reconcile_finalized_date_conflicts'):
            _reconcile_finalized_date_conflict(claim_data, results_finalized_date)
        return
    effective = claim_data.pop('_effective_date', None)
    claim_data.pop('_effective_date_read', None)
    finalized = resolve_finalized_date(
        effective_date=effective,
        results_finalized_date=results_finalized_date,
        check_date=claim_data.get('Check Date'),
    )
    claim_data['Finalized Date'] = finalized
    if _usable_finalized_date(effective):
        log_to_gui(f"    • Finalized Date (Effective Date): {finalized}\n", "info")
    elif results_finalized_date and finalized == _usable_finalized_date(results_finalized_date):
        pass
    elif finalized != '--' and finalized == _usable_finalized_date(claim_data.get('Check Date')):
        log_to_gui(
            f"    ℹ️ Results Finalized Date missing; using Check Date: {finalized}\n",
            "info",
        )


def _read_service_dates_panel(iframe, sel):
    """Return ``(start, end)`` from the Service Dates panel, or ``(None, None)``.

    Only invoice-alignment payers read this panel, so the extra locator lookups
    stay off every other payer's per-claim path.
    """
    for selector in sel.get('service_dates_panel_selectors') or []:
        try:
            text = iframe.locator(selector).text_content(timeout=3000).strip()
        except Exception:
            continue
        if text and text != '--':
            start, end = parse_service_dates_panel_text(text)
            if start and end:
                log_to_gui(f"    • Service Dates: {start} - {end}\n", "info")
            return start, end
    return None, None


def _extract_claim_header_validated(iframe, payer_config, sel, policy):
    """Header scrape for payers with a ``result_validation`` block (Molina).

    Every panel read is retried and its outcome recorded, so
    ``validate_claim_results`` can fail a row whose extraction was incomplete
    rather than writing a silently wrong ``--``.
    """
    read_kwargs = {
        'attempts': policy.get('field_read_attempts', 3),
        'retry_delay': policy.get('field_retry_delay_s', 0.35),
    }
    skip_detail_finalized = bool(payer_config.get('uses_results_finalized_date'))

    field_reads = {
        'Claim ID': _read_selector_checked(
            iframe, sel['claim_id_panel'], 'Claim ID', **read_kwargs,
        ),
        'Claim Status': _read_claim_detail_status_checked(
            iframe,
            sel.get('claim_status_selectors') or [sel['claim_status_panel']],
            **read_kwargs,
        ),
        'Finalized Date': (
            FieldRead.absent()
            if skip_detail_finalized
            else _read_selector_checked(
                iframe, sel['finalized_date_panel'], 'Finalized Date',
                **read_kwargs,
            )
        ),
        'Check Number': _read_selector_checked(
            iframe, sel['check_number_panel'], 'Check Number', **read_kwargs,
        ),
        'Check Date': _read_selector_checked(
            iframe, sel['check_date_panel'], 'Check Date', **read_kwargs,
        ),
        'Billed Amount': _read_first_selector_checked(
            sel['billed_amount_selectors'], iframe, 'Billed Amount', **read_kwargs,
        ),
        'Paid Amount': _read_first_selector_checked(
            sel['paid_amount_selectors'], iframe, 'Paid Amount', **read_kwargs,
        ),
    }
    data = {field: read.value for field, read in field_reads.items()}
    data['_field_states'] = {
        field: read.state for field, read in field_reads.items()
    }
    data['_field_errors'] = {
        field: read.error for field, read in field_reads.items()
        if read.state == FIELD_FAILED
    }

    if skip_detail_finalized:
        effective_read = FieldRead.absent()
        for selector in sel.get('effective_date_selectors') or []:
            candidate = _read_selector_checked(
                iframe, selector, 'Effective Date', quiet=True, **read_kwargs,
            )
            if candidate.state == FIELD_FOUND:
                effective_read = candidate
                break
            if candidate.state == FIELD_FAILED:
                effective_read = candidate
        data['_effective_date'] = effective_read.value
        data['_effective_date_read'] = effective_read
    return data


def extract_claim_header(iframe, payer_config=None):
    """Scrape the summary panels on the open claim detail page.

    Returns a dict whose keys match OUTPUT_COLUMNS.  Invoice-alignment payers
    additionally get ``_service_date_start`` / ``_service_date_end`` from the
    Service Dates panel.  Payers with a ``result_validation`` block take a
    retrying read path that records each field's outcome.
    """
    sel = get_selector_profile(payer_config)
    policy = _result_validation_policy(payer_config)
    skip_detail_finalized = bool(
        payer_config and payer_config.get('uses_results_finalized_date')
    )
    if policy:
        data = _extract_claim_header_validated(iframe, payer_config, sel, policy)
        if resolve_line_level_config(payer_config).get('match_by_invoice_alignment'):
            start, end = _read_service_dates_panel(iframe, sel)
            data['_service_date_start'] = start
            data['_service_date_end'] = end
        log_to_gui(f"    • Claim ID: {data['Claim ID']}\n")
        log_to_gui(f"    • Status:   {data['Claim Status']}\n")
        if _usable_finalized_date(data.get('_effective_date')):
            log_to_gui(
                f"    • Effective Date: {normalize_date(data['_effective_date'])}\n",
                "info",
            )
        return data
    data = {
        'Claim ID':       _read_selector(iframe, sel['claim_id_panel'],       'Claim ID'),
        'Claim Status':   (
            _read_first_selector(sel['claim_status_selectors'], iframe, 'Claim Status')
            if sel.get('claim_status_selectors')
            else _read_selector(iframe, sel['claim_status_panel'], 'Claim Status')
        ),
        'Finalized Date': (
            '--'
            if skip_detail_finalized
            else _read_selector(
                iframe, sel['finalized_date_panel'], 'Finalized Date'
            )
        ),
        'Check Number':   _read_selector(iframe, sel['check_number_panel'],   'Check Number'),
        'Check Date':     _read_selector(iframe, sel['check_date_panel'],     'Check Date'),
        'Billed Amount':  _read_first_selector(sel['billed_amount_selectors'], iframe, 'Billed Amount'),
        'Paid Amount':    _read_first_selector(sel['paid_amount_selectors'],   iframe, 'Paid Amount'),
    }
    if resolve_line_level_config(payer_config).get('match_by_invoice_alignment'):
        start, end = _read_service_dates_panel(iframe, sel)
        data['_service_date_start'] = start
        data['_service_date_end'] = end
    if skip_detail_finalized:
        effective = '--'
        for selector in sel.get('effective_date_selectors') or []:
            effective = _read_selector(
                iframe, selector, 'Effective Date', quiet=True,
            )
            if _usable_finalized_date(effective):
                break
        data['_effective_date'] = effective
    log_to_gui(f"    • Claim ID: {data['Claim ID']}\n")
    log_to_gui(f"    • Status:   {data['Claim Status']}\n")
    if _usable_finalized_date(data.get('_effective_date')):
        log_to_gui(
            f"    • Effective Date: {normalize_date(data['_effective_date'])}\n",
            "info",
        )
    return data


def _line_expand_button_locator(row, sel):
    """Return the expand/toggle button for a line-level service row."""
    if sel.get('line_expand_button'):
        return row.locator(sel['line_expand_button']).first
    return row.locator('td').first.locator('button').first


def _is_line_data_row(row, service_dates_col=None, sel=None):
    """True for service-line data rows (expandable or bundle layout), not detail panels."""
    sel = sel or {}
    try:
        if sel.get('line_expand_button'):
            if row.locator(sel['line_expand_button']).count() > 0:
                return True
        elif row.locator('td').first.locator('button').count() > 0:
            return True
        if service_dates_col is not None:
            return _line_date_range_from_row(row, service_dates_col) is not None
    except Exception:
        pass
    return False


# Default td indices when thead labels cannot be read (legacy Healthfirst layout).
_LINE_TABLE_COL_DEFAULTS = {
    'service_dates': 3, 'paid': 6, 'billed': 7,
    'quantity': None, 'status': None,
}


def _is_hipaa_codes_header(label):
    """True when a line-table header is the HIPAA Codes column."""
    normalized = ' '.join((label or '').lower().split())
    return (
        ('hipaa' in normalized or 'hippaa' in normalized)
        and 'code' in normalized
    )


def _payer_reads_hipaa_codes(payer_config):
    """True when Denial Reason is looked up from the collapsed HIPAA Codes column."""
    if not payer_config:
        return False
    if payer_config.get('read_hipaa_codes'):
        return True
    return bool(resolve_line_level_config(payer_config).get('read_hipaa_codes'))


def _payer_denial_description_only(payer_config):
    """True when Denial Reason should omit the ``[CODE] - `` prefix."""
    if _payer_reads_hipaa_codes(payer_config):
        return True
    return bool(payer_config and payer_config.get('denial_description_only'))


def _include_denial_code(payer_config):
    """True when Denial Reason should keep the ``[CODE] - `` prefix."""
    return not _payer_denial_description_only(payer_config)


def _payer_resets_via_edit_search(payer_config):
    """True when the next-invoice reset uses Edit Search + Clear Form."""
    return bool(payer_config and payer_config.get('reset_via_edit_search'))


def _payer_returns_via_back_to_results(payer_config):
    """True when results-list return uses only the Back to Results link."""
    return bool(payer_config and payer_config.get('return_via_back_to_results'))


def _payer_trims_denial_first_sentence(payer_config):
    """True when Denial Reason should keep only the first sentence."""
    return bool(payer_config and payer_config.get('trim_denial_to_first_sentence'))


def _trim_denial_to_first_sentence(text):
    """Keep the first sentence of a denial description.

    Strips truncated UI chrome such as ``Show more...`` and stops at the
    first period, including that period. Placeholders are left unchanged.
    """
    cleaned = ' '.join(str(text or '').split()).strip()
    if not cleaned or cleaned in ('--', 'N/A'):
        return cleaned or '--'
    cleaned = re.sub(r'(?i)show more\.{0,3}', '', cleaned)
    cleaned = cleaned.replace('View More', '').replace('View Less', '')
    cleaned = ' '.join(cleaned.split()).strip()
    period_idx = cleaned.find('.')
    if period_idx >= 0:
        cleaned = cleaned[:period_idx + 1]
    return cleaned.strip() or '--'


def _finalize_denial_reason(result, payer_config=None):
    """Apply payer-specific Denial Reason cleanup before writing output."""
    if result == '--' or not _payer_trims_denial_first_sentence(payer_config):
        return result
    return _trim_denial_to_first_sentence(result)


def _is_blank_hipaa_codes(value):
    """True when a HIPAA Codes cell has no usable value."""
    text = ' '.join(str(value or '').split()).strip()
    return text in ('', '--', '-', 'n/a', 'na', 'none')


def _is_remark_codes_label(text):
    """True when text is only the Reason/Remark Codes heading, not a code value."""
    normalized = ' '.join(str(text or '').split()).casefold()
    return normalized in (
        'reason/remark codes', 'reason/remark code',
        'reason / remark codes', 'reason / remark code',
        'remark codes', 'remark code',
    )


def _is_amount_text(text):
    """True when text is a billed/paid money value (``$0.00``), not a code."""
    cleaned = ' '.join(str(text or '').split()).strip()
    return bool(re.match(r'^\$\d[\d,]*(\.\d+)?$', cleaned))


def _looks_like_remark_code_token(text):
    """True for CARC/RARC-style tokens (``G18``, ``Y3Z``, ``N130``, ``256``)."""
    token = ' '.join(str(text or '').split()).strip()
    if not token or _is_amount_text(token):
        return False
    return bool(
        re.match(r'^[A-Za-z]{1,5}\d+[A-Za-z0-9]*$', token)
        or re.match(r'^[A-Za-z]{1,3}-\d+[A-Za-z0-9]*$', token)
        or re.match(r'^\d{1,4}$', token)
    )


_REMARK_HEADING_RE = re.compile(
    r'reason\s*/\s*remark\s*codes?\s*:?\s*',
    re.I,
)


def _text_after_remark_heading(text):
    """If a blob includes the Reason/Remark heading, keep only the value after it.

    Expanded panels wrap DX and Remark in the same outer grid. Tokens before
    the heading (``M150``, ``M6281``) are diagnosis codes, not remarks.
    """
    raw = str(text or '')
    match = _REMARK_HEADING_RE.search(raw)
    if match:
        return raw[match.end():]
    return raw


def _remark_codes_from_blob(text):
    """Pull CARC/RARC tokens out of a mixed panel blob (heading + badge + codes)."""
    tokens = []
    seen = set()
    for part in re.split(r'[,;\s]+', str(text or '')):
        token = part.strip().rstrip('.')
        if (
            not token
            or token in seen
            or not _looks_like_remark_code_token(token)
        ):
            continue
        seen.add(token)
        tokens.append(token)
    return ', '.join(tokens)


def _usable_remark_text(text):
    """Return remark-code text, or '' for blank / heading / amount / UI badge."""
    section = _text_after_remark_heading(text)
    cleaned = ' '.join(str(section or '').split()).strip()
    if (
        not cleaned
        or _is_blank_hipaa_codes(cleaned)
        or _is_remark_codes_label(cleaned)
        or _is_amount_text(cleaned)
    ):
        return ''
    return _remark_codes_from_blob(cleaned)


def parse_hipaa_status_code(raw):
    """Return the numeric Status code from a HIPAA Codes value.

    ``F2:585`` → ``585``. A bare numeric value is returned as-is.
    """
    text = ' '.join(str(raw or '').split()).strip()
    if _is_blank_hipaa_codes(text):
        return ''
    if ':' in text:
        text = text.rsplit(':', 1)[-1].strip()
    return text


def _hipaa_remark_fallback_codes():
    """Status codes that expand Reason/Remark Codes (585 and 101)."""
    return {
        str(code).strip()
        for code, _desc in HIPAA_REMARK_FALLBACK_STATUSES
    }


def select_hipaa_raw_for_denial(raw_values):
    """Prefer a 585/101 HIPAA cell value; otherwise the first usable raw.

    Collapsed cells often list more than one code (``F2:107`` then ``F2:585``).
    Fan-out harvest must still expand remarks when 585 or 101 is present.
    """
    raws = []
    for raw in raw_values or []:
        text = ' '.join(str(raw or '').split()).strip()
        if text and not _is_blank_hipaa_codes(text):
            raws.append(text)
    if not raws:
        return ''
    fallback_codes = _hipaa_remark_fallback_codes()
    for raw in raws:
        if parse_hipaa_status_code(raw) in fallback_codes:
            return raw
    return raws[0]


def _hipaa_raws_from_cell(row, col_idx):
    """Return every HIPAA Codes paragraph in a collapsed line-table cell."""
    if col_idx is None:
        return []
    try:
        cell = row.locator('td').nth(col_idx)
        paragraphs = cell.locator('p')
        if paragraphs.count() > 0:
            raws = []
            for idx in range(paragraphs.count()):
                text = ' '.join(
                    (paragraphs.nth(idx).text_content(timeout=3000) or '').split()
                ).strip()
                if text:
                    raws.append(text)
            return raws
        text = ' '.join((cell.text_content(timeout=3000) or '').split()).strip()
        return [text] if text else []
    except Exception:
        return []


def _first_hipaa_code_from_cell(row, col_idx):
    """Return the HIPAA code used for Denial Reason lookup.

    Prefers 585 or 101 when either appears in the cell so harvest/fan-out
    expands Reason/Remark Codes. Otherwise uses the first paragraph.
    """
    return select_hipaa_raw_for_denial(_hipaa_raws_from_cell(row, col_idx))


def _resolve_line_table_columns(iframe, payer_config=None):
    """Map Service Dates / Paid / Billed columns from #lineLevelTable thead.

    Uses per-profile ``line_table_col_defaults`` when set (e.g. Aetna).
    Header text wins when present.  Disk cache reuses columns from prior runs.
    """
    sel = get_selector_profile(payer_config)
    payer_name = state.current_payer_name or ''
    output_folder = state.current_batch_output_folder
    cached_cols = None
    if payer_name and output_folder:
        cached_cols = get_line_table_columns(output_folder, payer_name)
        # Corrupt cache from short-label miss: paid==billed must not be reused.
        if (
            cached_cols
            and cached_cols.get('paid') is not None
            and cached_cols.get('paid') == cached_cols.get('billed')
        ):
            cached_cols = None
    defaults = (
        cached_cols
        if cached_cols
        else sel.get('line_table_col_defaults', _LINE_TABLE_COL_DEFAULTS)
    )
    # Seed with defaults first so a cache written before quantity/status
    # existed still yields those keys.
    cols = dict(_LINE_TABLE_COL_DEFAULTS)
    cols.update(defaults)
    table_id = sel.get('line_table', SELECTORS['line_table'])
    try:
        headers = iframe.locator(f'{table_id} thead tr[role="row"] th')
        for i in range(headers.count()):
            raw_label = (headers.nth(i).inner_text(timeout=2000) or '').strip().lower()
            # Collapse whitespace/newlines so "Paid\nAmount" and short labels match.
            label = ' '.join(raw_label.split())
            if 'service date' in label:
                cols['service_dates'] = i
            elif 'billed amount' in label or label == 'billed':
                cols['billed'] = i
            elif 'paid amount' in label or label == 'paid':
                cols['paid'] = i
            elif 'reason/remark' in label or 'remark code' in label:
                cols['remark_codes'] = i
            elif _is_hipaa_codes_header(label):
                cols['hipaa_codes'] = i
            elif label in ('quantity', 'qty', 'units') or 'quantity' in label:
                cols['quantity'] = i
            elif label in ('status', 'line status') or (
                'status' in label and 'claim' not in label
            ):
                cols['status'] = i
    except Exception:
        pass
    if payer_name and output_folder:
        persist_line_table_columns(output_folder, payer_name, cols)
    return cols


def _line_date_range_from_row(row, service_dates_col):
    """Return normalised 'MM/DD/YYYY-MM/DD/YYYY' for a line table row, or None."""
    try:
        date_cell = row.locator('td').nth(service_dates_col)
        cell_text = date_cell.inner_text(timeout=3000).strip()
        if cell_text:
            parsed = parse_service_date_range(cell_text)
            if parsed:
                return parsed

        paragraphs = date_cell.locator('p')
        p_count = paragraphs.count()
        if p_count >= 2:
            from_d = normalize_date(paragraphs.nth(0).text_content(timeout=3000).strip())
            to_d = normalize_date(paragraphs.nth(1).text_content(timeout=3000).strip())
            return f"{from_d}-{to_d}"
        if p_count == 1:
            single_text = paragraphs.nth(0).text_content(timeout=3000).strip()
            parsed = parse_service_date_range(single_text)
            if parsed:
                return parsed
            single = normalize_date(single_text)
            return f"{single}-{single}"
    except Exception:
        pass
    return None


def _line_cell_text(row, col_index):
    """Return stripped td text for ``col_index``, or '' when missing."""
    if col_index is None:
        return ''
    try:
        return row.locator('td').nth(col_index).text_content(timeout=3000).strip()
    except Exception:
        return ''


def _scrape_line_data_rows(iframe, payer_config=None):
    """Return every service-line row with amounts, date_range, quantity, status."""
    line_rows = []
    sel = get_selector_profile(payer_config)
    cols = _resolve_line_table_columns(iframe, payer_config)
    table_sel = sel.get('line_table', SELECTORS['line_table'])
    row_sel = sel.get('line_table_body_row', f'{table_sel} tbody tr[role="row"]')
    iframe.locator(table_sel).wait_for(state='visible', timeout=15000)
    rows = iframe.locator(row_sel)
    hipaa_col = cols.get('hipaa_codes')
    for idx in range(rows.count()):
        row = rows.nth(idx)
        if not _is_line_data_row(row, cols['service_dates'], sel):
            continue
        date_range = _line_date_range_from_row(row, cols['service_dates'])
        if not date_range:
            continue
        billed = row.locator('td').nth(cols['billed']).text_content(timeout=3000).strip()
        paid   = row.locator('td').nth(cols['paid']).text_content(timeout=3000).strip()
        line_rows.append({
            'row': row,
            'idx': idx,
            'billed': billed,
            'paid': paid,
            'date_range': date_range,
            'quantity': _line_cell_text(row, cols.get('quantity')),
            'status': _line_cell_text(row, cols.get('status')),
            'hipaa_codes': _first_hipaa_code_from_cell(row, hipaa_col),
        })
    return line_rows


def _sum_line_billed_amounts(line_rows):
    """Sum the billed column across scraped portal lines (blanks count as 0)."""
    total = 0.0
    for entry in line_rows:
        val = parse_amount(entry.get('billed'))
        total += val if val is not None else 0.0
    return total


def build_invoice_alignment_match_map(
    iframe, claim_data, df, invoice_number, payer_config=None,
):
    """Scrape lines once and map sorted invoice inputs to portal lines.

    Returns ``(match_map, line_rows)`` where ``match_map`` is keyed by df index.
    """
    sorted_inputs = get_sorted_invoice_rows(df, invoice_number)
    range_start = claim_data.get('_service_date_start')
    range_end = claim_data.get('_service_date_end')
    dates_aligned = invoice_service_dates_aligned(
        sorted_inputs, range_start, range_end,
    )
    if dates_aligned:
        log_to_gui(
            f"    ✓ Service Dates panel matches invoice VisitDate span "
            f"({range_start} - {range_end})\n",
            "success",
        )
    else:
        log_to_gui(
            f"    ℹ️ Service Dates panel "
            f"({range_start or '--'} - {range_end or '--'}) "
            f"does not match first/last sorted VisitDate — "
            f"using Amount/Units line filtering\n",
            "info",
        )

    line_rows = _scrape_line_data_rows(iframe, payer_config)
    log_to_gui(
        f"    → Scraped {len(line_rows)} portal line(s) for "
        f"{len(sorted_inputs)} invoice input row(s)\n",
    )

    header_billed = parse_amount(claim_data.get('Billed Amount'))
    header_paid = parse_amount(claim_data.get('Paid Amount'))
    line_billed_sum = _sum_line_billed_amounts(line_rows)
    if header_billed is not None and abs(line_billed_sum - header_billed) >= 0.02:
        log_to_gui(
            f"    ⚠️ Line billed sum (${line_billed_sum:.2f}) != "
            f"header Billed (${header_billed:.2f}) — continuing\n",
            "info",
        )

    ll_cfg = resolve_line_level_config(payer_config)
    match_map = match_invoice_lines_to_inputs(
        sorted_inputs, line_rows, dates_aligned=dates_aligned,
        header_billed=header_billed, header_paid=header_paid,
        panel_range_start=range_start, panel_range_end=range_end,
        enable_pool_tiers=ll_cfg.get('enable_pool_tiers', True),
        allow_split_tiebreak=ll_cfg.get('allow_split_tiebreak', False),
        max_combo=int(ll_cfg.get('max_combo', 3) or 3),
    )
    return match_map, line_rows


def resolve_aligned_line_for_row(claim_data, invoice_context, iframe, row_data,
                                 payer_config=None):
    """Ensure the invoice-alignment match map exists; return this row's entry.

    Returns ``{'line': ..., 'unresolved': bool, ...}``.
    """
    ctx = invoice_context or {}
    if ctx.get('_aligned_line_entry') is not None:
        return ctx['_aligned_line_entry']

    def _unresolved_entry():
        return {
            'line': None,
            'unresolved': True,
            'units_mismatch': False,
            'confidence': MATCH_FLAG_MANUAL_REVIEW,
        }

    match_map = claim_data.get('_invoice_line_match_map')
    if match_map is None:
        df = ctx.get('df')
        invoice = ctx.get('invoice_number') or safe_invoice_number(
            row_data, 'InvoiceNumber', '',
        )
        if df is None or not invoice:
            return _unresolved_entry()
        match_map, _lines = build_invoice_alignment_match_map(
            iframe, claim_data, df, invoice, payer_config=payer_config,
        )
        claim_data['_invoice_line_match_map'] = match_map

    row_index = ctx.get('spreadsheet_row_index')
    if row_index is None:
        return _unresolved_entry()
    return match_map.get(row_index, _unresolved_entry())


def find_all_lines_by_visit_date(iframe, visit_date, payer_config=None,
                                 invoice_context=None):
    """Return every line-level row whose service dates match visit_date.

    Each entry is a dict: row, idx, billed, paid, date_range.
    Matching uses ``LINE_LEVEL_CONFIG`` (and optional per-payer ``line_level``
    overrides).  When VisitDate does not match but the claim has exactly one
    service line and ``fallback_to_sole_line`` is enabled, that line is used.

    When ``invoice_context`` is supplied it feeds day-position matching and
    receives ``_line_unresolved`` if no line can be identified without guessing.
    """
    matches = []
    ctx = invoice_context or {}
    try:
        visit_date = coerce_visit_date(visit_date)
        target = normalize_date_range(visit_date)
        cfg = resolve_line_level_config(payer_config)
        all_lines = _scrape_line_data_rows(iframe, payer_config)
        log_to_gui(
            f"    → Searching {len(all_lines)} line(s) for VisitDate: {target}\n"
        )

        filtered, unresolved = filter_line_rows_by_visit_date(
            all_lines, visit_date, cfg=cfg,
            invoice_row_count=ctx.get('invoice_row_count'),
            invoice_row_occurrence=ctx.get('invoice_row_occurrence'),
            visit_date_duplicated=ctx.get('visit_date_duplicated', False),
        )
        if unresolved and invoice_context is not None:
            invoice_context['_line_unresolved'] = True
        seen_keys = set()
        for entry in filtered:
            dedupe_key = (entry['date_range'], entry['billed'], entry['paid'])
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            matches.append(entry)
            log_to_gui(
                f"    ✓ Line match #{len(matches)}: {entry['date_range']} "
                f"(Billed: {entry['billed']}, Paid: {entry['paid']}"
                f"{', HIPAA: ' + entry['hipaa_codes'] if entry.get('hipaa_codes') else ''})\n",
                "success",
            )

        if not matches:
            log_to_gui("    ⚠️ No matching line found\n", "error")
        elif len(matches) > 1:
            log_to_gui(
                f"    ℹ️ {len(matches)} line(s) for {target} — "
                f"amounts summed across {len(matches)} lines; "
                f"denials merged into one field\n",
                "info",
            )
    except Exception as e:
        log_to_gui(f"    ⚠️ Line table error: {e}\n", "error")
    return matches


def _format_currency_amount(value):
    """Format a numeric amount as ``$X.XX``."""
    return format_currency_amount(value)


def aggregate_line_match_amounts(matches, payer_config=None):
    """Sum billed/paid across matching line dicts.

    ``parse_amount`` None (blank / ``--``) is treated as 0.0 for summing.
    Healthfirst adjustment pairs use original billed once and net paid.
    Returns ``(billed_str, paid_str)`` formatted as ``$X.XX``.
    """
    billed, paid, _is_adjusted = aggregate_line_amounts_for_payer(
        matches, payer_config,
    )
    return billed, paid


def aggregate_line_hipaa_codes(matches):
    """Join unique HIPAA Codes from matching line dicts.

    Blank / ``--`` values are skipped. Codes are split on commas, semicolons,
    and newlines so multi-line cells still collapse to one field.
    Returns ``'--'`` when nothing usable was found.
    """
    seen = []
    for entry in matches or []:
        raw = entry.get('hipaa_codes') if isinstance(entry, dict) else ''
        text = ' '.join(str(raw or '').split()).strip()
        if _is_blank_hipaa_codes(text):
            continue
        for part in re.split(r'[,;\n]+', text):
            code = ' '.join(part.split()).strip()
            if _is_blank_hipaa_codes(code) or code in seen:
                continue
            seen.append(code)
    return ', '.join(seen) if seen else '--'


def find_line_by_visit_date(iframe, visit_date, occurrence=0, payer_config=None,
                            invoice_context=None):
    """Search the line-level table for the Nth row matching visit_date (0-based).

    Both the target date (from CSV) and the scraped dates are normalised to
    'MM/DD/YYYY-MM/DD/YYYY' before comparison to avoid false mismatches.

    When multiple lines share the VisitDate, billed/paid are summed across all
    matches (Healthfirst original/adjustment pairs use original billed once
    and net paid). The first original line's row locator is returned.

    Returns (row_locator, row_index, billed_str, paid_str, is_adjusted).
    Returns (None, -1, '--', '--', False) if no match is found.
    """
    matches = find_all_lines_by_visit_date(
        iframe, visit_date, payer_config, invoice_context=invoice_context,
    )
    row, idx, billed, paid, _hipaa, is_adjusted = _result_from_line_matches(
        matches, occurrence, payer_config,
    )
    if row is None:
        return None, -1, '--', '--', False

    if is_adjusted:
        log_to_gui(
            f"    ✓ Adjustment pair: Billed {billed}, Paid {paid} (net)\n",
            "success",
        )
    elif len(matches) > 1:
        log_to_gui(
            f"    ✓ Aggregated {len(matches)} line(s): "
            f"Billed {billed}, Paid {paid}\n",
            "success",
        )
    return row, idx, billed, paid, is_adjusted


def _result_from_line_matches(matches, occurrence=0, payer_config=None):
    """Return (row, idx, billed, paid, hipaa_codes, is_adjusted) from matches."""
    if not matches or occurrence >= len(matches):
        return None, -1, '--', '--', '--', False
    hipaa = aggregate_line_hipaa_codes(matches)
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, payer_config,
    )
    chosen = first_adjustment_original_line(matches, payer_config)
    if chosen is None:
        chosen = matches[occurrence]
    return (
        chosen.get('row'),
        chosen.get('idx', -1),
        billed,
        paid,
        hipaa,
        is_adjusted,
    )


# ============================================================================
# SECTION 12 — DENIAL REASON EXTRACTION (INLINE)
# ============================================================================

def collect_line_data_rows(iframe, visit_date=None, payer_config=None,
                           invoice_context=None):
    """Return service-line rows for denial/amount extraction.

    When ``LINE_LEVEL_CONFIG['filter_by_visit_date']`` is True (default), only
    rows whose service dates match ``visit_date`` are returned.  Invoice-
    alignment payers reuse the line already chosen for this row instead.
    """
    visit_date = coerce_visit_date(visit_date)
    ctx = invoice_context or {}
    cfg = resolve_line_level_config(payer_config)

    if cfg.get('match_by_invoice_alignment') and ctx.get('_aligned_line_entry'):
        entry = ctx['_aligned_line_entry']
        if entry.get('unresolved') or not entry.get('line'):
            return []
        return [entry['line']]

    try:
        all_lines = _scrape_line_data_rows(iframe, payer_config)
    except Exception as e:
        log_to_gui(f"    ⚠️ Line table error: {e}\n", "error")
        return []

    if not cfg.get('filter_by_visit_date', True):
        return all_lines

    filtered, unresolved = filter_line_rows_by_visit_date(
        all_lines, visit_date, cfg=cfg,
        invoice_row_count=ctx.get('invoice_row_count'),
        invoice_row_occurrence=ctx.get('invoice_row_occurrence'),
        visit_date_duplicated=ctx.get('visit_date_duplicated', False),
    )
    if unresolved and invoice_context is not None:
        invoice_context['_line_unresolved'] = True
    if visit_date:
        target = normalize_date_range(visit_date)
        log_to_gui(
            f"    → Line-level filter: {len(filtered)} of {len(all_lines)} "
            f"row(s) match VisitDate {target}\n"
        )
    return filtered


def _code_tokens_from_text(text):
    """Split a codes cell into usable tokens, skipping blank / ``--``."""
    tokens = []
    seen = set()
    for part in re.split(r'[,;\n]+', str(text or '')):
        code = ' '.join(part.split()).strip()
        if (
            not code
            or _is_blank_hipaa_codes(code)
            or _is_amount_text(code)
            or not _looks_like_remark_code_token(code)
            or code in seen
        ):
            continue
        seen.add(code)
        tokens.append(code)
    return tokens


def _usable_code_pairs(pairs):
    """Drop code/description pairs that are blank or ``--``."""
    usable = []
    for code, desc in pairs or []:
        if (
            _is_blank_hipaa_codes(code)
            or _is_blank_hipaa_codes(desc)
            or _is_amount_text(code)
            or _is_amount_text(desc)
            or not _looks_like_remark_code_token(code)
        ):
            continue
        usable.append((code, desc))
    return usable


_EXPLICIT_REMARK_PLACEHOLDERS = ('--', '-', 'n/a', 'na', 'none')


def _is_explicit_remark_placeholder(text):
    """True when Reason/Remark already shows ``--`` (not an empty/unloaded cell)."""
    cleaned = ' '.join(str(text or '').split()).strip()
    if not cleaned:
        return False
    return cleaned.casefold() in _EXPLICIT_REMARK_PLACEHOLDERS


def _line_cell_raw_text(row, col_idx, timeout=1000):
    """Return stripped text from a line-table cell, or None if unreadable."""
    if col_idx is None:
        return None
    try:
        cell = row.locator('td').nth(col_idx)
        return ' '.join(str(cell.text_content(timeout=timeout) or '').split()).strip()
    except Exception:
        return None


def _collapsed_remark_raw(iframe, matching_row, payer_config=None):
    """Raw Reason/Remark cell text from the collapsed line row, or None."""
    try:
        cols = _resolve_line_table_columns(iframe, payer_config)
    except Exception:
        return None
    return _line_cell_raw_text(matching_row, cols.get('remark_codes'))


def _expanded_remark_is_placeholder(matching_row):
    """True when the expanded panel's Reason/Remark value is already ``--``."""
    try:
        sibling = matching_row.locator('xpath=following-sibling::tr[1]')
        if sibling.count() == 0:
            return False
        text = str(sibling.first.inner_text(timeout=200) or '')
    except Exception:
        return False
    if not _REMARK_HEADING_RE.search(text):
        return False
    after = ' '.join(_text_after_remark_heading(text).split()).strip()
    if _is_explicit_remark_placeholder(after):
        return True
    first = after.split()[0] if after else ''
    return _is_explicit_remark_placeholder(first)


def _remark_is_placeholder_now(iframe, matching_row, payer_config=None):
    """True when collapsed or expanded Reason/Remark already shows ``--``."""
    if _is_explicit_remark_placeholder(
        _collapsed_remark_raw(iframe, matching_row, payer_config)
    ):
        return True
    return _expanded_remark_is_placeholder(matching_row)


def _wait_for_remark_codes_panel(iframe, matching_row, remark_sel, payer_config=None):
    """Wait for Reason/Remark Codes, or stop immediately when the value is ``--``.

    Returns ``placeholder``, ``ready``, or ``missing``.
    """
    if _remark_is_placeholder_now(iframe, matching_row, payer_config):
        return 'placeholder'
    try:
        matching_row.locator('xpath=following-sibling::tr[1]').first.wait_for(
            state='visible', timeout=500,
        )
    except Exception:
        pass
    if _remark_is_placeholder_now(iframe, matching_row, payer_config):
        return 'placeholder'
    waits = (
        lambda: matching_row.locator('xpath=following-sibling::tr[1]').locator(
            '.MuiGrid-root:has(p:has-text("Reason/Remark Codes"))'
        ).first.wait_for(state='visible', timeout=5000),
        lambda: iframe.locator(remark_sel).first.wait_for(state='visible', timeout=5000),
        lambda: matching_row.locator('xpath=following-sibling::tr[1]').first.wait_for(
            state='visible', timeout=3000,
        ),
    )
    for wait in waits:
        try:
            wait()
            return 'ready'
        except Exception:
            if _remark_is_placeholder_now(iframe, matching_row, payer_config):
                return 'placeholder'
    return 'missing'


def _format_denial_from_code_pairs(lines_by_date, include_code=True):
    """Format denial text from code/description pairs grouped by service date.

    When ``include_code`` is True (default, non-HIPAA payers):
      - One service date, one code:  ``[CODE] - Description``
      - One service date, many codes: ``[CODE] Description, [CODE] Description``
        (same visit date — always one line, never numbered 1/2)
      - Multiple service dates (bundle): one line per code —
        ``MM/DD/YYYY-CODE-Description`` (newline-separated)

    When ``include_code`` is False (Integra / Anthem / Empire / Healthfirst):
      - One service date, one code:  ``Description``
      - One service date, many codes: ``Description, Description``
      - Multiple service dates: ``MM/DD/YYYY-Description``

    Blank / ``--`` remark codes are ignored so only available codes remain.
    """
    active = {}
    for date_range, pairs in (lines_by_date or {}).items():
        usable = _usable_code_pairs(pairs)
        if usable:
            active[date_range] = usable
    if not active:
        return '--'

    unique_dates = list(active.keys())
    if len(unique_dates) == 1:
        pairs = active[unique_dates[0]]
        if len(pairs) == 1:
            code, desc = pairs[0]
            if include_code:
                return f"[{code}] - {desc}"
            return desc
        if include_code:
            return ', '.join(f"[{code}] {desc}" for code, desc in pairs)
        return ', '.join(desc for _code, desc in pairs)

    parts = []
    for date_range in sorted(unique_dates):
        visit = date_range.split('-')[0]
        for code, desc in active[date_range]:
            if include_code:
                parts.append(f"{visit}-{code}-{desc}")
            else:
                parts.append(f"{visit}-{desc}")
    return '\n'.join(parts)


def _extract_code_pairs_from_row(iframe, matching_row, payer_config=None):
    """Expand one service line and return (code, description) pairs."""
    sel = get_selector_profile(payer_config)
    expand_btn = _line_expand_button_locator(matching_row, sel)
    remark_sel = sel.get('remark_codes_grid', SELECTORS['remark_codes_grid'])
    if _is_explicit_remark_placeholder(
        _collapsed_remark_raw(iframe, matching_row, payer_config)
    ):
        log_to_gui(
            "    ℹ️ Reason/Remark is -- — skipping wait\n",
            "info",
        )
        return []

    expanded = False
    saw_panel = False
    try:
        log_to_gui("    → Expanding line for denial codes...\n")
        expand_btn.click()
        expanded = True
        panel_state = _wait_for_remark_codes_panel(
            iframe, matching_row, remark_sel, payer_config,
        )
        if panel_state == 'placeholder':
            log_to_gui(
                "    ℹ️ Reason/Remark is -- — skipping wait\n",
                "info",
            )
            return []
        saw_panel = panel_state == 'ready'

        remark_text = _usable_remark_text(
            _read_remark_codes(iframe, matching_row, payer_config)
        )
        if not remark_text:
            log_to_gui("    ⚠️ Remark codes panel not found\n", "error")
            return []

        log_to_gui(f"    • Codes: {remark_text}\n")
        return _usable_code_pairs(_lookup_code_pairs(iframe, remark_text))

    except Exception as e:
        log_to_gui(f"    ⚠️ Denial extraction error: {e}\n", "error")
        return []
    finally:
        if expanded:
            try:
                expand_btn.click()
                if saw_panel:
                    try:
                        iframe.locator(remark_sel).first.wait_for(
                            state='hidden', timeout=3000,
                        )
                    except Exception:
                        pass
            except Exception:
                pass


def extract_line_level_denial_reason(iframe, claim_status, payer_config=None,
                                    visit_date=None):
    """Build denial text from service lines matching the input VisitDate.

    HIPAA-code payers (Integra / Anthem / Empire) read the collapsed HIPAA
    Codes column (first code only) and look it up as type Status. When that
    Status is 585 Denied Charge or Non-covered Charge, or 101 Claim was
    processed as adjustment to previous claim, the line is expanded and
    Reason/Remark Codes become the denial reason. Other line-level payers
    expand the row and use Reason/Remark codes.

    Anthem keeps only the first sentence of Denial Reason (strips
    ``Show more...`` and later appeal language).

    Lines sharing a visit date are merged onto one line; different dates
    (bundle claims) each get their own ``date-code-description`` line.
    """
    lines_by_date = {}
    line_rows = collect_line_data_rows(
        iframe, visit_date, payer_config,
    )
    use_hipaa = _payer_reads_hipaa_codes(payer_config)
    skipped_fully_paid = False
    roles = (
        classify_adjustment_pair_roles(line_rows)
        if payer_detects_adjustment_pairs(payer_config)
        else [None] * len(line_rows)
    )
    groups_with_codes = set()

    multi_match = len(line_rows) > 1
    for row_info, role in zip(line_rows, roles):
        date_range = row_info['date_range']
        lines_by_date.setdefault(date_range, [])

        extract_ok = _should_extract_line_denial(
            claim_status, row_info, role,
        )
        if not extract_ok:
            if (
                use_hipaa
                and str(claim_status or '').upper() != 'PENDING'
                and _is_fully_paid_amounts(
                    row_info['billed'], row_info['paid'],
                )
            ):
                skipped_fully_paid = True
            continue

        if use_hipaa:
            pairs = _hipaa_pairs_with_remark_fallback(
                iframe, row_info['row'], row_info.get('hipaa_codes'),
                payer_config,
            )
        else:
            if multi_match and role != 'original':
                log_to_gui(
                    f"    → Expanding unpaid/underpaid line "
                    f"(Billed: {row_info['billed']}, Paid: {row_info['paid']}) "
                    f"for denial codes\n",
                    "info",
                )
            pairs = _extract_code_pairs_from_row(
                iframe, row_info['row'], payer_config,
            )

        for pair in _usable_code_pairs(pairs):
            if pair not in lines_by_date[date_range]:
                lines_by_date[date_range].append(pair)
            groups_with_codes.add(adjustment_pair_group_key(row_info))

    if payer_detects_adjustment_pairs(payer_config):
        _fallback_adjustment_denial_pairs(
            iframe, claim_status, line_rows, roles, lines_by_date,
            groups_with_codes, payer_config, use_hipaa,
        )

    result = _finalize_denial_reason(
        _format_denial_from_code_pairs(
            lines_by_date, include_code=_include_denial_code(payer_config),
        ),
        payer_config,
    )
    if result != '--':
        log_to_gui(f"    ✓ Denial reason: {result}\n", "success")
        return result

    if use_hipaa and skipped_fully_paid:
        log_to_gui(f"    ✓ Denial reason: {FULLY_PAID_DENIAL_REASON}\n", "success")
        return FULLY_PAID_DENIAL_REASON

    return result


def extract_denial_codes_inline(iframe, matching_row, _row_index, payer_config=None):
    """Format denial for one line row (bundle fallback path)."""
    use_hipaa = _payer_reads_hipaa_codes(payer_config)
    if use_hipaa:
        cols = _resolve_line_table_columns(iframe, payer_config)
        raw = _first_hipaa_code_from_cell(matching_row, cols.get('hipaa_codes'))
        pairs = _hipaa_pairs_with_remark_fallback(
            iframe, matching_row, raw, payer_config,
        )
    else:
        pairs = _extract_code_pairs_from_row(iframe, matching_row, payer_config)
    if not pairs:
        return '--'
    return _finalize_denial_reason(
        _format_denial_from_code_pairs(
            {'_single': _usable_code_pairs(pairs)},
            include_code=_include_denial_code(payer_config),
        ),
        payer_config,
    )


def _read_remark_codes(iframe, matching_row, payer_config=None):
    """Find and return the Reason/Remark Codes text from the expanded inline panel.

    Strategy:
      1. Aetna: read Reason/Remark column from expanded sibling row or same row.
      2. Walk sibling <tr> elements of the expanded row (this line's panel).
      3. Fallback: last non-blank MUI Reason/Remark grid in the line table.
      4. Legacy fallback: pre-MUI label + following-sibling <p> layout.
    Blank / heading-only cells are skipped so a leftover empty panel cannot
    hide this row's codes. Returns an empty string if nothing is found.
    """
    sel = get_selector_profile(payer_config)

    def _safe_text(locator, timeout=400):
        try:
            if locator.count() == 0:
                return ''
            return locator.first.inner_text(timeout=timeout) or ''
        except Exception:
            return ''

    def _codes_from_locator(locator):
        return _usable_remark_text(_safe_text(locator))

    def _value_from_label(label):
        if label.count() == 0:
            return None
        parent_codes = _codes_from_locator(label.first.locator('xpath=..'))
        if parent_codes:
            return parent_codes
        return _codes_from_locator(
            label.first.locator('xpath=following-sibling::p[1]')
        ) or ''

    def _value_from_grid_cell(cell):
        labeled = _value_from_label(cell.locator('p:has-text("Reason/Remark Codes")'))
        if labeled is not None:
            return labeled
        return ''

    try:
        try:
            remark_col = _resolve_line_table_columns(
                iframe, payer_config,
            ).get('remark_codes')
        except Exception:
            remark_col = sel.get('line_table_col_defaults', {}).get('remark_codes')
        if remark_col is not None:
            for offset in range(0, 5):
                target_row = (
                    matching_row
                    if offset == 0
                    else matching_row.locator(f'xpath=following-sibling::tr[{offset}]')
                )
                raw = _safe_text(target_row.locator('td').nth(remark_col))
                text = _usable_remark_text(raw)
                if text:
                    return text
                if offset == 0 and _is_explicit_remark_placeholder(raw):
                    return ''

        for offset in range(1, 5):
            sibling = matching_row.locator(f'xpath=following-sibling::tr[{offset}]')
            labeled = _value_from_label(
                sibling.locator('p:has-text("Reason/Remark Codes")')
            )
            if labeled:
                return labeled
            if labeled is not None:
                return ''
            cell = sibling.locator(
                '.MuiGrid-root:has(p:has-text("Reason/Remark Codes"))'
            )
            if cell.count() > 0:
                return _value_from_grid_cell(cell.last) or ''

        cells = iframe.locator(sel.get('remark_codes_grid', SELECTORS['remark_codes_grid']))
        count = cells.count()
        for idx in range(count - 1, -1, -1):
            text = _value_from_grid_cell(cells.nth(idx))
            if text:
                return text

        # Legacy layout (font-weight-bold label + adjacent <p>)
        line_table = iframe.locator(sel.get('line_table', SELECTORS['line_table']))
        headers = line_table.locator(
            'p.font-weight-bold:has-text("Reason/Remark Codes")'
        )
        count = headers.count()
        if count > 0:
            text = _codes_from_locator(
                headers.nth(count - 1).locator('xpath=following-sibling::p[1]')
            )
            if text:
                return text

        for offset in range(1, 5):
            sibling = matching_row.locator(f'xpath=following-sibling::tr[{offset}]')
            header = sibling.locator(
                'p.font-weight-bold:has-text("Reason/Remark Codes")'
            )
            if header.count() > 0:
                text = _codes_from_locator(
                    header.locator('xpath=following-sibling::p[1]')
                )
                if text:
                    return text

        return ''
    except Exception as e:
        log_to_gui(f"    ⚠️ Remark codes read error: {e}\n", "error")
        return ''


def _codes_table_row_selector(code, code_type='Remark'):
    """CSS selector for a #codesTable body row matching type + exact code."""
    escaped = str(code).replace('\\', '\\\\').replace('"', '\\"')
    if code_type == 'Status':
        return (
            f'#codesTable tbody tr:has(td:text-is("Status"))'
            f':has(td:text-is("{escaped}"))'
        )
    return (
        f'#codesTable tbody tr:has(td:text("{code_type}"))'
        f':has(td:text-is("{escaped}"))'
    )


def _matching_hipaa_remark_fallback(pairs):
    """Return the matching (code, desc) when Status code is 585 or 101."""
    fallback_codes = _hipaa_remark_fallback_codes()
    for code, desc in (pairs or []):
        status_code = str(code).strip()
        if status_code in fallback_codes:
            return (status_code, desc)
    return None


def _is_denied_charge_or_noncovered_status(pairs):
    """True when HIPAA Status lookup should expand Reason/Remark Codes."""
    return _matching_hipaa_remark_fallback(pairs) is not None


def _hipaa_pairs_with_remark_fallback(iframe, matching_row, hipaa_raw, payer_config):
    """HIPAA Status pairs, or Remark pairs for 585 Denied Charge / 101 Adjustment."""
    pairs = _extract_hipaa_denial_pairs(iframe, hipaa_raw)
    matched = _matching_hipaa_remark_fallback(pairs)
    if not matched:
        return pairs
    status_code, status_desc = matched
    log_to_gui(
        f"    → Status {status_code} {status_desc} — expanding line for Remark codes\n",
    )
    remark_pairs = _usable_code_pairs(
        _extract_code_pairs_from_row(
            iframe, matching_row, payer_config,
        )
    )
    if remark_pairs:
        return remark_pairs
    log_to_gui(
        "    ⚠️ Remark codes not found — keeping HIPAA Status description\n",
        "error",
    )
    return pairs


def _extract_hipaa_denial_pairs(iframe, hipaa_raw):
    """Look up Denial Reason from the first HIPAA Codes value (no expand)."""
    status_code = parse_hipaa_status_code(hipaa_raw)
    if not status_code:
        return []
    log_to_gui(f"    • HIPAA Status code: {status_code}\n")
    pairs = _lookup_code_pairs(iframe, status_code, code_type='Status')
    return pairs


def _lookup_code_pairs(iframe, remark_codes_text, code_type='Remark'):
    """Resolve codes to (code, description) pairs via the codes table.

    ``code_type`` is the Type cell to match (``Remark`` for expanded line
    denials, ``Status`` for HIPAA Codes lookups).
    """
    try:
        iframe.locator(SELECTORS['codes_table']).scroll_into_view_if_needed()
        time.sleep(0.5)
    except Exception:
        pass

    pairs = []
    for code in _code_tokens_from_text(remark_codes_text):
        desc = code
        try:
            row = iframe.locator(_codes_table_row_selector(code, code_type))
            if row.count() > 0:
                desc = row.first.locator('td').nth(2).text_content(timeout=3000).strip() or code
                log_to_gui(f"      • {code}: {desc}\n")
        except Exception:
            pass
        if _is_blank_hipaa_codes(desc):
            continue
        pairs.append((code, desc))
    return pairs


def should_extract_denial(claim_status, billed, paid, matching_row):
    """Decide whether inline denial code extraction is worth attempting.

    Returns False (skip) when:
      - Claim is PENDING — no adjudication data exists yet.
      - No line-level row was found — we have nothing to expand.
      - Claim was fully paid — billed == paid (both non-zero), so no denial.
      - Paid exceeds billed — overpayment, so no denial.
    """
    if claim_status.upper() == 'PENDING':
        log_to_gui("    ℹ️ PENDING — skipping denial codes\n", "info")
        return False
    if matching_row is None:
        return False
    billed_val = parse_amount(billed)
    paid_val   = parse_amount(paid)
    if billed_val is not None and paid_val is not None and billed_val > 0:
        if paid_val == billed_val:
            log_to_gui("    ℹ️ Fully paid — skipping denial codes\n", "info")
            return False
        if paid_val > billed_val:
            log_to_gui("    ℹ️ Overpayment — skipping denial codes\n", "info")
            return False
    return True


def _should_extract_line_denial(claim_status, entry, role=None):
    """Like ``should_extract_denial``, with Healthfirst adjustment-pair roles.

    ``original`` (first positive-paid line in a pair) is expanded even when
    billed equals paid. ``adjustment`` is skipped on the first pass; the
    matching negative line is expanded later only if the original has no codes.
    """
    if role == 'adjustment':
        return False
    if role == 'original':
        if str(claim_status or '').upper() == 'PENDING':
            log_to_gui("    ℹ️ PENDING — skipping denial codes\n", "info")
            return False
        if entry.get('row') is None:
            return False
        log_to_gui(
            "    → Expanding original line of adjustment pair "
            "for denial codes\n",
            "info",
        )
        return True
    return should_extract_denial(
        claim_status, entry.get('billed'), entry.get('paid'), entry.get('row'),
    )


def _denial_pairs_from_line_row(iframe, entry, payer_config, use_hipaa):
    """Expand one service line and return usable (code, description) pairs."""
    if use_hipaa:
        pairs = _hipaa_pairs_with_remark_fallback(
            iframe, entry['row'], entry.get('hipaa_codes'), payer_config,
        )
    else:
        pairs = _extract_code_pairs_from_row(
            iframe, entry['row'], payer_config,
        )
    return _usable_code_pairs(pairs)


def _fallback_adjustment_denial_pairs(
    iframe, claim_status, line_rows, roles, lines_by_date,
    groups_with_codes, payer_config, use_hipaa,
):
    """Expand negative pair mates when the original line had no remark codes."""
    if str(claim_status or '').upper() == 'PENDING':
        return
    for row_info, role in zip(line_rows, roles):
        if role != 'adjustment':
            continue
        key = adjustment_pair_group_key(row_info)
        if key in groups_with_codes:
            continue
        if row_info.get('row') is None:
            continue
        log_to_gui(
            "    → Original line had no remark codes — "
            "falling back to adjustment line\n",
            "info",
        )
        pairs = _denial_pairs_from_line_row(
            iframe, row_info, payer_config, use_hipaa,
        )
        if not pairs:
            continue
        date_range = row_info['date_range']
        lines_by_date.setdefault(date_range, [])
        for pair in pairs:
            if pair not in lines_by_date[date_range]:
                lines_by_date[date_range].append(pair)
        groups_with_codes.add(key)


def _is_fully_paid_amounts(billed, paid):
    """True when billed equals paid and billed is greater than zero."""
    billed_val = parse_amount(billed)
    paid_val = parse_amount(paid)
    return (
        billed_val is not None and paid_val is not None
        and billed_val > 0 and paid_val == billed_val
    )


def _payment_skips_remittance(billed, paid):
    """Return ``fully_paid`` / ``overpayment`` when remittance should be skipped."""
    billed_val = parse_amount(billed)
    paid_val = parse_amount(paid)
    if billed_val is None or paid_val is None or billed_val <= 0:
        return None
    if paid_val == billed_val:
        return 'fully_paid'
    if paid_val > billed_val:
        return 'overpayment'
    return None


def _harvest_remittance_amounts(header, lines):
    """Billed/paid used to decide remittance skip during snapshot harvest.

    Line-level payers (Aetna) materialize each service line. Header totals
    can look fully paid while a VisitDate line is only partially paid.
    Prefer a line that still needs a denial; otherwise use header amounts.
    Skip reversal / non-positive billed lines so they cannot open remittance.
    """
    for entry in lines or []:
        billed = entry.get('billed', '--')
        paid = entry.get('paid', '--')
        billed_val = parse_amount(billed)
        if billed_val is None or billed_val <= 0:
            continue
        if _payment_skips_remittance(billed, paid) is None:
            return billed, paid
    return (
        (header or {}).get('Billed Amount', '--'),
        (header or {}).get('Paid Amount', '--'),
    )


def _payer_uses_canned_paid_denial(payer_config):
    """True when the legacy Healthfirst canned_paid_denial flag is on."""
    return bool(payer_config and payer_config.get('canned_paid_denial'))


def _normalized_claim_status(claim_status):
    """Lowercased Claim Status with collapsed internal whitespace."""
    return ' '.join(str(claim_status or '').split()).casefold()


def _canned_paid_denial_reason(claim_status, payer_config=None, billed=None, paid=None):
    """Return canned Denial Reason for Paid / Overpayment / Pending / Finalized.

    Applied for every payer when Denial Reason is otherwise blank.
    ``payer_config`` is accepted for existing call sites and ignored.
    Finalized with billed == paid > 0 maps to Fully Paid, not In Process.
    """
    status = _normalized_claim_status(claim_status)
    if not status:
        return None
    if status == PAID_OVERPAYMENT_STATUS.casefold():
        return FULLY_PAID_OVERPAYMENT_DENIAL_REASON
    if status == 'paid':
        return FULLY_PAID_DENIAL_REASON
    if status == 'pending':
        return IN_PROCESS_DENIAL_REASON
    if status == 'finalized':
        if _is_fully_paid_amounts(billed, paid):
            return FULLY_PAID_DENIAL_REASON
        return IN_PROCESS_DENIAL_REASON
    return None


def _fill_blank_status_denial(claim_data, billed=None, paid=None):
    """Fill Denial Reason from Claim Status when the field is still blank."""
    if not claim_data or not _is_blank_denial_reason(claim_data.get('Denial Reason')):
        return
    if billed is None:
        billed = claim_data.get('Billed Amount')
    if paid is None:
        paid = claim_data.get('Paid Amount')
    canned = _canned_paid_denial_reason(
        claim_data.get('Claim Status'), billed=billed, paid=paid,
    )
    if not canned:
        return
    log_to_gui(
        f"    ℹ️ {claim_data.get('Claim Status')} — Denial Reason: {canned}\n",
        "info",
    )
    claim_data['Denial Reason'] = canned


def _is_claim_not_found_status(status):
    """True when Claim Status is a claim-not-found outcome (current or legacy)."""
    return status in (
        CLAIM_NOT_FOUND_STATUS,
        CLAIM_NOT_FOUND_LEGACY_STATUS,
        CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS,
        NO_CLAIMS_FOUND_ADVISED_STATUS,
    )


def _empty_claim_result_for_payer(claim_status, payer_config=None):
    """empty_claim_result; claim-not-found rows share Status and Denial Reason."""
    result = empty_claim_result(claim_status, payer_config=payer_config)
    if _is_claim_not_found_status(claim_status):
        result['Claim Status'] = CLAIM_NOT_FOUND_STATUS
        result['Denial Reason'] = CLAIM_NOT_FOUND_DENIAL_REASON
    return result


# ============================================================================
# SECTION 13 — DENIAL REASON EXTRACTION (REMITTANCE VIEWER)
# ============================================================================

def _has_claim_id(claim_data):
    """True when claim detail includes a usable Claim ID for remittance search."""
    claim_id = (claim_data.get('Claim ID') or '').strip()
    return bool(claim_id) and claim_id not in ('--', 'N/A')


def _is_blank_denial_reason(value):
    """True when a Denial Reason should not count as a real reason."""
    text = (value or '').strip()
    return text in ('', '--', 'N/A')


def _usable_remittance_denial(value):
    """True when remittance text should be kept instead of line-level fallback."""
    if _is_blank_denial_reason(value):
        return False
    return str(value).strip().casefold() != REMIT_NOT_FOUND_DENIAL.casefold()


def _is_unposted_field(value):
    """True when a claim-detail field is empty or a ``--`` / N/A placeholder."""
    text = str(value or '').strip()
    return text in ('', '--', 'N/A')


def _unposted_payment_skips_remittance(payer_config, claim_data, paid=None):
    """True when Villagecaremax should skip remittance (check/paid still ``--``).

    All three of Check Number, Check Date, and Paid Amount must be unposted.
    """
    if not payer_config or not payer_config.get('skip_remittance_when_unposted'):
        return False
    if not claim_data:
        return False
    paid_value = paid if paid is not None else claim_data.get('Paid Amount')
    return (
        _is_unposted_field(claim_data.get('Check Number'))
        and _is_unposted_field(claim_data.get('Check Date'))
        and _is_unposted_field(paid_value)
    )


def _claim_status_is_pending(claim_data):
    """True when claim-detail Status is PENDING (no adjudication yet)."""
    return str((claim_data or {}).get('Claim Status') or '').strip().upper() == (
        'PENDING'
    )


def _should_open_remittance(payer_config, claim_id, line_denial=None,
                            claim_data=None, paid=None):
    """True when the remittance viewer should be opened for this claim.

    Remittance runs whenever ``uses_remittance`` is on and Claim ID exists.
    PENDING claims skip the viewer (Denial Reason stays In Process).
    When ``remittance_when_remarks_blank`` is on, skip the viewer if
    Reason/Remark or HIPAA already produced a usable denial.
    Villagecaremax also skips when Check Number, Check Date, and Paid
    Amount are all still ``--``.
    Fully paid / overpayment skips happen later in ``enrich_claim_with_denial``.
    """
    if not payer_config or not payer_config.get('uses_remittance'):
        return False
    text = (claim_id or '').strip()
    if not text or text in ('--', 'N/A'):
        return False
    if _claim_status_is_pending(claim_data):
        return False
    if (
        payer_config.get('remittance_when_remarks_blank')
        and not _is_blank_denial_reason(line_denial)
    ):
        return False
    if _unposted_payment_skips_remittance(payer_config, claim_data, paid):
        return False
    return True


def _harvested_lines_have_denial(lines):
    """True when harvested service lines already have usable remark/HIPAA pairs."""
    for line in lines or []:
        if _usable_code_pairs(line.get('denial_pairs')):
            return True
    return False


def _remit_search_not_found(iframe):
    """True when the remittance viewer shows the no-results message."""
    try:
        return iframe.locator(SELECTORS['remit_not_found']).first.is_visible(timeout=1000)
    except Exception:
        return False


def _wait_remit_search_outcome(iframe, timeout_ms=30000):
    """Wait until remittance search shows results table or not-found message."""
    not_found = iframe.locator(SELECTORS['remit_not_found'])
    table = iframe.locator(SELECTORS['remit_table'])
    combined = not_found.or_(table)
    combined.first.wait_for(state='visible', timeout=timeout_ms)
    return _remit_search_not_found(iframe)


_REMIT_CHECK_DATE_RE = re.compile(r'(\d{1,2}/\d{1,2}/\d{4})')


def _normalize_remit_id(value):
    """Collapse whitespace for Claim # comparison."""
    text = ' '.join(str(value or '').split())
    if not text or text in ('--', 'N/A'):
        return ''
    return text.upper()


def _normalize_remit_check_date(value):
    """Normalize Remits/claim Check Date to MM/DD/YYYY (strips parentheses)."""
    text = ' '.join(str(value or '').split())
    if not text or text in ('--', 'N/A'):
        return ''
    match = _REMIT_CHECK_DATE_RE.search(text)
    if not match:
        return ''
    return _usable_finalized_date(match.group(1)) or ''


def _parse_remit_check_date(value):
    """Parse a Remits/claim Check Date to ``datetime.date``, or None."""
    text = _normalize_remit_check_date(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, '%m/%d/%Y').date()
    except ValueError:
        return None


def _resolve_remit_check_date_grace_days(grace_days):
    """Return a non-negative integer remits Check/EFT Date grace window."""
    default = get_remit_check_date_grace_days()
    if grace_days is None:
        return default
    try:
        resolved = int(grace_days)
    except (TypeError, ValueError):
        return default
    return resolved if resolved >= 0 else 0


def _select_remit_row_index(rows, claim_id, check_date=None, grace_days=None):
    """Pick the Remits data-row index for Claim # and Check/EFT Date.

    ``rows`` is a list of dicts with ``claim_id`` and ``check_date``.
    Claim # is required. When Check Date is present, Remits Check/EFT Date
    may equal the claim-detail date or fall up to ``grace_days`` calendar
    days after it (not before). Default window is
    ``REMIT_CHECK_DATE_GRACE_DAYS``. When several rows are in range, the
    closest date is preferred (exact match first).

    When Check Date is missing, a Claim #-only match is used only if
    exactly one remits row has that Claim #.
    """
    claim_norm = _normalize_remit_id(claim_id)
    if not claim_norm:
        return None

    expected_date = _parse_remit_check_date(check_date)
    if expected_date is None:
        matches = [
            idx for idx, row in enumerate(rows or [])
            if _normalize_remit_id(row.get('claim_id')) == claim_norm
        ]
        return matches[0] if len(matches) == 1 else None

    max_delta = _resolve_remit_check_date_grace_days(grace_days)
    best_idx = None
    best_delta = None
    for idx, row in enumerate(rows):
        if _normalize_remit_id(row.get('claim_id')) != claim_norm:
            continue
        row_date = _parse_remit_check_date(row.get('check_date'))
        if row_date is None:
            continue
        delta = (row_date - expected_date).days
        if delta < 0 or delta > max_delta:
            continue
        if best_delta is None or delta < best_delta:
            best_idx = idx
            best_delta = delta
    return best_idx


def _read_remit_search_rows(rows_locator):
    """Read Claim # and Check/EFT Date from each Remits data row."""
    rows = []
    count = rows_locator.count()
    for idx in range(count):
        row = rows_locator.nth(idx)
        claim_id = ''
        claim_link = row.locator(SELECTORS['remit_claim_link'])
        if claim_link.count() > 0:
            claim_id = claim_link.first.inner_text(timeout=5000).strip()
        check_date = ''
        date_cell = row.locator(SELECTORS['remit_check_date'])
        if date_cell.count() > 0:
            check_date = date_cell.first.inner_text(timeout=5000).strip()
        rows.append({'claim_id': claim_id, 'check_date': check_date})
    return rows


def _click_matching_remit_claim(iframe, claim_id, check_date=None):
    """Click the Claim # link on the Remits row that matches claim and check date.

    Remits Check/EFT Date may equal the claim-detail date or fall up to
    ``REMIT_CHECK_DATE_GRACE_DAYS`` later. When Check Date is missing, the
    row is used only if exactly one remits row has that Claim #. Returns
    True when a matching row was clicked.
    """
    table = iframe.locator(SELECTORS['remit_table']).first
    table.wait_for(state='visible', timeout=30000)
    rows_locator = table.locator(SELECTORS['remit_data_row'])
    rows_locator.first.wait_for(state='visible', timeout=20000)

    rows = _read_remit_search_rows(rows_locator)
    row_idx = _select_remit_row_index(rows, claim_id, check_date)
    if row_idx is None:
        if _normalize_remit_check_date(check_date):
            log_to_gui(
                f"    ℹ️ Remittance row not found for claim {claim_id} "
                f"check date {check_date}\n",
                "info",
            )
        else:
            log_to_gui(
                f"    ℹ️ Remittance row not found for claim {claim_id}\n",
                "info",
            )
        return False

    matched_date = rows[row_idx].get('check_date')
    if (
        _normalize_remit_check_date(matched_date)
        != _normalize_remit_check_date(check_date)
    ):
        log_to_gui(
            f"    ℹ️ Remittance row matched claim {claim_id} "
            f"check date {matched_date} (claim-detail {check_date})\n",
            "info",
        )

    claim_link = rows_locator.nth(row_idx).locator(SELECTORS['remit_claim_link']).first
    claim_link.wait_for(state='visible', timeout=20000)
    claim_link.click(timeout=15000)
    return True


def _remit_page_is_live(remit_page):
    """True when the persistent remittance tab is still usable."""
    if remit_page is None:
        return False
    try:
        return not remit_page.is_closed()
    except Exception:
        return False


def _prepare_remit_search_form(remit_page):
    """Ensure the remittance Claim search input is visible; click Claim tab if needed."""
    remit_page.wait_for_selector(SELECTORS['iframe'], state='attached', timeout=30000)
    iframe = remit_page.frame_locator(SELECTORS['iframe'])
    search_input = iframe.locator(SELECTORS['remit_search_input'])
    try:
        if search_input.first.is_visible(timeout=2000):
            return iframe
    except Exception:
        pass

    claim_tab = iframe.locator(SELECTORS['remit_claim_tab'])
    claim_tab.first.wait_for(state='visible', timeout=20000)
    claim_tab.first.click(timeout=15000)
    search_input.first.wait_for(state='visible', timeout=20000)
    return iframe


def _goto_remit_home_fallback(remit_page):
    """Reload remittance home and land on the Claim search form. Keep the tab open."""
    remit_page.goto(
        REMITTANCE_URLS['home'], wait_until='domcontentloaded', timeout=45000,
    )
    return _prepare_remit_search_form(remit_page)


def _clear_remit_applied_filters(iframe):
    """Click every Applied Search Terms Remove Filter chip until none remain."""
    group = iframe.locator(SELECTORS['remit_applied_filters'])
    remove_btn = group.locator(SELECTORS['remit_remove_filter'])
    for _ in range(10):
        if remove_btn.count() == 0:
            return
        btn = remove_btn.first
        try:
            if not btn.is_visible(timeout=1000):
                return
        except Exception:
            return
        btn.click(timeout=5000)


def _reset_remit_search(remit_page, *, from_claim_details=False):
    """Return the remittance tab to an empty Claim search. Keep the tab open.

    After claim-details, click Back then clear Applied Search Terms chips.
    When already on search (not-found / unmatched row), only clear chips.
    If Back or clear fails, fall back to the remittance home URL.
    """
    if not _remit_page_is_live(remit_page):
        return
    try:
        iframe = remit_page.frame_locator(SELECTORS['iframe'])
        if from_claim_details:
            back = iframe.locator(SELECTORS['remit_back_link'])
            back.first.wait_for(state='visible', timeout=20000)
            back.first.click(timeout=15000)
            iframe.locator(SELECTORS['remit_search_input']).first.wait_for(
                state='visible', timeout=30000,
            )
        _clear_remit_applied_filters(iframe)
        return
    except Exception as e:
        log_to_gui(
            f"    ⚠️ Remittance Back/clear failed ({e}); reloading viewer home\n",
            "error",
        )
    try:
        _goto_remit_home_fallback(remit_page)
    except Exception as e:
        log_to_gui(f"    ⚠️ Remittance home fallback failed: {e}\n", "error")


def _recycle_open_remit_tab(claim_page):
    """Reload remittance home if that tab is already open; do not open a new one.

    On home reload failure, close the dead tab and reopen a single remittance
    tab. Brings the claim-search tab back to the front afterward.
    """
    remit_page = state.remit_page
    if not _remit_page_is_live(remit_page):
        return
    log_to_gui("  → Recycling remittance tab...\n")
    try:
        _goto_remit_home_fallback(remit_page)
        log_to_gui("  ✓ Remittance tab recycled\n", "success")
    except Exception as e:
        log_to_gui(
            f"  ⚠️ Remittance recycle failed ({e}); reopening tab\n",
            "error",
        )
        try:
            if not remit_page.is_closed():
                remit_page.close()
        except Exception:
            pass
        state.remit_page = None
        try:
            _ensure_remit_page(claim_page)
        except Exception as reopen_err:
            log_to_gui(
                f"  ⚠️ Remittance reopen failed: {reopen_err}\n",
                "error",
            )
    try:
        if claim_page is not None and not claim_page.is_closed():
            claim_page.bring_to_front()
    except Exception:
        pass


def _close_persistent_remit_page():
    """Close the remittance viewer tab at end of batch and clear state."""
    remit_page = state.remit_page
    state.remit_page = None
    if not _remit_page_is_live(remit_page):
        return
    try:
        remit_page.close()
    except Exception:
        pass


def _ensure_remit_page(page):
    """Return the live remittance tab, opening one if missing or closed."""
    remit_page = state.remit_page
    if _remit_page_is_live(remit_page):
        try:
            _prepare_remit_search_form(remit_page)
            return remit_page
        except Exception:
            try:
                _goto_remit_home_fallback(remit_page)
                return remit_page
            except Exception as e:
                log_to_gui(
                    f"    ⚠️ Remittance tab not ready ({e}); reopening\n",
                    "error",
                )
                try:
                    if not remit_page.is_closed():
                        remit_page.close()
                except Exception:
                    pass
                state.remit_page = None

    live = page
    try:
        if live is None or live.is_closed():
            live = get_live_page(page)
    except Exception:
        live = get_live_page(page)

    log_to_gui("  → Opening remittance viewer tab...\n")
    remit_page = live.context.new_page()
    remit_page.goto(
        REMITTANCE_URLS['search'], wait_until='domcontentloaded', timeout=45000,
    )
    _prepare_remit_search_form(remit_page)
    state.remit_page = remit_page
    log_to_gui("  ✓ Remittance viewer tab is open.\n", "success")
    # #region agent log
    _agent_log(
        'E',
        'automation.py:_ensure_remit_page',
        'remittance tab open',
        {},
    )
    # #endregion
    return remit_page


def _open_remit_claim_details(page, claim_id, check_date=None):
    """Search the persistent remittance tab by claim_id and land on claim-details.

    The Remits row must match Claim # and claim-detail Check Date (or a
    Remits Check/EFT Date up to REMIT_CHECK_DATE_GRACE_DAYS later). When
    Check Date is missing, a unique Claim #-only row is accepted.
    Returns (remit_page, iframe) on success.  Returns (None, None) when the
    claim is not found.  Caller must reset the viewer; the tab stays open.
    """
    if not claim_id or claim_id == '--':
        return None, None

    remit_page = _ensure_remit_page(page)
    if remit_page is None:
        return None, None

    try:
        remit_page.bring_to_front()
    except Exception:
        pass

    log_to_gui(
        f"    → Remittance search: claim {claim_id}"
        f"{f', check date {check_date}' if check_date and check_date not in ('--', 'N/A') else ''}\n"
    )

    iframe = _prepare_remit_search_form(remit_page)

    search_input = iframe.locator(SELECTORS['remit_search_input'])
    search_input.first.wait_for(state='visible', timeout=20000)
    search_input.first.fill(claim_id)
    iframe.locator(SELECTORS['remit_search_btn']).first.click(timeout=15000)

    if _wait_remit_search_outcome(iframe):
        log_to_gui("    ℹ️ Remittance not found in viewer\n", "info")
        return None, None

    if not _click_matching_remit_claim(iframe, claim_id, check_date):
        return None, None

    remit_page.wait_for_url("**claim-details**", timeout=45000)
    return remit_page, iframe


def _read_remit_denial_once(page, claim_id, check_date=None):
    """One remittance-viewer denial lookup, as a ``FieldRead``.

    Distinguishes "the remit was not found" from "the lookup itself broke", so
    validated payers can retry only the latter.
    """
    harvested = _harvest_remit_viewer(page, claim_id, check_date)
    if harvested.get('error') is not None:
        return FieldRead.failed(harvested['error'], value=harvested.get('denial') or '--')
    if harvested.get('not_found'):
        return FieldRead.absent(REMIT_NOT_FOUND_DENIAL)
    return FieldRead.found(harvested.get('denial') or '--')


def extract_denial_reason_villagecaremax(page, claim_id, check_date=None,
                                         payer_config=None):
    """Read denial reason from the persistent remittance viewer tab.

    Reuses the remittance tab opened at Start Automation, searches by
    claim_id, selects the Remits row that also matches claim-detail Check
    Date (or up to REMIT_CHECK_DATE_GRACE_DAYS later), reads the first Claim
    Adjustment value from the Adjustments table, then Back + clears Applied
    Search Terms.  Returns '--' on any failure.

    Payers that declare ``result_validation`` get retries and a ``FieldRead``
    instead of a bare string, so a broken lookup fails the row rather than
    silently writing ``--``.
    """
    policy = _result_validation_policy(payer_config)
    if not claim_id or claim_id == '--':
        blank = FieldRead.absent('--')
        return blank if policy else blank.value

    if not policy:
        return _read_remit_denial_once(page, claim_id, check_date).value

    attempts = max(1, int(policy.get('remittance_attempts', 1)))
    delay = float(policy.get('remittance_retry_delay_s', 0.0))
    read = FieldRead.failed('remittance lookup was not attempted')
    for attempt in range(attempts):
        read = _read_remit_denial_once(page, claim_id, check_date)
        if read.state != FIELD_FAILED:
            return read
        if attempt < attempts - 1:
            log_to_gui(
                f"    ↻ Retrying remittance lookup "
                f"({attempt + 2}/{attempts})\n",
                "info",
            )
            time.sleep(max(0.0, delay))
    return read


_REMIT_ADJUSTMENT_CHROME_LABELS = (
    'claim adjustment group codes',
    'claim adjustment group code',
    'claim adjustment code/desc',
    'claim adjustment code / desc',
    'adjusted units',
    'adj amount',
    'adj. amount',
)
_REMIT_ADJUSTMENT_CHROME_PHRASES = (
    'claim adjustment group',
    'claim adjustment code',
    'adjusted units',
    'adj amount',
    'code/desc',
    'group codes',
)
_REMIT_GROUP_CODES = frozenset({'co', 'cr', 'oa', 'pi', 'pr'})
_REMIT_GROUP_CODE_DESCRIPTIONS = frozenset({
    'contractual obligation',
    'contractual obligations',
    'patient responsibility',
    'other adjustments',
    'other adjustment',
    'payer initiated reductions',
    'payer initiated reduction',
    'corrections and reversals',
    'correction and reversal',
})
_REMIT_GROUP_CODE_RE = re.compile(
    r'^(CO|CR|OA|PI|PR)(\s*[-:]\s+\S.*)?$',
    re.IGNORECASE,
)
_REMIT_NUMERIC_FILLER_RE = re.compile(r'^[\$(]?\s*-?\d[\d,]*\.?\d*\s*\)?$')


def _clean_remit_cell_text(text):
    """Strip remittance 'View More' chrome and collapse surrounding space."""
    cleaned = (text or '').replace('View More', '').replace('View Less', '')
    return cleaned.strip()


def _folded_remit_text(text):
    """Lowercase remittance text with collapsed space and trailing periods."""
    return ' '.join(_clean_remit_cell_text(text).casefold().split()).rstrip('.')


def _is_remit_adjustment_chrome(text):
    """True when text is nested Adjustments column headers, not a denial."""
    folded = ' '.join(_clean_remit_cell_text(text).casefold().split())
    if not folded:
        return False
    if folded in _REMIT_ADJUSTMENT_CHROME_LABELS:
        return True
    hits = sum(1 for phrase in _REMIT_ADJUSTMENT_CHROME_PHRASES if phrase in folded)
    return hits >= 2


def _is_remit_group_code(text):
    """True when text is a Claim Adjustment Group Code, not Code/Desc."""
    folded = _folded_remit_text(text)
    if not folded:
        return False
    if folded in _REMIT_GROUP_CODES or folded in _REMIT_GROUP_CODE_DESCRIPTIONS:
        return True
    return bool(_REMIT_GROUP_CODE_RE.match(folded))


def _is_remit_numeric_filler(text):
    """True when text is Adjusted Units / Adj Amount, not a denial."""
    folded = _folded_remit_text(text).replace(' ', '')
    return bool(folded) and bool(_REMIT_NUMERIC_FILLER_RE.fullmatch(folded))


def _looks_like_remit_disclaimer(text):
    """True when scraped text is remittance-viewer boilerplate, not claim data."""
    lower = (text or '').lower()
    return (
        'disclaimer' in lower
        or 'remittance viewer reflects' in lower
        or '835 file' in lower
        or 'explanation of payments' in lower
    )


def _normalize_remit_amount_text(text):
    """Strip accounting-style parentheses so ``($0.00)`` parses as zero."""
    cleaned = (text or '').strip()
    if cleaned.startswith('(') and cleaned.endswith(')'):
        inner = cleaned[1:-1].strip()
        if inner and not inner.startswith('$') and inner[0].isdigit():
            return f'${inner}'
        return inner
    return cleaned


def _is_remit_currency_value(text):
    """True when text parses as a currency amount and is not boilerplate."""
    if not text or text.strip() in ('', '--'):
        return False
    if _looks_like_remit_disclaimer(text):
        return False
    return parse_amount(_normalize_remit_amount_text(text)) is not None


def _payer_filters_remit_disclaimer(payer_config=None):
    """True when the payer's remittance page interleaves disclaimer text."""
    if payer_config is None:
        payer_config = state.selected_payer_config or {}
    return bool(payer_config.get('filter_remit_disclaimer_text'))


def _is_usable_remit_code_desc(text):
    """True when text can be a Claim Adjustment Code/Desc denial value."""
    cleaned = _clean_remit_cell_text(text)
    if not cleaned or cleaned in ('--', 'N/A'):
        return False
    if _is_remit_adjustment_chrome(cleaned) or _is_remit_group_code(cleaned):
        return False
    if _payer_filters_remit_disclaimer() and _looks_like_remit_disclaimer(cleaned):
        return False
    return not _is_remit_numeric_filler(cleaned)


def _description_line_from_remit_text(text):
    """Pick a Claim Adjustment Code/Desc ``code - description`` line."""
    cleaned = _clean_remit_cell_text(text)
    if not cleaned:
        return ''
    for line in cleaned.splitlines():
        line = line.strip()
        if ' - ' in line and _is_usable_remit_code_desc(line):
            return line
    if not _is_usable_remit_code_desc(cleaned):
        return ''
    return cleaned


def _format_remit_claim_adjustment(adjustment):
    """Turn Claim Adjustment text into a trimmed first-sentence Denial Reason.

    Use Claim Adjustment Code/Desc, not Claim Adjustment Group Codes.
    When the value uses a ``code - description`` separator, keep only the
    description. Always stop at the first period, including that period.
    Nested Adjustments header labels are ignored.
    """
    text = _description_line_from_remit_text(adjustment)
    if not _is_usable_remit_code_desc(text):
        return '--'

    separator = ' - '
    if separator in text:
        text = text.split(separator, 1)[1].strip()
        if not _is_usable_remit_code_desc(text):
            return '--'

    period_idx = text.find('.')
    if period_idx >= 0:
        text = text[:period_idx + 1]
    text = text.strip()
    if not _is_usable_remit_code_desc(text):
        return '--'
    return text


def _prefer_remit_description_cell(nested_texts, fallback_text=''):
    """Pick Claim Adjustment Code/Desc from nested cells, then the td fallback.

    Nested Adjustments rows are ``Group Codes | Code/Desc | Units | Amount``.
    Villagecaremax/Healthfirst split a CARC across two ``role=cell`` nodes
    (description is index 1). Skip header labels and group-code values such
    as ``CO - Contractual Obligation``. Prefer the Code/Desc column.
    """
    candidates = []
    for raw in nested_texts or []:
        text = _clean_remit_cell_text(raw)
        if text and not _is_remit_adjustment_chrome(text):
            candidates.append(text)
    if len(candidates) >= 2 and _is_usable_remit_code_desc(candidates[1]):
        return candidates[1]
    for text in candidates:
        if ' - ' in text and _is_usable_remit_code_desc(text):
            return text
    usable = [text for text in candidates if _is_usable_remit_code_desc(text)]
    if len(usable) > 1:
        return usable[1]
    if len(usable) == 1:
        return usable[0]
    return _description_line_from_remit_text(fallback_text)


def _first_usable_remit_reason(adjustment_texts):
    """Return the first formatted Claim Adjustment that is a real denial."""
    for text in adjustment_texts or []:
        reason = _format_remit_claim_adjustment(text)
        if not _is_blank_denial_reason(reason):
            return reason
    return '--'


def _adj_table_column_map(table):
    """Map Adjustments thead labels to billed/paid/qty/date/denial indexes."""
    cols = {
        'denial': 3,
        'billed': None,
        'paid': None,
        'quantity': None,
        'service_dates': None,
    }
    try:
        headers = table.locator('thead th')
        for idx in range(headers.count()):
            raw = (headers.nth(idx).inner_text(timeout=2000) or '').strip().lower()
            label = ' '.join(raw.split())
            if 'service date' in label:
                cols['service_dates'] = idx
            elif 'charge' in label or 'billed' in label:
                cols['billed'] = idx
            elif 'payment' in label or 'paid amount' in label or label == 'paid':
                cols['paid'] = idx
            elif (
                'quantity' in label or label in ('qty', 'units')
                or 'adjusted units' in label
            ):
                cols['quantity'] = idx
            elif 'code/desc' in label or 'code / desc' in label:
                cols['denial'] = idx
            elif 'adjustment' in label and 'group' not in label:
                cols['denial'] = idx
    except Exception:
        pass
    return cols


def _adj_cell_text(cells, idx):
    """Inner text of ``td`` ``idx``, or empty string."""
    if idx is None:
        return ''
    try:
        if idx >= cells.count():
            return ''
        return (cells.nth(idx).inner_text(timeout=3000) or '').strip()
    except Exception:
        return ''


def _read_remit_adjustment_rows(iframe, adj_table):
    """Scrape every Adjustments body row into remit-line dicts.

    Each row may carry billed/paid/quantity/service date plus that row's
    Claim Adjustment text. Rows without amount columns still contribute a
    denial for claim-level fallback.
    """
    del iframe
    rows_out = []
    try:
        table_count = int(adj_table.count())
    except Exception:
        table_count = 0
    tables = (
        [adj_table.nth(idx) for idx in range(table_count)]
        if table_count > 0
        else [adj_table]
    )
    for table in tables:
        cols = _adj_table_column_map(table)
        try:
            rows = table.locator('tbody').first.locator(':scope > tr')
            row_count = int(rows.count())
        except Exception:
            continue
        for r_idx in range(row_count):
            try:
                cells = rows.nth(r_idx).locator(':scope > td')
                n_cells = int(cells.count())
                if n_cells < 1:
                    continue
                denial_idx = cols.get('denial')
                if denial_idx is None or denial_idx >= n_cells:
                    denial_idx = 3 if n_cells > 3 else n_cells - 1
                raw_denial = _read_remit_adjustment_desc(cells.nth(denial_idx))
                denial = _format_remit_claim_adjustment(raw_denial)
                rows_out.append({
                    'billed': _adj_cell_text(cells, cols.get('billed')) or '--',
                    'paid': _adj_cell_text(cells, cols.get('paid')) or '--',
                    'quantity': _adj_cell_text(cells, cols.get('quantity')),
                    'date_range': _adj_cell_text(cells, cols.get('service_dates')),
                    'denial': denial if not _is_blank_denial_reason(denial) else None,
                    'raw_denial': raw_denial,
                })
            except Exception:
                continue
    return rows_out


def _read_remit_denial(iframe, adj_table):
    """Read Denial Reason from Claim Adjustment cells on the remittance page.

    Scan each Adjustments table's own body rows (not nested header grids).
    Skip blank / header-only Claim Adjustment cells so Aetna partials can
    use a later service-line row.
    """
    rows = _read_remit_adjustment_rows(iframe, adj_table)
    return _first_usable_remit_reason(
        [row.get('raw_denial') or '' for row in rows],
    )


def _harvest_remit_viewer(page, claim_id, check_date=None):
    """Open remittance once and scrape per-line adjustments plus claim fallback.

    Returns a dict with ``denial``, ``lines``, ``not_found``, and ``error``.
    Always resets the remittance search form afterwards.
    """
    result = {
        'denial': '--',
        'lines': [],
        'not_found': False,
        'error': None,
    }
    landed_on_details = False
    try:
        remit_page, iframe = _open_remit_claim_details(page, claim_id, check_date)
        if remit_page is None:
            result['denial'] = REMIT_NOT_FOUND_DENIAL
            result['not_found'] = True
            return result

        landed_on_details = True
        adj_table = iframe.locator(SELECTORS['remit_adj_table'])
        adj_table.first.wait_for(state='visible', timeout=30000)
        rows = _read_remit_adjustment_rows(iframe, adj_table)
        result['lines'] = rows
        result['denial'] = _first_usable_remit_reason(
            [row.get('raw_denial') or row.get('denial') or '' for row in rows],
        )
        usable_count = sum(
            1 for row in rows if _usable_remittance_denial(row.get('denial'))
        )
        if usable_count > 1:
            log_to_gui(
                f"    ✓ Remittance adjustments: {usable_count} line(s); "
                f"claim-level: {result['denial']}\n",
                "success",
            )
        else:
            log_to_gui(f"    ✓ Remittance denial: {result['denial']}\n", "success")
        return result
    except Exception as e:
        log_to_gui(f"    ⚠️ Villagecaremax remittance failed: {e}\n", "error")
        result['error'] = e
        return result
    finally:
        _reset_remit_search(
            state.remit_page, from_claim_details=landed_on_details,
        )
        try:
            if page is not None and not page.is_closed():
                page.bring_to_front()
        except Exception:
            pass


def _nested_remit_cell_texts(adj_cell):
    """Inner texts of nested data ``role=cell`` nodes, grouped by row."""
    try:
        rows = adj_cell.locator('div[role="row"]:has(div[role="cell"])')
        row_count = rows.count()
    except Exception:
        return []
    texts = []
    for r_idx in range(row_count):
        cells = rows.nth(r_idx).locator('div[role="cell"]')
        try:
            n = cells.count()
        except Exception:
            continue
        if n <= 0:
            continue
        row_texts = []
        for idx in range(n):
            try:
                row_texts.append(cells.nth(idx).inner_text(timeout=10000).strip())
            except Exception:
                row_texts.append('')
        if all(
            (not t.strip()) or _is_remit_adjustment_chrome(t)
            for t in row_texts
        ):
            continue
        texts.extend(row_texts)
    if texts:
        return texts
    locators = (
        adj_cell.locator('div[role="row"] div[role="cell"].col-sm-4'),
        adj_cell.locator('div[role="row"] div[role="cell"]'),
    )
    for loc in locators:
        try:
            n = loc.count()
        except Exception:
            continue
        if n <= 0:
            continue
        flat = []
        for idx in range(n):
            try:
                flat.append(loc.nth(idx).inner_text(timeout=10000).strip())
            except Exception:
                flat.append('')
        if any(t.strip() and not _is_remit_adjustment_chrome(t) for t in flat):
            return flat
    return []


def _read_remit_adjustment_desc(adj_cell):
    """Read Claim Adjustment code/description from the Adjustments table cell."""
    if adj_cell.count() == 0:
        return ''
    _expand_view_more(adj_cell)
    fallback = ''
    try:
        fallback = adj_cell.inner_text(timeout=10000).strip()
    except Exception:
        fallback = ''
    return _prefer_remit_description_cell(
        _nested_remit_cell_texts(adj_cell), fallback,
    )


def _expand_view_more(container):
    """Click all 'View More' buttons inside container to reveal truncated text."""
    buttons = container.locator('button:has-text("View More")')
    for idx in range(buttons.count()):
        try:
            btn = buttons.nth(idx)
            btn.click(timeout=3000)
            try:
                btn.wait_for(state='hidden', timeout=2000)
            except Exception:
                pass
        except Exception:
            pass


# ============================================================================
# SECTION 14 — PAYER-SPECIFIC CLAIM ENRICHMENT
# ============================================================================
# These three functions encapsulate all payer-branching logic so that
# process_one_claim() is fully payer-agnostic.

def _enrich_amounts_by_invoice_alignment(iframe, claim_data, row_data,
                                         payer_config, invoice_context):
    """Amounts for invoice-alignment payers, from the pool-tier match map.

    Returns (matching_row, row_index, billed_str, paid_str).
    """
    ctx = invoice_context if invoice_context is not None else {}
    aligned = resolve_aligned_line_for_row(
        claim_data, ctx, iframe, row_data, payer_config=payer_config,
    )
    # Persist so denial extraction for this same claim reuses the chosen line.
    ctx['_aligned_line_entry'] = aligned
    write_confidence = 'Match Confidence' in resolved_output_columns(payer_config)

    if aligned.get('unresolved') or not aligned.get('line'):
        claim_data['_line_unresolved'] = True
        conf = aligned.get('confidence') or MATCH_FLAG_MANUAL_REVIEW
        note = aligned.get('note')
        if note:
            conf = f"{conf}; {note}"
        if write_confidence:
            claim_data['Match Confidence'] = humanize_match_confidence(conf)
        claim_data['Billed Amount'] = '--'
        claim_data['Paid Amount'] = '--'
        return None, -1, '--', '--'

    line = aligned['line']
    confidence = aligned.get('confidence') or ''
    note = aligned.get('note')
    if note and MATCH_FLAG_MANUAL_REVIEW in str(confidence):
        confidence = f"{confidence}; {note}"
    # A safe match keeps notes off Match Confidence; critical notes only ride
    # along with Need Manual Review.
    confidence = humanize_match_confidence(confidence)
    if write_confidence:
        claim_data['Match Confidence'] = confidence or '--'
    if aligned.get('denial_line_numbers'):
        claim_data['_denial_line_numbers'] = list(aligned['denial_line_numbers'])
    if MATCH_FLAG_MANUAL_REVIEW in confidence:
        log_to_gui(
            f"    ⚠️ Match confidence {confidence} — verify this row manually\n",
            "error",
        )

    if aligned.get('split_bill'):
        billed = aligned.get('billed') or line.get('billed', '--')
        paid = aligned.get('paid') or line.get('paid', '--')
        claim_data['_split_bill'] = True
        claim_data['_forced_status'] = aligned.get('forced_status')
        claim_data['_needs_claim_comment'] = bool(aligned.get('needs_claim_comment'))
        claim_data['_matched_line_status'] = (
            aligned.get('forced_status') or line.get('status') or ''
        )
        claim_data['Billed Amount'] = billed
        claim_data['Paid Amount'] = paid
        return line.get('row'), line.get('idx', -1), billed, paid

    claim_data['_matched_line_status'] = line.get('status') or ''
    claim_data['Billed Amount'] = line.get('billed', '--')
    claim_data['Paid Amount'] = line.get('paid', '--')
    return (
        line.get('row'),
        line.get('idx', -1),
        line.get('billed', '--'),
        line.get('paid', '--'),
    )


def enrich_claim_with_amounts(iframe, claim_data, row_data, payer_config,
                            is_bundle_claim=False, page=None,
                            invoice_context=None):
    """Populate Billed Amount and Paid Amount based on payer config.

    uses_line_level=True  → find the matching service-date row in the line table.
    uses_line_level=False → amounts are already on the header panels;
                            also promote Check Date to Finalized Date if present.

    Payers whose ``line_level`` sets ``match_by_invoice_alignment`` match every
    row of the invoice against every portal line at once instead (SWHNY).

    When ``is_bundle_claim``, amounts come only from the VisitDate line — no header
    fallback.

    Returns (matching_row, row_index, billed_str, paid_str).
    """
    if payer_config['uses_line_level']:
        cfg = resolve_line_level_config(payer_config)
        if cfg.get('match_by_invoice_alignment'):
            claim_data['_is_adjusted'] = False
            return _enrich_amounts_by_invoice_alignment(
                iframe, claim_data, row_data, payer_config, invoice_context,
            )

        visit_date = safe_field(row_data, 'VisitDate')
        row, idx, billed, paid, is_adjusted = find_line_by_visit_date(
            iframe, visit_date, payer_config=payer_config,
            invoice_context=invoice_context,
        )
        claim_data['_is_adjusted'] = bool(is_adjusted)
        if (invoice_context or {}).get('_line_unresolved'):
            claim_data['_line_unresolved'] = True

        if is_bundle_claim:
            if row is None:
                billed, paid = '--', '--'
                log_to_gui(
                    f"    ⚠️ Bundle claim: no line-level row for VisitDate "
                    f"{normalize_date_range(visit_date)}\n",
                    "error",
                )
        elif row is None or (billed in ('', '--') and paid in ('', '--')):
            header_billed = claim_data.get('Billed Amount', '--')
            header_paid = claim_data.get('Paid Amount', '--')
            if header_billed not in ('', '--') or header_paid not in ('', '--'):
                billed, paid = header_billed, header_paid
                row, idx = None, -1
                claim_data['_is_adjusted'] = False
                log_to_gui(
                    "    ℹ️ Line-level match not found; using header billed/paid amounts\n",
                    "info",
                )

        claim_data['Billed Amount'] = billed
        claim_data['Paid Amount']   = paid
        return row, idx, billed, paid
    else:
        claim_data['_is_adjusted'] = False
        billed = claim_data.get('Billed Amount', '--')
        paid   = claim_data.get('Paid Amount',   '--')
        if not payer_config.get('uses_results_finalized_date'):
            if claim_data.get('Check Date') and claim_data['Check Date'] != '--':
                claim_data['Finalized Date'] = claim_data['Check Date']
        return None, -1, billed, paid


def derive_claim_status(claim_data, billed, paid, payer_config, is_adjusted=False):
    """Override Claim Status where the portal-reported status is insufficient.

    Rules applied in priority order:
      0. Healthfirst original/adjustment pair                 → Adjusted
      1. uses_remittance payer + paid == $0                   → Denied
         uses_line_level payer + paid == $0 + billed > $0     → Denied
      2. Any payer: paid > $0 but paid < billed               → Partially Paid
      3. Any payer: paid > billed                             → Paid (Overpayment)
    """
    if is_adjusted and payer_detects_adjustment_pairs(payer_config):
        claim_data['Claim Status'] = ADJUSTED_STATUS
        log_to_gui("    ℹ️ Original/adjustment pair — marked as Adjusted\n", "info")
        return

    billed_val = parse_amount(billed)
    paid_val   = parse_amount(paid)

    if paid_val is not None and paid_val == 0:
        if payer_config['uses_remittance']:
            claim_data['Claim Status'] = 'Denied'
            log_to_gui("    ℹ️ Paid $0 — marked as Denied\n", "info")
        elif (payer_config.get('uses_line_level')
                and billed_val is not None and billed_val > 0):
            claim_data['Claim Status'] = 'Denied'
            log_to_gui("    ℹ️ Paid $0 — marked as Denied\n", "info")

    if (billed_val is not None and paid_val is not None
            and billed_val > 0 and paid_val > 0 and paid_val < billed_val):
        claim_data['Claim Status'] = 'Partially Paid'
        log_to_gui("    ℹ️ Partial payment — marked as Partially Paid\n", "info")

    if billed_val is not None and paid_val is not None and paid_val > billed_val:
        claim_data['Claim Status'] = PAID_OVERPAYMENT_STATUS
        log_to_gui("    ℹ️ Paid exceeds billed — marked as Paid (Overpayment)\n", "info")


def _line_level_denial_reason(
    iframe, claim_data, billed, paid, matching_row, row_idx, payer_config,
    row_data=None, is_bundle_claim=False,
):
    """Compute Denial Reason from HIPAA or Reason/Remark codes."""
    if (
        _payer_reads_hipaa_codes(payer_config)
        and str(claim_data.get('Claim Status', '')).upper() != 'PENDING'
        and _is_fully_paid_amounts(billed, paid)
    ):
        log_to_gui(
            f"    ℹ️ Fully paid — Denial Reason: {FULLY_PAID_DENIAL_REASON}\n",
            "info",
        )
        return FULLY_PAID_DENIAL_REASON
    if is_bundle_claim:
        if matching_row is not None and should_extract_denial(
            claim_data['Claim Status'], billed, paid, matching_row
        ):
            return extract_denial_codes_inline(
                iframe, matching_row, row_idx, payer_config,
            )
        return '--'
    visit_date = (
        coerce_visit_date(safe_field(row_data, 'VisitDate'))
        if row_data is not None else ''
    )
    return extract_line_level_denial_reason(
        iframe, claim_data['Claim Status'], payer_config,
        visit_date=visit_date,
    )


def enrich_claim_with_denial(page, iframe, claim_data, billed, paid,
                              matching_row, row_idx, payer_config, row_data=None,
                              is_bundle_claim=False, allow_line_fallback=True):
    """Determine and set the Denial Reason field based on payer config.

    Remittance-first payers open the viewer whenever Claim ID exists.
    When ``remittance_when_remarks_blank`` is on, Reason/Remark or HIPAA
    codes are applied first and remittance runs only if that result is blank.
    Fully paid / overpayment remittance lookup is skipped.
    Villagecaremax also skips remittance when Check Number, Check Date,
    and Paid Amount are all still ``--`` (Denial Reason becomes In Process).
    When extraction leaves Denial Reason blank, fill it from Claim Status
    unless this is remittance harvest (``allow_line_fallback=False``).
    """
    uses_remit = bool(payer_config.get('uses_remittance'))
    blank_mode = bool(payer_config.get('remittance_when_remarks_blank'))
    uses_line = bool(payer_config.get('uses_line_level'))
    line_reason = None

    def _apply_blank_status_fill():
        if allow_line_fallback:
            _fill_blank_status_denial(claim_data, billed, paid)

    def _fill_line_reason():
        nonlocal line_reason
        if line_reason is not None:
            return line_reason
        if uses_line:
            line_reason = _line_level_denial_reason(
                iframe, claim_data, billed, paid, matching_row, row_idx,
                payer_config, row_data=row_data, is_bundle_claim=is_bundle_claim,
            )
        elif should_extract_denial(
            claim_data.get('Claim Status'), billed, paid, matching_row,
        ):
            line_reason = extract_denial_codes_inline(
                iframe, matching_row, row_idx, payer_config,
            )
        else:
            line_reason = '--'
        return line_reason

    if uses_remit and blank_mode and uses_line and allow_line_fallback:
        reason = _fill_line_reason()
        if not _is_blank_denial_reason(reason):
            claim_data['Denial Reason'] = reason
            _apply_blank_status_fill()
            return

    if uses_remit:
        remit_reason = '--'
        claim_id = claim_data.get('Claim ID')
        if not _should_open_remittance(
            payer_config, claim_id, line_reason, claim_data, paid,
        ):
            if not _has_claim_id(claim_data):
                log_to_gui(
                    "    ℹ️ No claim number — skipping remittance denial lookup\n",
                    "info",
                )
            elif _claim_status_is_pending(claim_data):
                log_to_gui(
                    "    ℹ️ PENDING — skipping remittance denial lookup\n",
                    "info",
                )
            elif _unposted_payment_skips_remittance(payer_config, claim_data, paid):
                log_to_gui(
                    "    ℹ️ Check Number, Check Date, and Paid Amount are -- "
                    "— skipping remittance viewer\n",
                    "info",
                )
        else:
            skip_why = _payment_skips_remittance(billed, paid)
            skip_denial = bool(skip_why)
            if skip_why == 'fully_paid':
                log_to_gui(
                    "    ℹ️ Fully paid — skipping remittance denial lookup\n",
                    "info",
                )
            elif skip_why == 'overpayment':
                log_to_gui(
                    "    ℹ️ Overpayment — skipping remittance denial lookup\n",
                    "info",
                )

            if not skip_denial:
                remit_reason = extract_denial_reason_villagecaremax(
                    page, claim_data['Claim ID'], claim_data.get('Check Date'),
                    payer_config=payer_config,
                )
                if isinstance(remit_reason, FieldRead):
                    _set_denial_read(
                        claim_data, remit_reason,
                        required=remit_reason.state == FIELD_FAILED,
                    )
                    remit_reason = remit_reason.value

        if _usable_remittance_denial(remit_reason):
            claim_data['Denial Reason'] = remit_reason
            _apply_blank_status_fill()
            return
        if not allow_line_fallback or not uses_line:
            claim_data['Denial Reason'] = remit_reason or '--'
            _apply_blank_status_fill()
            return
        if line_reason is not None:
            claim_data['Denial Reason'] = (
                line_reason
                if not _is_blank_denial_reason(line_reason)
                else (remit_reason or '--')
            )
            _apply_blank_status_fill()
            return
        log_to_gui(
            "    ℹ️ Remittance denial unavailable — falling back to line codes\n",
            "info",
        )

    if uses_line:
        claim_data['Denial Reason'] = _fill_line_reason()
        _apply_blank_status_fill()
        return
    if should_extract_denial(
        claim_data.get('Claim Status'), billed, paid, matching_row,
    ):
        claim_data['Denial Reason'] = extract_denial_codes_inline(
            iframe, matching_row, row_idx, payer_config,
        )
    else:
        claim_data['Denial Reason'] = '--'
    _apply_blank_status_fill()


# ============================================================================
# SECTION 15 — SINGLE-CLAIM PROCESSING PIPELINE
# ============================================================================

def _should_harvest_line_denials(payer_config, header=None):
    """True when claim-detail remark/HIPAA codes should be expanded at harvest.

    Remittance-first payers (Aetna / Villagecaremax / SWHNY) skip line expansion.
    Line-level payers harvest remarks when remittance is off, when
    ``remittance_when_remarks_blank`` is on, or when HIPAA codes are read.
    """
    if not payer_config or not payer_config.get('uses_line_level'):
        return False
    if not payer_config.get('uses_remittance'):
        return True
    if payer_config.get('remittance_when_remarks_blank'):
        return True
    if _payer_reads_hipaa_codes(payer_config):
        return True
    return False


def harvest_open_claim_snapshot(page, iframe, payer_config,
                                fingerprint='', results_finalized_date=None):
    """Scrape the open claim detail page into a locator-free snapshot.

    Collects header panels, every ``#lineLevelTable`` row (amounts), and
    remittance denial when ``uses_remittance`` is True. Line remark-code
    expansion is skipped for remittance-first payers (Aetna / SWHNY). Callers fan
    the snapshot out to matching spreadsheet rows.
    """
    header = extract_claim_header(iframe, payer_config)
    portal_status = header.get('Claim Status', '')
    lines = []

    if payer_config.get('uses_line_level'):
        try:
            raw_lines = _scrape_line_data_rows(iframe, payer_config)
        except Exception as e:
            log_to_gui(f"    ⚠️ Line table harvest error: {e}\n", "error")
            raw_lines = []
        if raw_lines:
            log_to_gui(
                f"    → Harvesting {len(raw_lines)} service line(s) "
                "for invoice reuse\n",
                "info",
            )
        use_hipaa = _payer_reads_hipaa_codes(payer_config)
        extract_line_denials = _should_harvest_line_denials(payer_config, header)
        roles = (
            classify_adjustment_pair_roles(raw_lines)
            if payer_detects_adjustment_pairs(payer_config)
            else [None] * len(raw_lines)
        )
        for entry, role in zip(raw_lines, roles):
            serial = {
                'date_range': entry.get('date_range', ''),
                'billed': entry.get('billed', '--'),
                'paid': entry.get('paid', '--'),
                'quantity': entry.get('quantity') or '',
                'status': entry.get('status') or '',
                'hipaa_codes': entry.get('hipaa_codes') or '',
                'denial_pairs': [],
            }
            if extract_line_denials and _should_extract_line_denial(
                portal_status, entry, role,
            ):
                serial['denial_pairs'] = _denial_pairs_from_line_row(
                    iframe, entry, payer_config, use_hipaa,
                )
            lines.append(serial)

        if extract_line_denials and payer_detects_adjustment_pairs(payer_config):
            groups_with_codes = {
                adjustment_pair_group_key(serial)
                for serial, role in zip(lines, roles)
                if role == 'original' and serial.get('denial_pairs')
            }
            for serial, entry, role in zip(lines, raw_lines, roles):
                if role != 'adjustment':
                    continue
                key = adjustment_pair_group_key(serial)
                if key in groups_with_codes:
                    continue
                if str(portal_status or '').upper() == 'PENDING':
                    break
                if entry.get('row') is None:
                    continue
                log_to_gui(
                    "    → Original line had no remark codes — "
                    "falling back to adjustment line\n",
                    "info",
                )
                pairs = _denial_pairs_from_line_row(
                    iframe, entry, payer_config, use_hipaa,
                )
                if not pairs:
                    continue
                serial['denial_pairs'] = pairs
                groups_with_codes.add(key)

    remittance_denial = None
    remit_join_unresolved = False
    harvest_complete = True
    if payer_config.get('uses_remittance'):
        if _claim_status_is_pending(header):
            log_to_gui(
                "    ℹ️ PENDING — skipping remittance denial lookup\n",
                "info",
            )
        elif _unposted_payment_skips_remittance(payer_config, header):
            log_to_gui(
                "    ℹ️ Check Number, Check Date, and Paid Amount are -- "
                "— skipping remittance viewer\n",
                "info",
            )
        else:
            line_denial = (
                'codes' if _harvested_lines_have_denial(lines) else '--'
            )
            should_open = _should_open_remittance(
                payer_config, header.get('Claim ID'), line_denial,
                claim_data=header,
            )
            if should_open:
                billed, paid = _harvest_remittance_amounts(header, lines)
                skip_why = _payment_skips_remittance(billed, paid)
                if skip_why == 'fully_paid':
                    log_to_gui(
                        "    ℹ️ Fully paid — skipping remittance denial lookup\n",
                        "info",
                    )
                elif skip_why == 'overpayment':
                    log_to_gui(
                        "    ℹ️ Overpayment — skipping remittance denial lookup\n",
                        "info",
                    )
                elif skip_why:
                    pass
                else:
                    log_to_gui(
                        "    ℹ️ Searching remittance for line adjustments\n",
                        "info",
                    )
                    remit_error = None
                    harvested = {
                        'denial': '--',
                        'lines': [],
                        'not_found': False,
                        'error': None,
                    }
                    try:
                        harvested = _harvest_remit_viewer(
                            page, header.get('Claim ID'),
                            header.get('Check Date'),
                        )
                    except Exception as e:
                        harvest_complete = False
                        if not _result_validation_policy(payer_config):
                            raise
                        remit_error = e
                        log_to_gui(
                            f"    ⚠️ Remittance lookup failed: {e}\n", "error",
                        )
                    if harvested.get('error') is not None:
                        remit_error = harvested['error']
                        harvest_complete = False
                    remittance_denial = harvested.get('denial', '--')
                    lines, claim_level, remit_join_unresolved = (
                        join_remit_lines_to_claim_lines(
                            lines, harvested.get('lines') or [],
                        )
                    )
                    if harvested.get('not_found'):
                        remittance_denial = REMIT_NOT_FOUND_DENIAL
                    elif harvested.get('lines'):
                        remittance_denial = (
                            claim_level
                            if _usable_remittance_denial(claim_level)
                            else None
                        )
                    if _result_validation_policy(payer_config):
                        _set_denial_read(
                            header,
                            FieldRead.failed(
                                remit_error, value=remittance_denial or '--',
                            )
                            if remit_error is not None
                            else (
                                FieldRead.found(remittance_denial)
                                if _usable_remittance_denial(remittance_denial)
                                else FieldRead.absent(remittance_denial)
                            ),
                            required=True,
                        )
                        header.pop('Denial Reason', None)

    detail_key = fingerprint or build_results_row_fingerprint(
        claim_number=header.get('Claim ID', ''),
        billed=header.get('Billed Amount', ''),
        paid=header.get('Paid Amount', ''),
    )
    return make_claim_snapshot(
        header=header,
        lines=lines,
        remittance_denial=remittance_denial,
        fingerprint=detail_key,
        results_finalized_date=results_finalized_date,
        harvest_complete=harvest_complete,
        remit_join_unresolved=remit_join_unresolved,
    )


def process_one_claim(page, iframe, row_data, claim_idx, total, payer_config,
                      results_finalized_date=None, is_bundle_claim=False,
                      invoice_context=None):
    """Extract all data fields for the claim currently open in the detail view.

    Returns a filled result dict, or empty_claim_result('Error') on failure.
    Returns None if the user has requested a stop.
    """
    if not state.is_running:
        return None

    try:
        log_to_gui(f"  → Processing claim {claim_idx + 1}/{total}...\n", "info")
        claim_data = extract_claim_header(iframe, payer_config)

        # Pending claims may still show claim number and billed/paid on the detail page
        if claim_data['Claim Status'].upper() == 'PENDING':
            log_to_gui(
                "    ℹ️ PENDING — capturing claim ID and amounts when available\n",
                "info",
            )
            matching_row, row_idx, billed, paid = enrich_claim_with_amounts(
                iframe, claim_data, row_data, payer_config,
                is_bundle_claim=is_bundle_claim, page=page,
                invoice_context=invoice_context,
            )
            enrich_claim_with_denial(
                page, iframe, claim_data, billed, paid,
                matching_row, row_idx, payer_config, row_data=row_data,
                is_bundle_claim=is_bundle_claim,
            )
            _apply_finalized_date_priority(
                claim_data, results_finalized_date, payer_config,
            )
            claim_data.pop('_is_adjusted', None)
            log_to_gui(f"    • Billed:   {claim_data['Billed Amount']}\n")
            log_to_gui(f"    • Paid:     {claim_data['Paid Amount']}\n")
            log_to_gui(f"    ✓ Claim {claim_idx + 1} complete (pending)\n", "success")
            return claim_data

        matching_row, row_idx, billed, paid = enrich_claim_with_amounts(
            iframe, claim_data, row_data, payer_config,
            is_bundle_claim=is_bundle_claim, page=page,
            invoice_context=invoice_context,
        )

        is_adjusted = bool(claim_data.pop('_is_adjusted', False))
        derive_claim_status(
            claim_data, billed, paid, payer_config, is_adjusted=is_adjusted,
        )
        enrich_claim_with_denial(
            page, iframe, claim_data, billed, paid,
            matching_row, row_idx, payer_config, row_data=row_data,
            is_bundle_claim=is_bundle_claim,
        )

        _apply_finalized_date_priority(
            claim_data, results_finalized_date, payer_config,
        )

        log_to_gui(f"    ✓ Claim {claim_idx + 1} complete\n", "success")
        return claim_data

    except Exception as e:
        log_to_gui(f"    ❌ Claim error: {e}\n", "error")
        return empty_claim_result('Error')


def process_all_claims_for_invoice(page, iframe, df, row_data, invoice_number,
                                   row_indices, payer_config, match_mode='invoice',
                                   spreadsheet_row_index=None):
    """Open each matched ``#claimsTable`` row once and harvest snapshots.

    ``row_indices`` are 0-based positions in ``#claimsTable`` after sort.
    ``match_mode`` is ``'invoice'`` or ``'service_dates'`` (bundle fallback).
    After each claim (except the last), navigates back to the results list
    via the breadcrumb link inside the iframe.

    Row selection uses a results-row fingerprint (claim/TCN + billed/paid, or
    full-row text) so that after back-navigation re-sorts the table, we open
    the next *unprocessed* claim instead of re-opening ``row_indices[claim_idx]``
    or collapsing same-day claims that share Finalized Date.

    Returns ``(snapshots, harvest_complete)``. ``harvest_complete`` is False
    when Stop, a missing claim, or a harvest failure left the invoice partial.
    """
    snapshots = []
    total = len(row_indices)
    harvest_complete = True
    processed_row_keys = set()
    col_map = _claims_table_identity_columns(iframe)

    log_to_gui(
        f"  → Opening {total} claim submission(s) once for invoice reuse"
        f"{' (bundle match)' if match_mode == 'service_dates' else ''}\n",
        "info",
    )

    def _row_key_at(idx):
        row = _claim_row_locator_at_index(iframe, idx)
        fingerprint = _results_row_fingerprint(iframe, row, idx, col_map)
        finalized = _peek_results_row_finalized_date(iframe, row)
        return fingerprint, finalized, row

    for claim_idx in range(total):
        if not state.is_running:
            harvest_complete = False
            break

        try:
            next_idx = None
            results_finalized_date = None
            for idx in row_indices:
                row_key, finalized, result_row = _row_key_at(idx)
                if not row_key or row_key in processed_row_keys:
                    continue
                next_idx = idx
                results_finalized_date = finalized
                break

            if next_idx is None:
                harvest_complete = False
                break

            result_row = _claim_row_locator_at_index(iframe, next_idx)
            row_key, _, _ = _row_key_at(next_idx)

            if payer_config.get('uses_results_finalized_date'):
                if results_finalized_date:
                    log_to_gui(
                        f"  • Results Finalized Date: {results_finalized_date}\n"
                    )
                else:
                    results_finalized_date = read_finalized_date_from_results_row(
                        iframe, result_row
                    )

            expected_claim_id = (
                _peek_results_row_claim_number(iframe, result_row, col_map)
                if _result_validation_policy(payer_config)
                else ''
            )

            result_row.click()
            detail_sel = get_selector_profile(payer_config).get(
                'claim_detail_ready', '[data-testid="testClaim NumberPanel"]',
            )
            iframe.locator(detail_sel).wait_for(state='visible', timeout=15000)
            if expected_claim_id:
                _wait_for_claim_detail_identity(
                    iframe, expected_claim_id, payer_config,
                )

            snapshot = harvest_open_claim_snapshot(
                page, iframe, payer_config,
                fingerprint=row_key,
                results_finalized_date=(
                    results_finalized_date
                    if (
                        payer_config.get('uses_results_finalized_date')
                        or payer_config.get('reconcile_finalized_date_conflicts')
                    )
                    else None
                ),
            )
            if row_key:
                processed_row_keys.add(row_key)
            if snapshot:
                detail_key = snapshot.get('fingerprint') or build_results_row_fingerprint(
                    claim_number=(snapshot.get('header') or {}).get('Claim ID', ''),
                    billed=(snapshot.get('header') or {}).get('Billed Amount', ''),
                    paid=(snapshot.get('header') or {}).get('Paid Amount', ''),
                )
                if detail_key:
                    processed_row_keys.add(detail_key)
                snapshots.append(snapshot)
                log_to_gui(
                    f"    ✓ Claim {claim_idx + 1}/{total} harvested "
                    f"({len(snapshot.get('lines') or [])} line(s))\n",
                    "success",
                )
                if snapshot.get('harvest_complete') is False:
                    harvest_complete = False
                    break
            else:
                harvest_complete = False
                break

            if claim_idx < total - 1:
                iframe, row_indices = _navigate_back_to_results(
                    page, iframe, df, invoice_number,
                    visit_date=safe_field(row_data, 'VisitDate'),
                    payer_config=payer_config,
                    row_data=row_data,
                    narrow_by_visit_date=False,
                )
                if iframe is None:
                    harvest_complete = False
                    break
                col_map = _claims_table_identity_columns(iframe)

        except AvailitySessionInactiveError:
            raise
        except Exception as e:
            harvest_complete = False
            log_to_gui(f"  ❌ Claim iteration error: {e}\n", "error")
            iframe = _attempt_recovery(page, row_data, payer_config, df=df)
            if iframe is None:
                break
            sort_claim_results_by_finalized_date(iframe)
            row_indices, _recovered_mode = resolve_matching_claim_indices(
                iframe, df, invoice_number,
                visit_date=safe_field(row_data, 'VisitDate'),
                payer_config=payer_config,
                narrow_by_visit_date=False,
                amount=safe_field(row_data, 'Amount'),
            )
            if not row_indices:
                harvest_complete = False
                break
            col_map = _claims_table_identity_columns(iframe)

    if len(snapshots) != total:
        harvest_complete = False
    return snapshots, harvest_complete


def _navigate_back_to_results(page, iframe, df, invoice_number,
                              visit_date=None, payer_config=None,
                              row_data=None, narrow_by_visit_date=False):
    """Click the Results breadcrumb and restore ascending sort order.

    Re-resolves matching row indices (invoice or bundle service dates) after
    sort. Callers must pick the next *unprocessed* claim by results-row
    fingerprint (claim/TCN + amounts), not by a fixed ``claim_idx`` into the
    re-sorted list or by Finalized Date alone.
    Returns (iframe, row_indices) on success, or (None, None) on failure.
    """
    try:
        log_to_gui("  → Going back to results...\n")

        try:
            iframe = _return_to_results_view(page, payer_config)
        except AvailitySessionInactiveError:
            raise
        except Exception as nav_error:
            if row_data is None:
                raise nav_error
            log_to_gui(
                f"  ℹ️ Back to results failed ({nav_error}) — "
                "re-running search\n",
                "info",
            )
            page, iframe = _rerun_claim_search(page, row_data, payer_config, df=df)

        sort_claim_results_by_finalized_date(iframe)
        row_indices, _match_mode = resolve_matching_claim_indices(
            iframe, df, invoice_number,
            visit_date=visit_date, payer_config=payer_config,
            narrow_by_visit_date=narrow_by_visit_date,
            amount=(
                safe_field(row_data, 'Amount') if row_data is not None else None
            ),
        )
        if not row_indices:
            raise Exception("Could not re-locate matching claims after back navigation")

        log_to_gui("  ✓ Back to results\n", "success")
        return iframe, row_indices
    except AvailitySessionInactiveError:
        raise
    except Exception as e:
        log_to_gui(f"  ❌ Back-navigation failed: {e}\n", "error")
        return None, None


def _attempt_recovery(page, row_data, payer_config=None, df=None):
    """Re-open claim search and re-run the search after a claim processing error.

    Returns a fresh iframe if successful, or None if recovery also fails.
    """
    try:
        log_to_gui("  → Attempting recovery navigation...\n", "error")
        page = _reload_and_navigate(page, payer_config)
        iframe = retry_with_backoff(
            "Recovery page ready",
            lambda: wait_for_page_ready(page, payer_config=payer_config),
            attempts=2,
        )
        fill_search_form(page, iframe, row_data, payer_config, df=df)
        submit_search_and_wait(iframe)
        return iframe
    except AvailitySessionInactiveError:
        raise
    except Exception as e:
        log_to_gui(f"  ❌ Recovery failed: {e}\n", "error")
        return None


def _format_merged_denial_reason(results):
    """Format Denial Reason when several claims merge into one invoice row.

    Number every claim, including identical reasons and ``--``, so Denial
    Reason lines up with the other numbered fields. Empty / None become ``--``.
    """
    values = [r.get('Denial Reason', '--') for r in results]
    return '\n'.join(
        f"{i + 1}. {value if value not in (None, '') else '--'}"
        for i, value in enumerate(values)
    )


def format_multi_claim_results(results, payer_config=None):
    """Merge multiple claim result dicts into a single dict with numbered values.

    When one invoice has several matching claims, all values are numbered
    and joined with newlines so the output CSV stays one row per invoice.

    If any claim is Need Manual Review, the merged row is Need Manual Review
    with other claim fields left as ``--``.
    """
    if not results:
        return empty_claim_result('No data', payer_config=payer_config)
    if any(
        str(row.get('Claim Status') or '') == NEED_MANUAL_REVIEW_STATUS
        for row in results
    ):
        return need_manual_review_result(payer_config)
    merged_fields = [
        f for f in resolved_output_columns(payer_config)
        if f not in ('Last Action Taken', 'Recheck')
    ]
    if len(results) == 1:
        return {field: results[0].get(field, '--') for field in merged_fields}
    merged = {}
    for field in merged_fields:
        if field == 'Denial Reason':
            merged[field] = _format_merged_denial_reason(results)
        else:
            merged[field] = '\n'.join(
                f"{i + 1}. {r.get(field, '--')}" for i, r in enumerate(results)
            )
    return merged


def _pending_siblings_for_seed(df, seed_index, payer_config):
    """Spreadsheet indexes filled from one invoice harvest."""
    if payer_matches_results_by_visit_and_amount(payer_config):
        return [seed_index]
    siblings = pending_invoice_sibling_indices(
        df, seed_index, should_process_fn=row_should_process,
        payer_config=payer_config,
    )
    return siblings or [seed_index]


def _write_need_manual_review_rows(
    df, indices, payer_config, output_folder, note, log_detail,
):
    """Write Claim Status Need Manual Review onto each index and save."""
    result = need_manual_review_result(payer_config, note)
    for idx in indices:
        write_row_result(df, idx, result)
        safe_mark_row_status(
            df, idx, 'Error', note or NEED_MANUAL_REVIEW_STATUS,
        )
        log_to_gui(
            f"  ⚠️ Row {idx + 1} complete ({log_detail})\n",
            "error",
        )
    save_progress_results(df, output_folder, force=True)
    return list(indices)


def write_invoice_snapshots_to_rows(
    df, seed_index, snapshots, payer_config, output_folder,
):
    """Materialize harvested snapshots onto every pending sibling invoice row.

    Returns the list of dataframe indices that were written. When no snapshot
    applies to a VisitDate, that row is marked claim-not-found (same status
    as a results-table miss).

    Payers that match results by VisitDate + Amount (Aetna) write only the
    seed row so same-day claims with different billed amounts stay distinct.
    """
    if payer_matches_results_by_visit_and_amount(payer_config):
        siblings = [seed_index]
    else:
        siblings = pending_invoice_sibling_indices(
            df, seed_index, should_process_fn=row_should_process,
            payer_config=payer_config,
        )
    if not siblings:
        siblings = [seed_index]

    invoice_number = safe_invoice_number(df.iloc[seed_index], 'InvoiceNumber', '')
    if len(siblings) > 1:
        log_to_gui(
            f"  → Filling {len(siblings)} pending row(s) for invoice "
            f"{invoice_number} from harvested claim detail\n",
            "info",
        )

    attach_invoice_alignment(snapshots, df, invoice_number, payer_config)

    for idx in siblings:
        visit_date = visit_date_for_row(df, idx)
        claims = materialize_row_results(
            snapshots, visit_date, payer_config, row_index=idx,
        )
        if not claims:
            unresolved = unresolved_alignment_result(snapshots, idx, payer_config)
            if unresolved is not None:
                write_row_result(df, idx, unresolved)
                safe_mark_row_status(
                    df, idx, 'Error', 'Line match needs manual review',
                )
                log_to_gui(
                    f"  ⚠️ Row {idx + 1} complete "
                    f"(line could not be matched — needs manual review)\n",
                    "error",
                )
                continue
            write_row_result(
                df, idx,
                _empty_claim_result_for_payer(
                    CLAIM_NOT_FOUND_STATUS, payer_config,
                ),
            )
            safe_mark_row_status(df, idx, 'Done', '')
            log_to_gui(
                f"  ✓ Row {idx + 1} complete "
                f"(no matching service line for VisitDate {visit_date})\n",
                "success",
            )
            continue
        errors = (
            validate_claim_results(claims, payer_config)
            if _result_validation_policy(payer_config)
            else []
        )
        write_row_result(df, idx, format_multi_claim_results(claims, payer_config))
        if str(df.at[idx, 'Claim Status'] or '') == NEED_MANUAL_REVIEW_STATUS:
            safe_mark_row_status(df, idx, 'Error', NEED_MANUAL_REVIEW_STATUS)
            log_to_gui(
                f"  ⚠️ Row {idx + 1} complete (Need Manual Review)\n",
                "error",
            )
            continue
        if errors:
            safe_mark_row_status(
                df, idx, 'Error', format_validation_errors(errors),
            )
            log_to_gui(
                f"  ⚠️ Row {idx + 1} failed extraction validation: "
                f"{format_validation_errors(errors)}\n",
                "error",
            )
            continue
        safe_mark_row_status(df, idx, 'Done', '')
        log_to_gui(f"  ✓ Row {idx + 1} complete\n", "success")

    save_progress_results(df, output_folder, force=len(siblings) > 1)
    return siblings


# ============================================================================
# SECTION 16 — ROW PROCESSING  (one CSV row = one invoice)
# ============================================================================

def _click_first_visible(iframe, selectors, timeout_ms=5000):
    """Click the first visible control among ``selectors``.

    Raises the last Playwright error if every selector fails.
    """
    last_error = None
    seen = []
    for selector in selectors:
        if not selector or selector in seen:
            continue
        seen.append(selector)
        loc = iframe.locator(selector)
        try:
            loc.first.wait_for(state='visible', timeout=timeout_ms)
            loc.first.click(timeout=timeout_ms)
            return selector
        except Exception as e:
            last_error = e
    raise last_error or RuntimeError('No selector to click')


def _search_text_input_empty(iframe, selector):
    """True when a search input has no non-whitespace value."""
    try:
        val = iframe.locator(selector).first.input_value(timeout=800)
        return not (val or '').strip()
    except Exception:
        return False


def _search_form_core_fields_empty(iframe, payer_config):
    """True when member, last name, and service dates look cleared.

    Provider NPI is ignored — Availity often keeps it filled across searches.
    """
    sel = get_selector_profile(payer_config)
    if not _search_text_input_empty(iframe, sel['member_id']):
        return False
    if not _search_text_input_empty(iframe, sel['last_name']):
        return False
    if _uses_plain_date_inputs(sel):
        for key in ('dob', 'date_from', 'date_to'):
            selector = sel.get(key)
            if selector and not _search_text_input_empty(iframe, selector):
                return False
        return True
    try:
        from_group = _resolve_mui_date_group(
            iframe, sel.get('service_from_date_label', 'Service From Date'),
        )
        to_group = _resolve_mui_date_group(
            iframe, sel.get('service_to_date_label', 'Service To Date'),
        )
        return (
            _mui_date_group_is_empty(from_group)
            and _mui_date_group_is_empty(to_group)
        )
    except Exception:
        return False


def _wait_for_search_form_cleared(iframe, payer_config, timeout_ms=10000):
    """Poll until Clear Form has emptied member/name/service dates."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        if _search_form_core_fields_empty(iframe, payer_config):
            return True
        time.sleep(0.1)
    raise RuntimeError('Search form still had values after Clear Form')


def _clear_search_form_and_wait(iframe, payer_config):
    """Click Clear Form, then wait until core fields are actually empty."""
    sel = get_selector_profile(payer_config)
    clear_selectors = (
        sel.get('clear_form'),
        sel.get('clear_form_fallback'),
        'button:has-text("Clear Form")',
    )
    clicked = _click_first_visible(iframe, clear_selectors, timeout_ms=8000)
    _wait_for_search_form_cleared(iframe, payer_config)
    return clicked


def _reset_search_via_edit_search(page, payer_config):
    """Return to a blank claim-search form via Edit Search + Clear Form.

    Uses SPA hash navigation inside the claim iframe — does not wait on a
    full page load. Raises on failure so the caller can fall back to reload.
    When Edit Search is missing (already on the search form), skips it and
    clicks Clear Form after the search fields are visible.
    """
    page = get_live_page(page)
    sel = get_selector_profile(payer_config)
    iframe = _claim_search_iframe(page, payer_config)
    edit_selectors = (sel.get('edit_search'), 'a:has-text("Edit Search")')

    edit_clicked = False
    try:
        _click_first_visible(iframe, edit_selectors, timeout_ms=5000)
        edit_clicked = True
        log_to_gui("  → Edit Search clicked — waiting for search fields...\n")
    except Exception:
        log_to_gui(
            "  ℹ️ Edit Search not found — trying Clear Form on current search form\n",
            "info",
        )

    if edit_clicked:
        wait_for_page_ready(page, payer_config=payer_config, quiet=True)
        iframe = _claim_search_iframe(page, payer_config)
    else:
        _wait_for_search_form_in_iframe(iframe, sel, timeout_ms=8000)

    log_to_gui("  → Clicking Clear Form...\n")
    _clear_search_form_and_wait(iframe, payer_config)
    wait_for_page_ready(page, payer_config=payer_config, quiet=True)
    return page


def _reset_search_for_next_row(page, payer_config, *, already_counted=False):
    """Reset to a clean claim-search form for the next invoice group.

    ``already_counted`` is True when the caller already ticked
    ``invoice_searches_since_remount`` (keep-table harvest that must remount).
    """
    try:
        log_to_gui("  → Resetting search page for next row...\n")
        if not already_counted:
            state.invoice_searches_since_remount += 1
        remount_due = (
            state.invoice_searches_since_remount >= CLAIM_SPA_REMOUNT_EVERY
        )
        if _payer_resets_via_edit_search(payer_config) and not remount_due:
            try:
                page = _reset_search_via_edit_search(page, payer_config)
                log_to_gui("  ✓ Search form reset via Edit Search\n", "success")
                return page
            except Exception as e:
                log_to_gui(
                    f"  ⚠️ Edit Search reset failed ({e}) — full reload\n",
                    "error",
                )
        elif remount_due:
            log_to_gui(
                f"  → Remounting claim search iframe "
                f"(every {CLAIM_SPA_REMOUNT_EVERY} invoice searches)...\n",
                "info",
            )
        page = _reload_and_navigate(page, payer_config)
        if remount_due:
            _recycle_open_remit_tab(page)
            state.invoice_searches_since_remount = 0
        retry_with_backoff(
            "Reset search page ready",
            lambda: wait_for_page_ready(page, payer_config=payer_config),
            attempts=3,
        )
        return page
    except Exception as e:
        log_to_gui(f"  ⚠️ Search page reset failed: {e}\n", "error")
        return page


def _reset_or_return_to_results(page, payer_config, *, skip_row_reset=False):
    """After a row completes, reset the search form or stay on #claimsTable.

    After an invoice's submissions are harvested, callers pass False so
    Edit Search + Clear Form runs from claim detail. ``skip_row_reset``
    stays on the results list only when ``#claimsTable`` is visible or the
    Back to Results link is on screen (claim detail after a successful
    harvest). Search alerts and portal errors always reset the form.

    Keep-table harvests still increment ``invoice_searches_since_remount``.
    When the remount interval is due, this remounts instead of keeping the
    table so a patient cluster cannot skip the SPA recycle.
    Back to Results between Availity submissions of the same invoice is
    handled inside ``process_all_claims_for_invoice``.
    """
    if skip_row_reset:
        state.invoice_searches_since_remount += 1
        remount_due = (
            state.invoice_searches_since_remount >= CLAIM_SPA_REMOUNT_EVERY
        )
        if remount_due:
            return _reset_search_for_next_row(
                page, payer_config, already_counted=True,
            )
        try:
            page = get_live_page(page)
            if _results_table_is_visible(page, payer_config):
                wait_for_claim_results_ready(
                    page, payer_config=payer_config, quiet=True,
                )
            elif _back_to_results_link_is_visible(page, payer_config):
                _return_to_results_view(page, payer_config)
            else:
                log_to_gui(
                    "  ℹ️ No results table or Back to Results — "
                    "resetting search form\n",
                    "info",
                )
                return _reset_search_for_next_row(
                    page, payer_config, already_counted=True,
                )
            sort_claim_results_by_finalized_date(
                page.frame_locator(get_selector_profile(payer_config)['iframe']),
            )
        except Exception as e:
            log_to_gui(
                f"  ⚠️ Could not return to results ({e}) — full reset\n",
                "error",
            )
            return _reset_search_for_next_row(
                page, payer_config, already_counted=True,
            )
        return page
    return _reset_search_for_next_row(page, payer_config)


def _finish_row_without_claims(
    page,
    df,
    row_index,
    output_folder,
    payer_config,
    *,
    claim_status,
    automation_status='Done',
    last_error='',
    success_log=None,
    reload_after=False,
    skip_row_reset=False,
    iframe=None,
    prepare_in_place=False,
):
    """Record a non-extraction row outcome, reset search, then save progress."""
    write_row_result(df, row_index, _empty_claim_result_for_payer(
        claim_status, payer_config,
    ))
    safe_mark_row_status(df, row_index, automation_status, last_error)
    if success_log:
        log_to_gui(success_log + "\n", "success")
    if prepare_in_place and iframe is not None:
        try:
            _prepare_search_form_for_next_row(page, iframe, payer_config)
        except Exception as e:
            log_to_gui(f"  ⚠️ Could not clear search form in place: {e}\n", "error")
    elif reload_after:
        page = _reset_or_return_to_results(
            page, payer_config, skip_row_reset=skip_row_reset,
        )
    save_progress_results(df, output_folder)
    return page


def process_one_row(page, df, row_index, row_data, output_folder, payer_config,
                    *, skip_initial_navigate=False, skip_search=False,
                    skip_row_reset=False, filled_indices_out=None,
                    search_meta_out=None):
    """Full pipeline for one CSV row: navigate → search → extract → save.

    After harvesting claim details, all still-pending spreadsheet rows that
    share this invoice are filled from the same snapshots.

    Returns ``(row_ok, page)`` where ``row_ok`` is True when the row was
    handled (including intentional skips).  ``page`` is reset to the claim
    search form after successful extraction so the next row starts clean,
    unless ``skip_row_reset`` keeps ``#claimsTable`` after a successful
    harvest for a grouped row. Search misses and harvest failures always
    reset the form.
    When ``filled_indices_out`` is a list, sibling indices written by
    invoice reuse are appended to it. When ``search_meta_out`` is a dict,
    ``date_fallback_attempted`` is written after the search step.
    """
    if not state.is_running:
        return False, page

    date_fallback_attempted = False

    # #region agent log
    if not getattr(state, '_dbg_first_row_logged', False):
        state._dbg_first_row_logged = True
        from availity_app import invoice_index as _ii
        _agent_log(
            'E',
            'automation.py:process_one_row',
            'first row start',
            {
                'row_index': int(row_index),
                'index_hits': _ii.index_hits,
                'index_rebuilds': _ii.index_rebuilds,
            },
        )
    # #endregion

    try:
        if row_is_in_billing_review_skip(df, row_index):
            df.at[row_index, 'Claim Status'] = IN_BILLING_REVIEW_STATUS
            df.at[row_index, 'Denial Reason'] = IN_BILLING_REVIEW_STATUS
            safe_mark_row_status(df, row_index, 'Done', '')
            log_to_gui(
                f"  ℹ️ Row {row_index + 1} skipped search "
                f"({IN_BILLING_REVIEW_STATUS}, Recheck=no)\n",
                "info",
            )
            save_progress_results(df, output_folder, force=True)
            return True, page

        if not row_should_process(df, row_index):
            log_to_gui(
                f"  ℹ️ Row {row_index + 1} already completed "
                "(invoice reuse) — skipping\n",
                "info",
            )
            return True, page

        log_to_gui(f"\n🔄 Row {row_index + 1}\n", "info")
        invoice_number = safe_invoice_number(row_data, 'InvoiceNumber')
        visit_date     = safe_field(row_data, 'VisitDate')
        row_amount     = safe_field(row_data, 'Amount')

        if not invoice_number:
            log_to_gui(f"  ⚠️ Row {row_index + 1}: InvoiceNumber is empty — skipping\n", "error")
            write_row_result(df, row_index, empty_claim_result('Missing InvoiceNumber'))
            safe_mark_row_status(df, row_index, 'Error', 'Missing InvoiceNumber')
            save_progress_results(df, output_folder, force=True)
            return False, page

        if payer_matches_results_by_visit_and_amount(payer_config):
            if not coerce_visit_date(visit_date):
                log_to_gui(
                    f"  ⚠️ Row {row_index + 1}: VisitDate is empty or invalid "
                    f"({visit_date!r}) — skipping\n",
                    "error",
                )
                write_row_result(df, row_index, empty_claim_result('Missing VisitDate'))
                safe_mark_row_status(df, row_index, 'Error', 'Missing VisitDate')
                save_progress_results(df, output_folder, force=True)
                return False, page
            if parse_amount(row_amount) is None:
                log_to_gui(
                    f"  ⚠️ Row {row_index + 1}: Amount is empty or invalid "
                    f"({row_amount!r}) — skipping\n",
                    "error",
                )
                write_row_result(df, row_index, empty_claim_result('Missing Amount'))
                safe_mark_row_status(df, row_index, 'Error', 'Missing Amount')
                save_progress_results(df, output_folder, force=True)
                return False, page

        if not row_has_valid_dob(row_data, safe_field):
            dob_raw = safe_field(row_data, 'DOB')
            log_to_gui(
                f"  ⚠️ Row {row_index + 1}: DOB missing or invalid "
                f"({dob_raw!r}) — skipping\n",
                "error",
            )
            page = _finish_row_without_claims(
                page, df, row_index, output_folder, payer_config,
                claim_status=DOB_NOT_FOUND_STATUS,
                automation_status='Skipped',
                success_log=f"  ✓ Row {row_index + 1} skipped ({DOB_NOT_FOUND_STATUS})",
                reload_after=False,
            )
            return True, page

        if payer_matches_results_by_visit_and_amount(payer_config):
            log_to_gui(
                f"  → Invoice: {invoice_number}  |  Visit: {visit_date}  "
                f"|  Amount: {row_amount}\n"
            )
        else:
            log_to_gui(f"  → Invoice: {invoice_number}  |  Visit: {visit_date}\n")

        searched_start, searched_end, from_aggregation = resolve_invoice_search_dates(
            df, row_data, safe_field, payer_config=payer_config,
        )
        date_fallback_attempted = False
        iframe = None

        def _maybe_date_fallback():
            nonlocal search_outcome, date_fallback_attempted
            nonlocal from_aggregation, searched_start, searched_end
            if date_fallback_attempted or iframe is None:
                return
            did_retry, new_outcome = apply_start_end_search_fallback(
                page, iframe, row_data, payer_config,
                from_aggregation=from_aggregation,
                searched_start=searched_start,
                searched_end=searched_end,
            )
            if not did_retry:
                return
            date_fallback_attempted = True
            from_aggregation = False
            searched_start, searched_end = resolve_service_search_dates(
                row_data, safe_field,
            )
            search_outcome = new_outcome
            if iframe is not None and _claims_table_has_results(iframe):
                search_outcome = 'results'

        def _finish_search_miss(**kwargs):
            # Portal errors / not-found / invalid member have no #claimsTable.
            # Never hunt Back to Results — clear the form for the next search.
            return _finish_row_without_claims(
                page, df, row_index, output_folder, payer_config,
                iframe=iframe,
                prepare_in_place=True,
                skip_row_reset=False,
                reload_after=False,
                **kwargs,
            )

        if skip_search:
            log_to_gui("  ℹ️ Reusing search results (grouped row)\n", "info")
            try:
                page = get_live_page(page)
                if not _results_table_is_visible(page, payer_config):
                    iframe = _return_to_results_view(page, payer_config)
                else:
                    iframe = wait_for_claim_results_ready(
                        page, payer_config=payer_config, quiet=True,
                    )
                sort_claim_results_by_finalized_date(iframe)
            except AvailitySessionInactiveError:
                raise
            except Exception as nav_error:
                log_to_gui(
                    f"  ❌ Could not reuse search results: {nav_error}\n",
                    "error",
                )
                write_row_result(df, row_index, empty_claim_result('Search failed'))
                safe_mark_row_status(
                    df, row_index, 'Error', f'Search failed: {nav_error}',
                )
                save_progress_results(df, output_folder, force=True)
                return False, page
            search_outcome = 'results'
        else:
            # First row in batch: session already verified.  All later rows reset
            # to the payer search URL regardless of spreadsheet row index.
            if skip_initial_navigate:
                try:
                    iframe = retry_with_backoff(
                        "Wait for page ready",
                        lambda: wait_for_page_ready(page, payer_config=payer_config),
                        attempts=3,
                    )
                except AvailitySessionInactiveError:
                    raise
                except Exception as nav_error:
                    log_to_gui(f"  ❌ Page setup failed: {nav_error}\n", "error")
                    write_row_result(df, row_index, empty_claim_result('Navigation failed'))
                    safe_mark_row_status(df, row_index, 'Error', f'Navigation failed: {nav_error}')
                    save_progress_results(df, output_folder, force=True)
                    return False, page
            else:
                page = _reload_and_navigate(page, payer_config)
                try:
                    iframe = retry_with_backoff(
                        "Wait for page ready",
                        lambda: wait_for_page_ready(page, payer_config=payer_config),
                        attempts=3,
                    )
                except AvailitySessionInactiveError:
                    raise
                except Exception as nav_error:
                    log_to_gui(f"  ❌ Page setup failed: {nav_error}\n", "error")
                    write_row_result(df, row_index, empty_claim_result('Navigation failed'))
                    safe_mark_row_status(df, row_index, 'Error', f'Navigation failed: {nav_error}')
                    save_progress_results(df, output_folder, force=True)
                    return False, page

            if not fill_search_form(page, iframe, row_data, payer_config, df=df):
                outcome, page, iframe = _handle_form_fill_failure(
                    page, iframe, df, row_index, row_data, output_folder, payer_config,
                )
                if outcome == 'failed':
                    return False, page
                if outcome == 'row_done':
                    return True, page

            search_outcome = submit_search_and_wait(iframe)
            if search_outcome == 'results':
                sort_claim_results_by_finalized_date(iframe)

        def _finish_from_search_outcome(outcome):
            if outcome == 'invalid_member_id':
                return _finish_search_miss(
                    claim_status=INVALID_MEMBER_ID_STATUS,
                    success_log=(
                        f"  ✓ Row {row_index + 1} complete "
                        f"({INVALID_MEMBER_ID_STATUS})"
                    ),
                )
            if outcome == 'not_found':
                return _finish_search_miss(
                    claim_status=CLAIM_NOT_FOUND_STATUS,
                    success_log=f"  ✓ Row {row_index + 1} complete (claim not found)",
                )
            if outcome == 'failed':
                raise AvailitySessionInactiveError(
                    'Search returned no results table or payer alert after retries'
                )
            if outcome != 'results':
                return _finish_search_miss(
                    claim_status=CLAIM_NOT_FOUND_STATUS,
                    success_log=(
                        f"  ✓ Row {row_index + 1} complete "
                        f"(search failed — moving on)"
                    ),
                )
            return None

        if search_outcome in _PORTAL_SEARCH_DATE_FALLBACK_OUTCOMES:
            _maybe_date_fallback()

        if search_outcome == 'failed':
            raise AvailitySessionInactiveError(
                'Search returned no results table or payer alert after retries'
            )

        finished = _finish_from_search_outcome(search_outcome)
        if finished is not None:
            return True, finished

        row_indices, match_mode = resolve_matching_claim_indices(
            iframe, df, invoice_number,
            visit_date=visit_date, payer_config=payer_config,
            narrow_by_visit_date=False,
            amount=row_amount,
        )
        if not row_indices:
            _maybe_date_fallback()
            if search_outcome == 'failed':
                raise AvailitySessionInactiveError(
                    'Search returned no results table or payer alert after retries'
                )
            finished = _finish_from_search_outcome(search_outcome)
            if finished is not None:
                return True, finished
            if search_outcome == 'results':
                row_indices, match_mode = resolve_matching_claim_indices(
                    iframe, df, invoice_number,
                    visit_date=visit_date, payer_config=payer_config,
                    narrow_by_visit_date=False,
                    amount=row_amount,
                )
            if not row_indices:
                page = _finish_search_miss(
                    claim_status=CLAIM_NOT_FOUND_STATUS,
                    success_log=(
                        f"  ✓ Row {row_index + 1} complete "
                        f"(claim not found in results)"
                    ),
                )
                return True, page

        if match_mode == 'service_dates':
            log_to_gui("  ℹ️ Bundle claim match (service dates)\n", "info")
        elif match_mode == 'visit_amount':
            log_to_gui("  ℹ️ Aetna match (VisitDate + Amount)\n", "info")

        snapshots, harvest_complete = process_all_claims_for_invoice(
            page, iframe, df, row_data, invoice_number, row_indices, payer_config,
            match_mode=match_mode,
            spreadsheet_row_index=row_index,
        )
        if not snapshots:
            write_row_result(df, row_index, empty_claim_result('Error'))
            safe_mark_row_status(df, row_index, 'Error', 'Claim harvest failed')
            save_progress_results(df, output_folder, force=True)
            log_to_gui(f"  ❌ Row {row_index + 1}: no claim detail harvested\n", "error")
            try:
                page = _reset_or_return_to_results(
                    page, payer_config, skip_row_reset=False,
                )
            except Exception:
                pass
            return False, page

        if not harvest_complete:
            expected = len(row_indices)
            harvested = len(snapshots)
            siblings = _pending_siblings_for_seed(df, row_index, payer_config)
            filled = _write_need_manual_review_rows(
                df, siblings, payer_config, output_folder,
                note=f"harvest incomplete {harvested}/{expected}",
                log_detail=(
                    f"harvest incomplete ({harvested}/{expected}) — "
                    "Need Manual Review"
                ),
            )
            if filled_indices_out is not None:
                for idx in filled:
                    if idx not in filled_indices_out:
                        filled_indices_out.append(idx)
            page = _reset_or_return_to_results(
                page, payer_config, skip_row_reset=False,
            )
            return True, page

        filled = write_invoice_snapshots_to_rows(
            df, row_index, snapshots, payer_config, output_folder,
        )
        if filled_indices_out is not None:
            for idx in filled:
                if idx not in filled_indices_out:
                    filled_indices_out.append(idx)
        page = _reset_or_return_to_results(
            page, payer_config, skip_row_reset=skip_row_reset,
        )
        return True, page

    except AvailitySessionInactiveError:
        raise
    except Exception as e:
        log_to_gui(f"  ❌ Row error: {e}\n", "error")
        log_to_gui(traceback.format_exc(), "error")
        write_row_result(df, row_index, empty_claim_result('Critical error'))
        safe_mark_row_status(df, row_index, 'Error', str(e))
        save_progress_results(df, output_folder, force=True)
        capture_row_failure_artifact(
            page, output_folder, row_index,
            {
                'error': str(e),
                'payer': state.current_payer_name or '',
                'invoice': safe_invoice_number(row_data, 'InvoiceNumber', ''),
            },
        )
        try:
            page = _reset_or_return_to_results(
                page, payer_config, skip_row_reset=False,
            )
        except Exception:
            pass
        return False, page
    finally:
        if search_meta_out is not None:
            search_meta_out['date_fallback_attempted'] = date_fallback_attempted


def _search_group_row_label(group_indices):
    """Human-readable row numbers for a search group (1-based)."""
    if not group_indices:
        return ''
    first = group_indices[0]
    last = group_indices[-1]
    if last - first + 1 == len(group_indices):
        return f"{first + 1}–{last + 1}"
    return ', '.join(str(i + 1) for i in group_indices)


def _later_group_members_pending(group_indices, current_idx, row_results, df):
    """True when a later group row still needs processing after ``current_idx``."""
    seen_current = False
    for idx in group_indices:
        if idx == current_idx:
            seen_current = True
            continue
        if not seen_current:
            continue
        if idx in row_results:
            continue
        if row_should_process(df, idx):
            return True
    return False


def process_search_group(page, df, group_indices, output_folder, payer_config,
                         *, skip_initial_navigate=False):
    """Run one claim search for ``group_indices`` then extract each row.

    The first row harvests claim details and fans out to all pending sibling
    invoice rows. Remaining group members that were filled are skipped.

    When ``group_search_by_patient`` is on and later group members still need
    ``#claimsTable``, harvest returns to results instead of clearing the form.
    A StartDate/EndDate fallback on the first row disables table reuse.

    Returns ``(all_ok, page, row_results)`` where ``row_results`` maps each
    index to True/False (handled ok vs error), including extra siblings
    filled outside the group.
    """
    if not group_indices:
        return True, page, {}

    row_results = {}
    first_idx = group_indices[0]
    filled = []
    patient_mode = supports_patient_search_grouping(payer_config)
    first_skip_reset = bool(
        patient_mode
        and _later_group_members_pending(group_indices, first_idx, {}, df)
    )
    search_meta = {}

    log_to_gui(
        f"\n📋 Search group: {len(group_indices)} row(s) "
        f"({_search_group_row_label(group_indices)}) sharing one search\n",
        "info",
    )

    row_ok, page = process_one_row(
        page, df, first_idx, df.iloc[first_idx], output_folder, payer_config,
        skip_initial_navigate=skip_initial_navigate,
        skip_search=False,
        skip_row_reset=first_skip_reset,
        filled_indices_out=filled,
        search_meta_out=search_meta,
    )
    row_results[first_idx] = row_ok
    for idx in filled:
        row_results.setdefault(idx, row_ok)

    if not row_ok:
        for idx in group_indices[1:]:
            if not row_should_process(df, idx):
                row_results[idx] = True
                continue
            solo_ok, page = process_one_row(
                page, df, idx, df.iloc[idx], output_folder, payer_config,
                skip_initial_navigate=True,
            )
            row_results[idx] = solo_ok
        return all(row_results.values()), page, row_results

    fallback = bool(search_meta.get('date_fallback_attempted'))
    reuse_results = _results_table_is_visible(page, payer_config)
    if fallback:
        reuse_results = False
        if first_skip_reset:
            log_to_gui(
                "  ℹ️ Date fallback ran — remaining grouped rows will search "
                "themselves\n",
                "info",
            )
            try:
                page = _reset_or_return_to_results(
                    page, payer_config, skip_row_reset=False,
                )
            except Exception as e:
                log_to_gui(
                    f"  ⚠️ Could not reset after date fallback ({e})\n",
                    "error",
                )
    left_on_results = bool(first_skip_reset and not fallback and reuse_results)
    if not reuse_results and not fallback:
        log_to_gui(
            "  ℹ️ No results table after first grouped row — "
            "remaining rows will search in place\n",
            "info",
        )

    for idx in group_indices[1:]:
        if not state.is_running:
            break
        if idx in row_results:
            continue
        if not row_should_process(df, idx):
            log_to_gui(
                f"  ℹ️ Row {idx + 1} already completed (invoice reuse) — skipping\n",
                "info",
            )
            row_results[idx] = True
            continue
        later_pending = _later_group_members_pending(
            group_indices, idx, row_results, df,
        )
        skip_reset = bool(patient_mode and reuse_results and later_pending)
        row_ok, page = process_one_row(
            page, df, idx, df.iloc[idx], output_folder, payer_config,
            skip_initial_navigate=True,
            skip_search=reuse_results,
            skip_row_reset=skip_reset,
            filled_indices_out=filled,
        )
        if not row_ok:
            log_to_gui(
                f"  ↻ Row {idx + 1} failed in group — retrying with solo search\n",
                "info",
            )
            row_ok, page = process_one_row(
                page, df, idx, df.iloc[idx], output_folder, payer_config,
                skip_initial_navigate=True,
                filled_indices_out=filled,
            )
            left_on_results = False
        elif reuse_results and not _results_table_is_visible(page, payer_config):
            reuse_results = False
            left_on_results = False
        else:
            left_on_results = skip_reset
        row_results[idx] = row_ok
        for extra_idx in filled:
            row_results.setdefault(extra_idx, row_ok)

    if left_on_results:
        try:
            page = _reset_or_return_to_results(
                page, payer_config, skip_row_reset=False,
            )
        except Exception as e:
            log_to_gui(
                f"  ⚠️ Could not reset after patient search group ({e})\n",
                "error",
            )

    all_ok = all(row_results.get(i, False) for i in group_indices)
    return all_ok, page, row_results


def _goto_navigation_home(page):
    """Unload the embedded Availity app so the next payer load remounts the iframe."""
    page.goto(
        AVAILITY_NAVIGATION_HOME,
        wait_until='domcontentloaded',
        timeout=45000,
    )
    page.wait_for_load_state('domcontentloaded', timeout=15000)
    return page


def _reload_and_navigate(page, payer_config):
    """Reset to a clean payer claim-search form via two-hop shell navigation.

    A same-URL ``goto`` from claim detail often leaves ``iframe#newBodyFrame`` on
    the detail route.  Unloading the navigation shell first forces a fresh app load.
    Breadcrumb shortcuts are not used here — MUI service-date pickers keep stale
    values after a prior search and reject in-place date changes.
    """
    log_to_gui("  → Navigating to search page...\n")
    page = retry_with_backoff(
        "Acquire live page",
        lambda: get_live_page(page),
        attempts=3,
    )
    retry_with_backoff(
        "Unload Availity app shell",
        lambda: _goto_navigation_home(page),
        attempts=3,
    )
    page = navigate_to_payer_page(page, payer_config)
    log_to_gui("  ✓ Navigation complete\n", "success")
    return page


# ============================================================================
# SECTION 17 — BATCH PROCESSING
# ============================================================================

def open_edge_and_connect(payer_name, excel_path, output_folder=None):
    """Phase 1: load Excel, then launch Edge with CDP and normalize tabs.

    Runs in a background thread (spawned by the GUI). Excel is read before any
    browser work. The user signs in to Availity manually in Edge; only after
    they click Start Automation do we navigate to claim search and process rows.

    Note on threading: ``sync_playwright()`` objects are bound to the thread
    that called ``start()`` and cannot be used (or even ``stop()``-ed) from
    another thread. So we attach Playwright here only long enough to bring
    the right tab forward, then release it on this same thread. Phase 2
    re-connects fresh on its own worker thread.
    """
    state.worker_busy = True
    user_stopped = False
    output_folder = output_folder or os.path.dirname(os.path.abspath(excel_path))
    try:
        payer_config = resolve_payer_config(payer_name)
        if not payer_config:
            log_to_gui(f"❌ Unknown payer '{payer_name}'\n", "error")
            _set_ui_state(UI_STATE_IDLE)
            return

        log_to_gui("📂 Importing collection file into SQLite...\n", "info")
        from availity_app.run_store import RunStore, open_or_import
        try:
            def _import_progress(count):
                if not state.is_running:
                    raise InterruptedError('Import stopped by user.')
                log_to_gui(f"  → Imported {count:,} rows...\n", "info")

            run_path = open_or_import(
                excel_path, output_folder, payer_name, payer_config,
                progress=_import_progress,
            )
            run = RunStore(run_path)
            stats = run.meta['stats']
            run.close()
        except Exception as exc:
            log_to_gui(f"❌ Could not import collection file: {exc}\n", "error")
            state.is_running = False
            _set_ui_state(UI_STATE_IDLE)
            return

        state.loaded_run_path = run_path
        state.loaded_run_output_folder = os.path.abspath(output_folder)
        state.loaded_excel_path = excel_path
        state.excel_row_stats = stats
        update_excel_row_stats(stats)
        log_to_gui(f"✓ Loaded {stats['total']} row(s)\n", "success")
        log_to_gui(
            f"   • Total rows read: {stats['total']}\n"
            f"   • Rows with Recheck = yes: {stats['recheck_yes']}\n"
            f"   • Rows with Recheck empty (new): {stats['recheck_empty']}\n",
            "info",
        )
        if not state.is_running:
            return

        log_to_gui("🌐 Starting Edge browser...\n", "info")
        if not ensure_edge_with_cdp():
            state.is_running = False
            _set_ui_state(UI_STATE_IDLE)
            return
        if not state.is_running:
            return

        log_to_gui("🌐 Connecting to browser via CDP...\n", "info")
        page = setup_browser()
        # Release Playwright on the same thread that started it. Phase 2 will
        # re-attach on its own worker thread before it does any work.
        ensure_playwright_disconnected()

        if not state.is_running:
            return

        state.selected_payer_config = payer_config
        state.selected_payer_name = payer_name
        state.current_payer_name = payer_name
        org_id, payer_id = parse_org_payer_ids(payer_config.get('url', ''))
        if org_id or payer_id:
            log_to_gui(f"   • OrgID: {org_id}  Payer ID: {payer_id}\n", "info")
        log_to_gui("✓ Browser connected\n", "success")
        log_to_gui(
            "✓ Edge ready. Please sign in to Availity in Edge, then click "
            "Start Automation.\n",
            "success",
        )
        _set_ui_state(UI_STATE_READY)
    except Exception as e:
        log_to_gui(f"❌ Could not open Edge: {e}\n", "error")
        log_to_gui(traceback.format_exc(), "error")
        state.is_running = False
        _set_ui_state(UI_STATE_IDLE)
    finally:
        user_stopped = not state.is_running
        state.worker_busy = False
        ensure_playwright_disconnected()
        if user_stopped:
            close_managed_edge_if_owned()
            reset_ui_state(clear_all=False)




def run_batch(batch_size, excel_path, output_folder):
    """Phase 2: process rows from the Excel loaded in Phase 1.

    Expects ``state.selected_payer_config`` and ``state.loaded_df`` to be set by
    ``open_edge_and_connect`` and Edge to still be running. Re-attaches
    Playwright on this worker thread because Playwright sync objects can't
    cross threads.
    """
    df = None
    should_close_browser = False
    page = None
    payer_config = state.selected_payer_config
    user_stopped = False

    state.worker_busy = True
    state.stop_save_requested = False
    state.invoice_searches_since_remount = 0
    state.rows_since_last_save = 0
    state.last_save_monotonic = time.monotonic()
    state.dirty_row_indices = set()
    state._large_autofit_notice_logged = False
    state._dbg_first_row_logged = False
    from availity_app import invoice_index as _inv_reset
    _inv_reset.index_hits = 0
    _inv_reset.index_rebuilds = 0
    try:
        if payer_config is None:
            log_to_gui(
                "❌ Browser is not open. Click Open Edge & Login first.\n",
                "error",
            )
            return

        payer_name = state.selected_payer_name or ''
        if not payer_name:
            for name, cfg in PAYER_CONFIG.items():
                if cfg is payer_config:
                    payer_name = name
                    break
        if not payer_name:
            payer_name = payer_config.get('display_name') or payer_config.get('name') or ''
        state.current_payer_name = payer_name
        resolved = resolve_payer_config(payer_name)
        if resolved is not None:
            payer_config = resolved
            state.selected_payer_config = payer_config

        from availity_app.run_store import RunStore, open_or_import
        run_path = state.loaded_run_path
        if not run_path or state.loaded_excel_path != excel_path:
            run_path = open_or_import(excel_path, output_folder, payer_name, payer_config)
            state.loaded_run_path = run_path
            state.loaded_excel_path = excel_path
        df = RunStore(run_path)
        df.recover_in_progress()
        state.excel_row_stats = df.meta['stats']

        stats = state.excel_row_stats or compute_recheck_stats(df)
        update_excel_row_stats(stats)
        log_to_gui(
            f"✓ Using {stats.get('total', len(df))} row(s) — "
            f"Recheck yes: {stats.get('recheck_yes', 0)}, "
            f"new (empty Recheck): {stats.get('recheck_empty', 0)}\n",
            "success",
        )

        log_to_gui("▶ Starting automation...\n", "info")
        log_to_gui("  → Attaching Playwright to Edge...\n", "info")
        page = setup_browser()

        circuit_breaker = BatchCircuitBreaker()
        log_to_gui("  → Verifying Availity session...\n", "info")
        try:
            ok, page = _verify_payer_search_session(page, payer_config, quiet=True)
        except AvailitySessionInactiveError as session_err:
            recovered, page = _await_user_session_recovery(
                page, payer_config, circuit_breaker, session_err.reason,
            )
            if not recovered:
                return
            ok = True
        if not ok:
            log_to_gui(
                "❌ Could not open the payer claim search. Sign in to Availity "
                "in Edge, then click Start Automation again.\n",
                "error",
            )
            return
        log_to_gui("✓ Session verified — claim search is open.\n", "success")
        if payer_config.get('uses_remittance'):
            try:
                _ensure_remit_page(page)
            except Exception as e:
                log_to_gui(
                    f"  ⚠️ Could not open remittance viewer: {e}\n",
                    "error",
                )

        # #region agent log
        from availity_app import invoice_index as _inv_idx
        _post_remit = time.perf_counter()
        _agent_log(
            'D',
            'automation.py:run_batch',
            'post-remit before apply/queue',
            {
                'nrows': len(df),
                'ncols': len(df.columns),
                'index_hits': _inv_idx.index_hits,
                'index_rebuilds': _inv_idx.index_rebuilds,
                'has_index': bool(getattr(df, 'attrs', {}).get('_availity_invoice_index')),
            },
        )
        # #endregion
        if not getattr(df, 'is_run_store', False):
            df = apply_claim_not_found_completed_rows(df)
            df = queue_recheck_yes_rows(df)
            df = queue_error_rows_for_retry(df)
        # #region agent log
        _after_apply = time.perf_counter()
        _agent_log(
            'D',
            'automation.py:run_batch',
            'after apply/queue loops',
            {'ms': round((_after_apply - _post_remit) * 1000, 1), 'nrows': len(df)},
        )
        # #endregion

        # Expose for emergency save from the GUI thread (Stop button).
        state.current_batch_df = df
        state.current_batch_output_folder = output_folder
        if not getattr(df, 'is_run_store', False):
            start_save_worker()
        pending_indices = _collect_pending_indices(df, limit=batch_size)
        work_queue = list(pending_indices)
        # #region agent log
        _after_pending = time.perf_counter()
        _agent_log(
            'C',
            'automation.py:run_batch',
            'after pending_indices',
            {
                'ms': round((_after_pending - _after_apply) * 1000, 1),
                'pending': len(pending_indices),
                'queued': len(work_queue),
                'batch_size': batch_size,
            },
        )
        # #endregion
        initial_queued = len(work_queue)
        rows_to_process = len(work_queue)
        batch_error_indices = set()

        if rows_to_process == 0:
            log_to_gui(
                "✓ No rows to process (all Done/Skipped with no prior errors).\n",
                "success",
            )
            reset_automation_progress("Nothing to process — all rows done", 100)
            should_close_browser = True
            _run_finalize_save(df, output_folder)
            return

        log_to_gui(
            f"\n📦 Processing {initial_queued} pending row(s)"
            f"{' for ' + payer_name if payer_name else ''} (resume-aware)...\n",
            "info"
        )
        log_to_gui("-" * 60 + "\n")

        update_automation_progress(
            0,
            rows_to_process,
            f"Starting batch — {rows_to_process} claim(s) queued",
        )

        attempted_count = 0
        error_counts = {}
        search_form_ready = True
        # #region agent log
        _before_groups = time.perf_counter()
        # #endregion
        search_groups = build_consecutive_search_groups(work_queue, df, payer_config)
        # #region agent log
        from availity_app import invoice_index as _inv_idx2
        _agent_log(
            'B',
            'automation.py:run_batch',
            'after search grouping',
            {
                'ms': round((time.perf_counter() - _before_groups) * 1000, 1),
                'groups': len(search_groups),
                'queued': len(work_queue),
                'index_hits': _inv_idx2.index_hits,
                'index_rebuilds': _inv_idx2.index_rebuilds,
            },
        )
        # #endregion
        group_pos = 0

        def _record_batch_row(idx, row_ok, page_ref):
            nonlocal page, search_form_ready
            if row_ok:
                search_form_ready = True
                batch_error_indices.discard(idx)
                circuit_breaker.record_row_success()
            else:
                search_form_ready = False
                batch_error_indices.add(idx)
                failure_stage = classify_row_failure(
                    df.at[idx, 'LastError'],
                    df.at[idx, 'Claim Status'],
                )
                circuit_breaker.record_transient_failure(failure_stage)
                capture_row_failure_artifact(
                    page_ref, output_folder, idx,
                    {
                        'payer': payer_name,
                        'invoice': safe_invoice_number(df.iloc[idx], 'InvoiceNumber', ''),
                        'last_error': str(df.at[idx, 'LastError']),
                        'failure_stage': failure_stage,
                    },
                )
            if not row_ok and str(df.at[idx, 'AutomationStatus']).strip().lower() != 'error':
                safe_mark_row_status(df, idx, 'Error', 'Unknown row processing error')
                save_progress_results(df, output_folder, force=True)
            _record_error(df, idx, error_counts)

        counted_indices = set()

        def _apply_filled_progress(row_results_map):
            nonlocal attempted_count, rows_to_process
            queued = set(work_queue)
            for row_idx, row_ok in row_results_map.items():
                if row_idx in counted_indices:
                    continue
                counted_indices.add(row_idx)
                if row_idx not in queued:
                    rows_to_process += 1
                    extra_invoice = safe_invoice_number(
                        df.iloc[row_idx], 'InvoiceNumber', '',
                    )
                    log_to_gui(
                        f"  ℹ️ Counted sibling row {row_idx + 1} "
                        f"(invoice {extra_invoice}) filled outside the current batch\n",
                        "info",
                    )
                attempted_count += 1
                _record_batch_row(row_idx, row_ok, page)
                update_automation_progress(
                    attempted_count,
                    rows_to_process,
                    f"Completed {attempted_count} of {rows_to_process} attempts — "
                    f"row {row_idx + 1}"
                    + (
                        f" — Invoice {safe_invoice_number(df.iloc[row_idx], 'InvoiceNumber', '')}"
                        if safe_invoice_number(df.iloc[row_idx], 'InvoiceNumber', '')
                        else ""
                    ),
                )

        while group_pos < len(search_groups):
            group = search_groups[group_pos]
            idx = group[0]
            if not state.is_running:
                log_to_gui("⚠ Stopped by user\n", "error")
                break

            if circuit_breaker.tripped:
                log_to_gui(
                    f"⚠ Circuit breaker tripped: {circuit_breaker.trip_reason}\n",
                    "error",
                )
                break

            if all(not row_should_process(df, i) for i in group):
                log_to_gui(
                    f"  ℹ️ Skipping search group rows "
                    f"{_search_group_row_label(group)} "
                    "(already filled via invoice reuse)\n",
                    "info",
                )
                group_pos += 1
                continue

            active_page = _find_logged_in_page(page)
            if active_page is not None:
                page = active_page
            ready, reason = validate_navigation_page_ready(page, timeout_ms=2500)
            if not ready:
                recovered, page = _await_user_session_recovery(
                    page, payer_config, circuit_breaker, reason,
                )
                if not recovered:
                    break
                search_form_ready = True

                prior_errors = sorted(i for i in batch_error_indices if i != idx)
                if prior_errors:
                    log_to_gui(
                        f"  ↻ Resuming: retrying {len(prior_errors)} prior error row(s), "
                        f"then row {idx + 1} and continuing…\n",
                        "info",
                    )
                flat_tail = [i for g in search_groups[group_pos:] for i in g]
                work_queue = _dedupe_preserve_order(sorted(batch_error_indices) + flat_tail)
                rows_to_process = len(work_queue)
                search_groups = build_consecutive_search_groups(work_queue, df, payer_config)
                group_pos = 0
                continue

            if len(group) >= 2:
                progress_label = (
                    f"Processing search group rows {group[0] + 1}–{group[-1] + 1} "
                    f"({len(group)} rows, one search)"
                )
            else:
                is_prior_error_retry = idx in batch_error_indices
                progress_label = (
                    f"Retrying row {idx + 1} (prior error)"
                    if is_prior_error_retry
                    else f"Processing row {idx + 1}"
                )
            invoice_number = safe_invoice_number(df.iloc[idx], 'InvoiceNumber', '')
            update_automation_progress(
                attempted_count,
                rows_to_process,
                progress_label
                + (f" — Invoice {invoice_number}" if invoice_number else ""),
            )

            try:
                if len(group) >= 2:
                    for row_idx in group:
                        if row_should_process(df, row_idx):
                            safe_mark_row_status(df, row_idx, 'InProgress', '')
                    _, page, row_results = process_search_group(
                        page, df, group, output_folder, payer_config,
                        skip_initial_navigate=search_form_ready,
                    )
                    _apply_filled_progress(row_results)
                    if circuit_breaker.tripped:
                        break
                else:
                    safe_mark_row_status(df, idx, 'InProgress', '')
                    filled = []
                    row_ok, page = process_one_row(
                        page, df, idx, df.iloc[idx], output_folder, payer_config,
                        skip_initial_navigate=search_form_ready,
                        filled_indices_out=filled,
                    )
                    row_results = {idx: row_ok}
                    for extra_idx in filled:
                        row_results.setdefault(extra_idx, row_ok)
                    _apply_filled_progress(row_results)
            except AvailitySessionInactiveError as session_err:
                recovered, page = _await_user_session_recovery(
                    page, payer_config, circuit_breaker, session_err.reason,
                )
                if not recovered:
                    break
                search_form_ready = True
                prior_errors = sorted(i for i in batch_error_indices if i != idx)
                if prior_errors:
                    log_to_gui(
                        f"  ↻ Resuming: retrying {len(prior_errors)} prior error row(s), "
                        f"then row {idx + 1} and continuing…\n",
                        "info",
                    )
                flat_tail = [i for g in search_groups[group_pos:] for i in g]
                work_queue = _dedupe_preserve_order(sorted(batch_error_indices) + flat_tail)
                rows_to_process = len(work_queue)
                search_groups = build_consecutive_search_groups(work_queue, df, payer_config)
                group_pos = 0
                continue

            if circuit_breaker.tripped:
                log_to_gui(
                    f"⚠ Circuit breaker tripped: {circuit_breaker.trip_reason}\n",
                    "error",
                )
                break

            group_pos += 1

        log_to_gui("-" * 60 + "\n")
        if getattr(df, 'is_run_store', False) or not state.stop_save_requested:
            _run_finalize_save(df, output_folder)
        else:
            log_to_gui("  ℹ️ Final save handled by Stop — skipping duplicate.\n", "info")

        batch_complete = group_pos >= len(search_groups) and state.is_running
        if batch_complete:
            log_to_gui(
                f"✓ Complete! {attempted_count} attempt(s), "
                f"{initial_queued} initially queued\n",
                "success",
            )
            finish_automation_progress(attempted_count, rows_to_process, complete=True)
            should_close_browser = True
        else:
            log_to_gui(
                f"⚠ Stopped. {attempted_count}/{rows_to_process} attempt(s) saved\n",
                "error",
            )
            finish_automation_progress(attempted_count, rows_to_process, stopped=True)

        if error_counts:
            log_to_gui("\n📊 Error summary:\n", "error")
            for reason, count in sorted(error_counts.items(), key=lambda x: -x[1]):
                log_to_gui(f"   • {reason}: {count} row(s)\n", "error")

    except Exception as e:
        log_to_gui(f"❌ Critical batch error: {e}\n", "error")
        log_to_gui(traceback.format_exc(), "error")
        if df is not None and (getattr(df, 'is_run_store', False) or not state.stop_save_requested):
            _run_finalize_save(df, output_folder)
    finally:
        user_stopped = not state.is_running
        state.worker_busy = False
        if getattr(df, 'is_run_store', False):
            df.close()
        else:
            stop_save_worker(wait=True)
        state.current_batch_df = None
        state.current_batch_output_folder = None
        state.current_payer_name = None
        state.loaded_df = None
        state.loaded_excel_path = None
        state.excel_row_stats = None
        _close_persistent_remit_page()
        ensure_playwright_disconnected()
        if should_close_browser or user_stopped:
            close_managed_edge_if_owned()
        reset_ui_state(clear_all=user_stopped)


def _record_error(df, row_index, error_counts):
    """Increment the counter for a known error status written into Claim Status."""
    status_val = df.at[row_index, 'Claim Status']
    if not isinstance(status_val, str):
        return
    for line in status_val.split('\n'):
        val = line.split('. ', 1)[-1].strip()
        if val in KNOWN_ERROR_STATUSES:
            error_counts[val] = error_counts.get(val, 0) + 1

