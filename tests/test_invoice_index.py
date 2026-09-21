"""Invoice index lookups match a full-sheet scan and stay fast on noisy files."""
import pandas as pd

from availity_app.claim_snapshots import pending_invoice_sibling_indices
from availity_app.invoice_index import ATTR_KEY, attach_invoice_index, get_invoice_index
from availity_app.utils import (
    aggregate_invoice_visit_date_range,
    format_invoice_number,
    invoice_row_indices,
    normalize_date_range,
    normalize_search_date,
    resolve_invoice_search_dates,
)


def _reference_invoice_indices(df, invoice_number):
    target = format_invoice_number(invoice_number, '')
    if not target:
        return []
    indices = []
    for i in range(len(df)):
        if format_invoice_number(df.iloc[i].get('InvoiceNumber', ''), '') == target:
            indices.append(i)
    return indices


def _reference_visit_range(df, invoice_number):
    target = format_invoice_number(invoice_number, '')
    if not target:
        return None
    parsed = []
    for i in range(len(df)):
        row = df.iloc[i]
        if format_invoice_number(row.get('InvoiceNumber', ''), '') != target:
            continue
        normalized = normalize_search_date(row.get('VisitDate', ''))
        if not normalized:
            continue
        dt = pd.to_datetime(normalized, format='%m/%d/%Y')
        if pd.notna(dt):
            parsed.append(dt)
    if not parsed:
        return None
    start = min(parsed).strftime('%m/%d/%Y')
    end = max(parsed).strftime('%m/%d/%Y')
    return normalize_date_range(f"{start}-{end}")


def _base_row(**overrides):
    row = {
        'AltPatientID': 'M1',
        'PatientName': 'Doe, Jane',
        'Last_Name': 'Doe',
        'First_Name': 'Jane',
        'DOB': '01/01/1990',
        'StartDate': '01/01/2025',
        'EndDate': '01/31/2025',
        'InvoiceNumber': 'NOISE',
        'VisitDate': '03/01/2025',
        'AutomationStatus': 'Pending',
        'Recheck': '',
    }
    row.update(overrides)
    return row


def test_index_matches_scan_on_noisy_two_thousand_row_file():
    rows = [_base_row(InvoiceNumber=f'N{i}', VisitDate='02/01/2025') for i in range(2000)]
    rows[17] = _base_row(InvoiceNumber='TARGET', VisitDate='01/20/2025')
    rows[800] = _base_row(InvoiceNumber='TARGET', VisitDate='01/05/2025')
    rows[1500] = _base_row(InvoiceNumber='TARGET', VisitDate='01/11/2025')
    df = pd.DataFrame(rows)

    assert invoice_row_indices(df, 'TARGET') == _reference_invoice_indices(df, 'TARGET')
    assert invoice_row_indices(df, 'TARGET') == [17, 800, 1500]
    assert aggregate_invoice_visit_date_range(df, 'TARGET') == _reference_visit_range(
        df, 'TARGET',
    )
    assert aggregate_invoice_visit_date_range(df, 'TARGET') == '01/05/2025-01/20/2025'

    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[17])
    assert (start, end, from_agg) == ('01/05/2025', '01/20/2025', True)


def test_blank_and_placeholder_visit_dates_fall_back_to_row_start_end():
    df = pd.DataFrame([
        _base_row(
            InvoiceNumber='INV1', VisitDate='',
            StartDate='02/01/2025', EndDate='02/28/2025',
        ),
        _base_row(
            InvoiceNumber='INV1', VisitDate='--',
            StartDate='02/01/2025', EndDate='02/28/2025',
        ),
    ])
    assert aggregate_invoice_visit_date_range(df, 'INV1') is None
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('02/01/2025', '02/28/2025', False)


def test_empty_invoice_is_not_indexed():
    df = pd.DataFrame([
        _base_row(InvoiceNumber='', VisitDate='01/05/2025'),
        _base_row(InvoiceNumber='INV2', VisitDate='02/01/2025'),
    ])
    assert invoice_row_indices(df, '') == []
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert from_agg is False
    assert start == '01/01/2025'
    assert end == '01/31/2025'


def test_lazy_rebuild_when_index_missing_or_stale():
    df = pd.DataFrame([_base_row(InvoiceNumber='INV1', VisitDate='01/05/2025')])
    assert ATTR_KEY not in df.attrs
    assert invoice_row_indices(df, 'INV1') == [0]
    assert isinstance(df.attrs.get(ATTR_KEY), type(get_invoice_index(df)))

    df.attrs[ATTR_KEY].nrows = 0
    extra = pd.concat(
        [df, pd.DataFrame([_base_row(InvoiceNumber='INV1', VisitDate='01/06/2025')])],
        ignore_index=True,
    )
    assert invoice_row_indices(extra, 'INV1') == [0, 1]


def test_pending_siblings_only_scan_matching_invoice():
    rows = [_base_row(InvoiceNumber=f'N{i}') for i in range(200)]
    rows[3] = _base_row(InvoiceNumber='INV1', VisitDate='01/05/2025')
    rows[9] = _base_row(InvoiceNumber='INV1', VisitDate='01/06/2025')
    rows[12] = _base_row(
        InvoiceNumber='INV1', VisitDate='01/07/2025', AutomationStatus='Done',
    )
    df = pd.DataFrame(rows)
    attach_invoice_index(df)

    def _pending(frame, idx):
        return str(frame.at[idx, 'AutomationStatus']).strip().lower() != 'done'

    siblings = pending_invoice_sibling_indices(df, 3, _pending)
    assert siblings == [3, 9]
