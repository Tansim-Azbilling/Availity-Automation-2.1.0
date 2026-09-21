"""Append-only JSONL progress log between formatted Automated.xlsx compiles."""
import json
import os

import pandas as pd

from availity_app.constants import STATUS_COLUMNS, resolved_output_columns
from availity_app.logging_gui import log_to_gui
from availity_app.utils import (
    normalize_search_date,
    safe_alt_patient_id,
    safe_field,
    safe_invoice_number,
)

WAL_FILENAME = 'Automated_progress.jsonl'


def wal_path(output_folder):
    """Return the JSONL path for ``output_folder``."""
    return os.path.join(output_folder, WAL_FILENAME)


def _json_safe(value):
    """Convert a dataframe cell to a JSON-serializable value."""
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _visit_identity(row):
    """Normalized VisitDate used to match a WAL line back to a spreadsheet row."""
    normalized = normalize_search_date(safe_field(row, 'VisitDate'))
    if normalized:
        return normalized
    return str(safe_field(row, 'VisitDate') or '')


def row_wal_identity(df, idx):
    """Identity fields stored with each WAL line."""
    row = df.iloc[idx]
    return {
        'idx': int(idx),
        'invoice': safe_invoice_number(row, 'InvoiceNumber'),
        'visit': _visit_identity(row),
        'member': safe_alt_patient_id(row, 'AltPatientID'),
    }


def _wal_fields_for_row(df, idx):
    """Output and status column values for one row."""
    fields = {}
    columns = list(resolved_output_columns()) + list(STATUS_COLUMNS)
    for col in columns:
        if col in df.columns:
            fields[col] = _json_safe(df.at[idx, col])
    return fields


def _identity_matches(df, idx, rec):
    """True when spreadsheet row ``idx`` matches WAL identity fields."""
    row = df.iloc[idx]
    return (
        safe_invoice_number(row, 'InvoiceNumber') == str(rec.get('invoice') or '')
        and _visit_identity(row) == str(rec.get('visit') or '')
        and safe_alt_patient_id(row, 'AltPatientID') == str(rec.get('member') or '')
    )


def _resolve_replay_index(df, rec):
    """Return a unique dataframe index for ``rec``, or None if ambiguous."""
    from availity_app.utils import invoice_row_indices

    idx = rec.get('idx')
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = None
    if idx is not None and 0 <= idx < len(df) and _identity_matches(df, idx, rec):
        return idx

    target_inv = str(rec.get('invoice') or '')
    target_visit = str(rec.get('visit') or '')
    target_member = str(rec.get('member') or '')
    candidates = (
        invoice_row_indices(df, target_inv) if target_inv else list(range(len(df)))
    )
    matches = []
    for i in candidates:
        row = df.iloc[i]
        if (
            _visit_identity(row) == target_visit
            and safe_alt_patient_id(row, 'AltPatientID') == target_member
        ):
            matches.append(i)
    if len(matches) == 1:
        return matches[0]
    return None


def append_wal_rows(df, output_folder, indices):
    """Append one JSON line per index. No-op when folder or indices are empty."""
    if df is None or not output_folder or not indices:
        return
    path = wal_path(output_folder)
    try:
        os.makedirs(output_folder, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            for idx in indices:
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    continue
                if idx < 0 or idx >= len(df):
                    continue
                record = row_wal_identity(df, idx)
                record['fields'] = _wal_fields_for_row(df, idx)
                handle.write(json.dumps(record, default=str) + '\n')
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    except OSError as exc:
        log_to_gui(f"  ⚠️ Could not write progress log: {exc}\n", "error")


def clear_wal(output_folder):
    """Delete the JSONL after a successful formatted Excel checkpoint."""
    if not output_folder:
        return
    path = wal_path(output_folder)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        log_to_gui(f"  ⚠️ Could not clear progress log: {exc}\n", "error")


def replay_wal(df, output_folder):
    """Apply leftover JSONL lines onto ``df``. Returns the number of applied lines."""
    if df is None or not output_folder:
        return 0
    path = wal_path(output_folder)
    if not os.path.exists(path):
        return 0

    applied = 0
    skipped = 0
    try:
        with open(path, encoding='utf-8') as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                idx = _resolve_replay_index(df, rec)
                if idx is None:
                    skipped += 1
                    continue
                fields = rec.get('fields') or {}
                if not isinstance(fields, dict):
                    skipped += 1
                    continue
                for col, value in fields.items():
                    if col in df.columns:
                        df.at[idx, col] = value
                applied += 1
    except OSError as exc:
        log_to_gui(f"  ⚠️ Could not read progress log: {exc}\n", "error")
        return applied

    if skipped:
        log_to_gui(
            f"  ⚠️ Skipped {skipped} unusable progress log line(s)\n",
            "error",
        )
    return applied
