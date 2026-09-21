"""Group rows that share the same claim-search criteria."""
from collections import defaultdict

import pandas as pd

from availity_app.utils import (
    normalize_dob,
    normalize_patient_names,
    normalize_search_date,
    resolve_invoice_search_dates,
    row_has_valid_dob,
    safe_alt_patient_id,
    safe_field,
    safe_invoice_number,
)

PATIENT_SEARCH_CLUSTER_DAYS = 90
_PATIENT_CLUSTER_ATTR = '_availity_patient_clusters'


def supports_search_grouping(payer_config):
    """Return True when this payer may reuse one search for multiple rows."""
    if not payer_config:
        return False
    return payer_config.get('supports_search_grouping', True)


def supports_patient_search_grouping(payer_config):
    """True when search dates and groups use patient 90-day VisitDate clusters."""
    return (
        supports_search_grouping(payer_config)
        and bool(payer_config.get('group_search_by_patient'))
    )


def patient_identity_key(row, safe_field_fn=safe_field):
    """Normalized (member, last, first, dob) identifying one patient."""
    member_id = safe_alt_patient_id(row, 'AltPatientID')
    last_name, first_name = normalize_patient_names(
        safe_field_fn(row, 'Last_Name'),
        safe_field_fn(row, 'First_Name'),
    )
    dob = normalize_dob(safe_field_fn(row, 'DOB'))
    return (member_id, last_name, first_name, dob)


def cluster_visit_dates(parsed_dates, max_days=PATIENT_SEARCH_CLUSTER_DAYS):
    """Greedy clusters of datetimes; each span is at most ``max_days`` from start.

    ``parsed_dates`` are timezone-naive pandas/datetime values. Returns a list
    of ``(start_dt, end_dt)`` pairs inclusive of every date in the cluster.
    """
    unique = sorted({d for d in parsed_dates if d is not None})
    if not unique:
        return []
    clusters = []
    start = unique[0]
    end = unique[0]
    for dt in unique[1:]:
        if (dt - start).days <= max_days:
            end = dt
        else:
            clusters.append((start, end))
            start = dt
            end = dt
    clusters.append((start, end))
    return clusters


def _parse_visit_datetime(value):
    """Return a datetime for a VisitDate cell, or None if unusable."""
    normalized = normalize_search_date(value)
    if not normalized:
        return None
    try:
        dt = pd.to_datetime(normalized, format='%m/%d/%Y')
        if pd.isna(dt):
            return None
        return dt
    except Exception:
        return None


def _cluster_range_containing(visit_dt, clusters):
    """Return ``(start_str, end_str)`` for the cluster that contains ``visit_dt``."""
    for start, end in clusters:
        if start <= visit_dt <= end:
            return start.strftime('%m/%d/%Y'), end.strftime('%m/%d/%Y')
    return '', ''


def _build_patient_cluster_map(df, safe_field_fn=safe_field):
    """Map patient identity to greedy 90-day VisitDate clusters for ``df``."""
    by_patient = defaultdict(list)
    if df is None or len(df) == 0:
        return {}
    for i in range(len(df)):
        row = df.iloc[i]
        identity = patient_identity_key(row, safe_field_fn)
        if not identity[0] or not identity[1]:
            continue
        visit_dt = _parse_visit_datetime(safe_field_fn(row, 'VisitDate'))
        if visit_dt is None:
            continue
        by_patient[identity].append(visit_dt)
    clusters_by_identity = {}
    for identity, dates in by_patient.items():
        clusters_by_identity[identity] = cluster_visit_dates(dates)
    return clusters_by_identity


def _patient_cluster_cache(df, safe_field_fn=safe_field):
    """Return cached identity→clusters map for ``df``, rebuilding if stale."""
    stored = None
    try:
        stored = df.attrs.get(_PATIENT_CLUSTER_ATTR)
    except Exception:
        stored = None
    nrows = 0 if df is None else len(df)
    if (
        isinstance(stored, dict)
        and stored.get('nrows') == nrows
        and 'clusters' in stored
    ):
        return stored['clusters']
    clusters = _build_patient_cluster_map(df, safe_field_fn)
    if df is not None:
        try:
            df.attrs[_PATIENT_CLUSTER_ATTR] = {
                'nrows': nrows,
                'clusters': clusters,
            }
        except Exception:
            pass
    return clusters


def resolve_patient_cluster_search_dates(df, row_data, safe_field_fn=None):
    """Return (start, end) min–max VisitDate for this row's 90-day patient cluster.

    Uses every spreadsheet row with the same patient identity, including
    already-completed rows. Returns ('', '') when the row has no usable
    VisitDate or no cluster can be built.
    """
    if safe_field_fn is None:
        safe_field_fn = safe_field
    if df is None or row_data is None:
        return '', ''
    visit_dt = _parse_visit_datetime(safe_field_fn(row_data, 'VisitDate'))
    if visit_dt is None:
        return '', ''
    identity = patient_identity_key(row_data, safe_field_fn)
    if not identity[0] or not identity[1]:
        return '', ''
    clusters = _patient_cluster_cache(df, safe_field_fn).get(identity) or []
    return _cluster_range_containing(visit_dt, clusters)


def search_group_key(df, row_index, safe_field_fn=safe_field, payer_config=None):
    """Normalized tuple identifying the claim-search form for a row."""
    row = df.iloc[row_index]
    member_id, last_name, first_name, dob = patient_identity_key(row, safe_field_fn)
    start_date, end_date, _ = resolve_invoice_search_dates(
        df, row, safe_field_fn, payer_config=payer_config,
    )
    return (member_id, last_name, first_name, dob, start_date, end_date)


def row_eligible_for_grouping(
    df, row_index, safe_field_fn=safe_field, payer_config=None,
):
    """Rows need invoice + valid DOB to participate in a shared search."""
    row = df.iloc[row_index]
    if not safe_invoice_number(row, 'InvoiceNumber'):
        return False
    if not row_has_valid_dob(row, safe_field_fn):
        return False
    key = search_group_key(df, row_index, safe_field_fn, payer_config=payer_config)
    if not key[0] or not key[1]:
        return False
    if not key[4] and not key[5]:
        return False
    return True


def _build_restacked_search_groups(work_queue, df, payer_config):
    """Bucket all work-queue rows that share a patient+cluster search key."""
    buckets = {}
    for idx in work_queue:
        if not row_eligible_for_grouping(df, idx, payer_config=payer_config):
            buckets[('singleton', idx)] = [idx]
            continue
        key = search_group_key(df, idx, payer_config=payer_config)
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(idx)
    return list(buckets.values())


def build_consecutive_search_groups(work_queue, df, payer_config):
    """Split ``work_queue`` into index groups that share one claim search.

    Default: consecutive rows with the same invoice-aggregated search key.
    When ``group_search_by_patient`` is on, rows for the same patient and
    90-day VisitDate cluster are bucketed together even if they are not
    adjacent. Ineligible rows are always placed in single-item groups.
    """
    if not work_queue:
        return []
    if not supports_search_grouping(payer_config):
        return [[idx] for idx in work_queue]
    if supports_patient_search_grouping(payer_config):
        return _build_restacked_search_groups(work_queue, df, payer_config)

    groups = []
    current = []
    current_key = None

    for idx in work_queue:
        if not row_eligible_for_grouping(df, idx, payer_config=payer_config):
            if current:
                groups.append(current)
                current = []
                current_key = None
            groups.append([idx])
            continue

        key = search_group_key(df, idx, payer_config=payer_config)
        if current and key == current_key:
            current.append(idx)
        else:
            if current:
                groups.append(current)
            current = [idx]
            current_key = key

    if current:
        groups.append(current)
    return groups
