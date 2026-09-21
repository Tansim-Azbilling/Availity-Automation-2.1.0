"""Pool-based tiered matching (SPLIT_VISIT, MERGE, paid-disambiguation, etc.)."""
from unittest.mock import patch

import pandas as pd

from availity_app.utils import (
    MATCH_FLAG_MANUAL_REVIEW,
    get_sorted_invoice_rows,
    match_invoice_lines_to_inputs,
    money_eq,
    to_cents,
)


def _line(date_range, billed='$100.00', paid='$50.00', quantity='1',
          status='Paid', idx=0):
    return {
        'row': object(),
        'idx': idx,
        'billed': billed,
        'paid': paid,
        'date_range': date_range,
        'quantity': quantity,
        'status': status,
    }


def _conf(entry):
    return str(entry.get('confidence') or '')


def _note(entry):
    return str(entry.get('note') or '')


@patch('availity_app.utils.log_to_gui')
def test_split_visit_partial_paid_denied(mock_log):
    """Observed failure: 44u/$300.52 partial â†’ lines qty7/$47.81 + qty37/$252.71."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/15/2026'],
        'Amount': [300.52],
        'Units': [44],
        'PaidAmount': [47.81],
        'Adj': [0],
        'Balance': [252.71],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/10/2026-06/10/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=0),
        _line('06/11/2026-06/11/2026', billed='$80.00', paid='$80.00',
              quantity='8', status='Paid', idx=1),
        _line('06/12/2026-06/12/2026', billed='$50.00', paid='$50.00',
              quantity='5', status='Paid', idx=2),
        _line('06/15/2026-06/15/2026', billed='$47.81', paid='$47.81',
              quantity='7', status='Paid', idx=3),
        _line('06/16/2026-06/16/2026', billed='$90.00', paid='$90.00',
              quantity='9', status='Paid', idx=4),
        _line('06/15/2026-06/15/2026', billed='$252.71', paid='$0.00',
              quantity='37', status='Denied', idx=6),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        header_billed=620.52,
    )
    entry = result[0]
    assert entry['unresolved'] is False
    assert _conf(entry) == ''
    assert entry['split_bill'] is True
    assert entry.get('forced_status') == 'PARTIALLY PAID'
    assert entry.get('matched_line_idxs') == [3, 6]
    assert entry.get('denial_line_numbers') == [7]
    assert 'paid/balance verified' in _note(entry)


@patch('availity_app.utils.log_to_gui')
def test_tier15_paid_disambiguation(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [169.92, 169.92],
        'Units': [24, 24],
        'PaidAmount': [169.92, 0.0],
        'Adj': [0, 0],
        'Balance': [0.0, 169.92],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$169.92', paid='$169.92',
              quantity='24', status='Paid', idx=0),
        _line('06/23/2026-06/23/2026', billed='$169.92', paid='$0.00',
              quantity='24', status='Denied', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    )
    assert _conf(result[0]) == ''
    assert _conf(result[1]) == ''
    assert MATCH_FLAG_MANUAL_REVIEW not in _conf(result[0])
    assert 'paid-disambiguated' in _note(result[0])
    assert result[0]['line']['status'] == 'Paid'
    assert result[1]['line']['status'] == 'Denied'


@patch('availity_app.utils.log_to_gui')
def test_duplicate_identical_paid_falls_to_order_based(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [169.92, 169.92],
        'Units': [24, 24],
        'PaidAmount': [0.0, 0.0],
        'Adj': [0, 0],
        'Balance': [169.92, 169.92],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$169.92', paid='$0.00',
              quantity='24', status='Paid', idx=0),
        _line('06/23/2026-06/23/2026', billed='$169.92', paid='$0.00',
              quantity='24', status='Denied', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    )
    assert MATCH_FLAG_MANUAL_REVIEW in _conf(result[0])
    assert MATCH_FLAG_MANUAL_REVIEW in _conf(result[1])


@patch('availity_app.utils.log_to_gui')
def test_fully_paid_exact_declined_split_found(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/15/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [40.00],
        'Adj': [0],
        'Balance': [60.00],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/15/2026-06/15/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=0),
        _line('06/15/2026-06/15/2026', billed='$40.00', paid='$40.00',
              quantity='4', status='Paid', idx=1),
        _line('06/15/2026-06/15/2026', billed='$60.00', paid='$0.00',
              quantity='6', status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    entry = result[0]
    assert _conf(entry) == ''
    assert entry['split_bill'] is True
    assert entry.get('matched_line_idxs') == [1, 2]
    assert entry.get('forced_status') == 'PARTIALLY PAID'


@patch('availity_app.utils.log_to_gui')
def test_merge_two_rows_to_one_line(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [50.00, 50.00],
        'Units': [5, 5],
        'PaidAmount': [50.00, 50.00],
        'Adj': [0, 0],
        'Balance': [0.0, 0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/23/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=0),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    assert _conf(result[0]) == ''
    assert _conf(result[1]) == ''
    assert result[0]['line']['idx'] == 0
    assert 'part of merged line 0 covering 2 visits' in _note(result[0])


@patch('availity_app.utils.log_to_gui')
def test_paired_reversal_netted(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/22/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [100.00],
        'Adj': [0],
        'Balance': [0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/20/2026-06/20/2026', billed='$50.00', paid='$50.00',
              quantity='5', status='Paid', idx=0),
        _line('06/20/2026-06/20/2026', billed='-$50.00', paid='-$50.00',
              quantity='5', status='Adjusted', idx=1),
        _line('06/22/2026-06/22/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    entry = result[0]
    assert entry['unresolved'] is False
    assert entry['line']['idx'] == 2
    assert 'netted reversal pair' in _note(entry)


@patch('availity_app.utils.log_to_gui')
def test_unpaired_reversal_aborts(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100.00, 200.00],
        'Units': [10, 20],
        'PaidAmount': [100.00, 200.00],
        'Adj': [0, 0],
        'Balance': [0.0, 0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=0),
        _line('06/23/2026-06/23/2026', billed='-$75.00', paid='$0.00',
              quantity='5', status='Adjusted', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    assert all(v['unresolved'] for v in result.values())
    assert all(MATCH_FLAG_MANUAL_REVIEW in _conf(v) for v in result.values())
    assert all('unpaired reversal line' in _note(v) for v in result.values())


@patch('availity_app.utils.log_to_gui')
def test_data_mismatch_excludes_row(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/22/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [40.00],
        'Adj': [0],
        'Balance': [50.00],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', paid='$40.00',
              quantity='10', status='Paid', idx=0),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    )
    entry = result[0]
    assert entry['unresolved'] is True
    assert MATCH_FLAG_MANUAL_REVIEW in _conf(entry)
    assert entry.get('line') is None


@patch('availity_app.utils.log_to_gui')
def test_ambiguous_split_no_tiebreak(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/15/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [40.00],
        'Adj': [0],
        'Balance': [60.00],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/15/2026-06/15/2026', billed='$40.00', paid='$40.00',
              quantity='4', status='Paid', idx=0),
        _line('06/15/2026-06/15/2026', billed='$60.00', paid='$0.00',
              quantity='6', status='Denied', idx=1),
        _line('06/15/2026-06/15/2026', billed='$30.00', paid='$10.00',
              quantity='3', status='Paid', idx=2),
        _line('06/15/2026-06/15/2026', billed='$70.00', paid='$30.00',
              quantity='7', status='Denied', idx=3),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        allow_split_tiebreak=False,
    )
    entry = result[0]
    assert entry['unresolved'] is True
    assert 'valid split subsets â€” ambiguous' in _note(entry)


@patch('availity_app.utils.log_to_gui')
def test_ambiguous_split_with_tiebreak(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/15/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [40.00],
        'Adj': [0],
        'Balance': [60.00],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    # Subset A (0,1): paid 40+0=40, unpaid 60 â€” passes tiebreak
    # Subset B (2,3): paid 20+20=40, unpaid 20+40=60 â€” ALSO passes paid;
    # use paid that fails: 5+5=10 != 40
    lines = [
        _line('06/15/2026-06/15/2026', billed='$40.00', paid='$40.00',
              quantity='4', status='Paid', idx=0),
        _line('06/15/2026-06/15/2026', billed='$60.00', paid='$0.00',
              quantity='6', status='Denied', idx=1),
        _line('06/15/2026-06/15/2026', billed='$30.00', paid='$5.00',
              quantity='3', status='Paid', idx=2),
        _line('06/15/2026-06/15/2026', billed='$70.00', paid='$5.00',
              quantity='7', status='Denied', idx=3),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        allow_split_tiebreak=True,
    )
    entry = result[0]
    assert entry['unresolved'] is False
    assert _conf(entry) == ''
    assert entry['split_bill'] is True
    assert entry.get('matched_line_idxs') == [0, 1]


@patch('availity_app.utils.log_to_gui')
def test_pagination_miss_w1_warning(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/22/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [100.00],
        'Adj': [0],
        'Balance': [0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', paid='$100.00',
              quantity='10', status='Paid', idx=0),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=500.00,
    )
    entry = result[0]
    assert 'CLAIM WARN: line sum' in _note(entry)
    assert 'possible pagination miss' in _note(entry)
    assert entry['unresolved'] is False


@patch('availity_app.utils.log_to_gui')
def test_penny_tolerance_one_cent_matches(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/22/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [100.00],
        'Adj': [0],
        'Balance': [0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$40.00', paid='$40.00',
              quantity='4', status='Paid', idx=0),
        _line('06/22/2026-06/22/2026', billed='$60.01', paid='$60.00',
              quantity='6', status='Paid', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    assert _conf(result[0]) == ''
    assert result[0]['split_bill'] is True


@patch('availity_app.utils.log_to_gui')
def test_penny_tolerance_three_cents_do_not_match(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['06/22/2026'],
        'Amount': [100.00],
        'Units': [10],
        'PaidAmount': [100.00],
        'Adj': [0],
        'Balance': [0.0],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$40.00', paid='$40.00',
              quantity='4', status='Paid', idx=0),
        _line('06/22/2026-06/22/2026', billed='$60.03', paid='$60.00',
              quantity='6', status='Paid', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    )
    assert result[0].get('split_bill') is not True


def test_money_eq_helpers():
    assert money_eq(to_cents(1.00), to_cents(1.01), 1)
    assert not money_eq(to_cents(1.00), to_cents(1.03), 1)
    assert money_eq(to_cents(1.00), to_cents(1.02), 2)


@patch('availity_app.utils.log_to_gui')
def test_enable_pool_tiers_false_preserves_split_bill(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [500],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$300.00', paid='$300.00',
              quantity='20', idx=0),
        _line('04/29/2026-04/29/2026', billed='$200.00', paid='$200.00',
              quantity='16', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=500.0, header_paid=500.0,
        enable_pool_tiers=False,
    )
    assert result[0]['split_bill'] is True
    assert result[0]['confidence'] == ''
