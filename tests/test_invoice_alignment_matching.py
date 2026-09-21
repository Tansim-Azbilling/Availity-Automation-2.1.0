"""Invoice-alignment line matching (Service Dates + Amount/Units)."""
from unittest.mock import patch

import pandas as pd

from availity_app.constants import (
    CLAIM_UNIDENTIFIED_STATUS,
    PAID_OVERPAYMENT_STATUS,
    PAYER_CONFIG,
)
from availity_app.utils import (
    MATCH_CONFIDENCE_AMOUNT_UNITS,
    MATCH_CONFIDENCE_HIGH,
    MATCH_CONFIDENCE_LOW_ORDER,
    MATCH_CONFIDENCE_OPEN_DENIED,
    MATCH_CONFIDENCE_POSITIONAL,
    MATCH_CONFIDENCE_REVIEW,
    MATCH_CONFIDENCE_SPLIT_BILL,
    MATCH_FLAG_MANUAL_REVIEW,
    build_match_confidence,
    filter_excess_portal_lines,
    get_sorted_invoice_rows,
    invoice_service_dates_aligned,
    invoice_visit_dates_cover_panel_range,
    line_signature,
    match_by_signature_groups,
    match_invoice_lines_to_inputs,
    parse_service_dates_panel_text,
    rate_check,
    resolve_line_level_config,
    score_line_against_input,
)


def _swhny_cfg():
    return resolve_line_level_config(PAYER_CONFIG['SWHNY'])


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


def test_swhny_config_uses_invoice_alignment():
    cfg = _swhny_cfg()
    assert cfg['match_by_invoice_alignment'] is True
    assert cfg.get('range_match_by_day_position') is False
    assert cfg.get('fallback_within_range') is False


def test_parse_service_dates_panel_text():
    start, end = parse_service_dates_panel_text('06/22/2026 - 06/26/2026')
    assert start == '06/22/2026'
    assert end == '06/26/2026'
    start, end = parse_service_dates_panel_text('6/22/2026-6/26/2026')
    assert start == '06/22/2026'
    assert end == '06/26/2026'
    assert parse_service_dates_panel_text('--') == (None, None)


def test_get_sorted_invoice_rows_ascending():
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '200'],
        'VisitDate': ['06/24/2026', '06/22/2026', '06/26/2026', '01/01/2026'],
        'Amount': [10, 20, 30, 40],
        'Units': [1, 1, 1, 1],
    })
    rows = get_sorted_invoice_rows(df, '100')
    assert [idx for idx, _ in rows] == [1, 0, 2]
    assert [
        str(r['VisitDate']) for _, r in rows
    ] == ['06/22/2026', '06/24/2026', '06/26/2026']


def test_invoice_service_dates_aligned():
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/26/2026', '06/22/2026'],
        'Amount': [10, 20],
        'Units': [1, 1],
    })
    sorted_rows = get_sorted_invoice_rows(df, '100')
    assert invoice_service_dates_aligned(
        sorted_rows, '06/22/2026', '06/26/2026',
    ) is True
    assert invoice_service_dates_aligned(
        sorted_rows, '06/22/2026', '06/25/2026',
    ) is False


def test_score_prefers_amount_then_units():
    # Exact amount beats exact units alone.
    amount_hit = score_line_against_input(100.0, 2.0, '$100.00', '9')
    units_hit = score_line_against_input(50.0, 2.0, '$100.00', '2')
    assert amount_hit > units_hit


@patch('availity_app.utils.log_to_gui')
def test_equal_count_positional_when_dates_aligned(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100, 200],
        'Units': [1, 2],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=0),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    assert result[0]['line']['idx'] == 0
    assert result[1]['line']['idx'] == 1
    assert result[0]['unresolved'] is False
    assert result[1]['units_mismatch'] is False


@patch('availity_app.utils.log_to_gui')
def test_positional_logs_units_mismatch(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100, 200],
        'Units': [1, 9],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=0),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    assert result[1]['units_mismatch'] is True
    assert result[1]['line']['idx'] == 1


@patch('availity_app.utils.log_to_gui')
def test_filter_excess_lines_by_amount_then_units(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100, 200],
        'Units': [1, 2],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$999.00', quantity='9', idx=0),
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=1),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=2),
    ]
    kept = filter_excess_portal_lines(sorted_inputs, lines)
    assert len(kept) == 2
    assert {line['idx'] for line in kept} == {1, 2}

    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    
        enable_pool_tiers=False)
    assert result[0]['unresolved'] is False
    assert result[1]['unresolved'] is False
    assert {result[0]['line']['idx'], result[1]['line']['idx']} == {1, 2}


@patch('availity_app.utils.log_to_gui')
def test_more_inputs_than_lines_marks_unmatched(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026', '06/24/2026'],
        'Amount': [100, 200, 300],
        'Units': [1, 2, 3],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=0),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    
        enable_pool_tiers=False)
    matched = [k for k, v in result.items() if not v['unresolved']]
    unresolved = [k for k, v in result.items() if v['unresolved']]
    assert len(matched) == 2
    assert len(unresolved) == 1
    assert 'search manually' in CLAIM_UNIDENTIFIED_STATUS.lower()


@patch('availity_app.utils.log_to_gui')
def test_exact_single_line_when_lines_exceed_one_input(mock_log):
    """One portal line matches header billed + input Units â†’ keep that line."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [500],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$100.00', paid='$50.00',
              quantity='4', idx=0),
        _line('04/29/2026-04/29/2026', billed='$500.00', paid='$226.56',
              quantity='36', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=500.0, header_paid=226.56,
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['unresolved'] is False
    assert entry.get('split_bill') is False
    assert entry['line']['idx'] == 1
    assert entry['line']['billed'] == '$500.00'


@patch('availity_app.utils.log_to_gui')
def test_split_bill_paid_when_sum_paid_equals_header_billed(mock_log):
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
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['split_bill'] is True
    assert entry['unresolved'] is False
    assert entry['billed'] == '$500.00'
    assert entry['paid'] == '$500.00'
    assert entry['forced_status'] == 'PAID'
    assert entry['needs_claim_comment'] is False


@patch('availity_app.utils.log_to_gui')
def test_split_bill_partially_paid_when_sum_paid_less_than_header_billed(mock_log):
    """Row-7 style: sum paid equals header Paid but is less than header Billed."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [254.88],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$150.00', paid='$130.00',
              quantity='20', idx=0),
        _line('04/29/2026-04/29/2026', billed='$104.88', paid='$96.56',
              quantity='16', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=254.88, header_paid=226.56,
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['split_bill'] is True
    assert entry['billed'] == '$254.88'
    assert entry['paid'] == '$226.56'
    assert entry['forced_status'] == 'PARTIALLY PAID'
    assert entry['needs_claim_comment'] is True


@patch('availity_app.utils.log_to_gui')
def test_split_bill_denied_when_sum_paid_zero(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [500],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$300.00', paid='$0.00',
              quantity='20', idx=0),
        _line('04/29/2026-04/29/2026', billed='$200.00', paid='$0.00',
              quantity='16', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=500.0, header_paid=226.56,
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['split_bill'] is True
    assert entry['forced_status'] == 'DENIED'
    assert entry['needs_claim_comment'] is True


@patch('availity_app.utils.log_to_gui')
def test_split_bill_overpayment_when_sum_paid_exceeds_header_billed(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [500],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$300.00', paid='$350.00',
              quantity='20', idx=0),
        _line('04/29/2026-04/29/2026', billed='$200.00', paid='$200.00',
              quantity='16', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=500.0, header_paid=226.56,
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['split_bill'] is True
    assert entry['forced_status'] == PAID_OVERPAYMENT_STATUS
    assert entry['needs_claim_comment'] is False

@patch('availity_app.utils.log_to_gui')
def test_split_bill_falls_back_when_sums_do_not_match(mock_log):
    """No exact line and sums mismatch â†’ filter_excess keeps best Amount/Units line."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['04/29/2026'],
        'Amount': [400],
        'Units': [36],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('04/29/2026-04/29/2026', billed='$100.00', paid='$50.00',
              quantity='4', idx=0),
        _line('04/29/2026-04/29/2026', billed='$400.00', paid='$200.00',
              quantity='32', idx=1),
    ]
    # header billed 500 != sum 500? sum is 500, units 36 != sum qty 36? sum qty=36
    # Make sums fail: header billed 999, units still 36 but sum billed != 999
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
        header_billed=999.0, header_paid=226.56,
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry.get('split_bill') is not True
    assert entry['unresolved'] is False
    # Amount 400 matches line idx 1 billed
    assert entry['line']['idx'] == 1
    assert entry['units_mismatch'] is True


# ---------------------------------------------------------------------------
# Tier 1 â€” (Units, Amount) signature groups
# ---------------------------------------------------------------------------

def _five_row_invoice_df():
    """Real SWHNY shape: three rows at 24/$169.92, two at 20/$141.60."""
    return pd.DataFrame({
        'InvoiceNumber': ['100'] * 5,
        'VisitDate': ['06/22/2026', '06/23/2026', '06/24/2026',
                      '06/25/2026', '06/26/2026'],
        'Units': [24, 24, 24, 20, 20],
        'Amount': [169.92, 169.92, 169.92, 141.60, 141.60],
    })


def _five_portal_lines(statuses=None):
    specs = [
        ('06/22/2026-06/22/2026', '$169.92', '24'),
        ('06/23/2026-06/23/2026', '$169.92', '24'),
        ('06/24/2026-06/24/2026', '$169.92', '24'),
        ('06/25/2026-06/25/2026', '$141.60', '20'),
        ('06/26/2026-06/26/2026', '$141.60', '20'),
    ]
    statuses = statuses or ['Paid'] * 5
    return [
        _line(date_range, billed=billed, paid=billed, quantity=qty,
              status=status, idx=i)
        for i, ((date_range, billed, qty), status)
        in enumerate(zip(specs, statuses))
    ]


def test_line_signature_quantizes_and_rejects_unparseable():
    assert line_signature('24', '$169.92') == (2400, 16992)
    # Currency formatting and float noise collapse to one key.
    assert line_signature(24, '$1,169.92') == line_signature(24.0, 1169.9200001)
    assert line_signature('', '$100.00') is None
    assert line_signature('4', '--') is None


def test_rate_check_flags_only_real_disagreement():
    # 169.92/24 == 141.60/20 == $7.08 contract rate.
    assert rate_check(169.92, 24, '$141.60', '20') is False
    assert rate_check(169.92, 24, '$169.92', '12') is True
    # Missing or unusable inputs are not evidence of a fault.
    assert rate_check(None, 24, '$100.00', '4') is False
    assert rate_check(100.0, 0, '$100.00', '4') is False
    assert rate_check(100.0, 4, '--', '4') is False


def test_build_match_confidence_only_flags_critical_cases():
    assert build_match_confidence(MATCH_CONFIDENCE_HIGH) == ''
    assert build_match_confidence(
        MATCH_CONFIDENCE_POSITIONAL, units_mismatch=True,
    ) == ''
    assert build_match_confidence(
        MATCH_CONFIDENCE_POSITIONAL, rate_mismatch=True, units_mismatch=True,
    ) == MATCH_FLAG_MANUAL_REVIEW
    assert build_match_confidence(MATCH_CONFIDENCE_LOW_ORDER) == (
        MATCH_FLAG_MANUAL_REVIEW
    )
    assert build_match_confidence(MATCH_CONFIDENCE_REVIEW) == (
        MATCH_FLAG_MANUAL_REVIEW
    )


@patch('availity_app.utils.log_to_gui')
def test_duplicate_signature_groups_all_same_status_are_high(mock_log):
    """Three identical 24/$169.92 rows, all Paid â†’ risk-free order assignment."""
    sorted_inputs = get_sorted_invoice_rows(_five_row_invoice_df(), '100')
    result = match_invoice_lines_to_inputs(
        sorted_inputs, _five_portal_lines(), dates_aligned=True,
    
        enable_pool_tiers=False)
    assert [result[i]['line']['idx'] for i in range(5)] == [0, 1, 2, 3, 4]
    assert all(
        result[i]['confidence'] == '' for i in range(5)
    )
    assert all(result[i]['unresolved'] is False for i in range(5))
    assert all(result[i]['units_mismatch'] is False for i in range(5))


@patch('availity_app.utils.log_to_gui')
def test_duplicate_group_with_mixed_status_is_low_confidence(mock_log):
    """One DENIED among identical siblings â†’ that group is order-based only."""
    lines = _five_portal_lines(
        statuses=['Paid', 'Denied', 'Paid', 'Paid', 'Paid'],
    )
    sorted_inputs = get_sorted_invoice_rows(_five_row_invoice_df(), '100')
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    # The 24/$169.92 group cannot be proven â€” flag all three for audit.
    for i in (0, 1, 2):
        assert result[i]['confidence'] == MATCH_FLAG_MANUAL_REVIEW
        assert result[i]['unresolved'] is False
    # The 20/$141.60 group is uniform, so its order is still risk-free.
    for i in (3, 4):
        assert result[i]['confidence'] == ''
    assert result[1]['line']['status'] == 'Denied'


@patch('availity_app.utils.log_to_gui')
def test_signature_match_beats_blind_positional_order(mock_log):
    """Unique signatures in a shuffled portal table still map correctly."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026', '06/24/2026'],
        'Units': [24, 20, 12],
        'Amount': [169.92, 141.60, 84.96],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/24/2026-06/24/2026', billed='$84.96', quantity='12', idx=0),
        _line('06/22/2026-06/22/2026', billed='$169.92', quantity='24', idx=1),
        _line('06/23/2026-06/23/2026', billed='$141.60', quantity='20', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    # Positional mapping would have paired row 0 with idx 0 ($84.96).
    assert [result[i]['line']['idx'] for i in range(3)] == [1, 2, 0]
    assert all(
        result[i]['confidence'] == '' for i in range(3)
    )


@patch('availity_app.utils.log_to_gui')
def test_signature_groups_decline_on_extra_portal_line(mock_log):
    """No bijection â†’ Tier 1 declines and filter_excess behaviour is unchanged."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100, 200],
        'Units': [1, 2],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$999.00', quantity='9', idx=0),
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=1),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=2),
    ]
    assert match_by_signature_groups(sorted_inputs, lines) is None

    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    
        enable_pool_tiers=False)
    assert {result[0]['line']['idx'], result[1]['line']['idx']} == {1, 2}
    assert result[0]['confidence'] == ''


@patch('availity_app.utils.log_to_gui')
def test_signature_groups_decline_on_blank_units(mock_log):
    """An unparseable Units cell disables Tier 1 rather than guessing."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Amount': [100, 200],
        'Units': ['', 2],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=0),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=1),
    ]
    assert match_by_signature_groups(sorted_inputs, lines) is None

    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    assert [result[i]['line']['idx'] for i in range(2)] == [0, 1]
    assert result[0]['confidence'] == ''


@patch('availity_app.utils.log_to_gui')
def test_positional_match_carries_manual_review_on_rate_mismatch(mock_log):
    """Billed/Quantity ratio disagreeing with Amount/Units needs review."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026'],
        'Units': [24, 20],
        'Amount': [169.92, 141.60],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$169.92', quantity='24', idx=0),
        # Half the units for the same money â†’ $14.16/unit, not $7.08.
        _line('06/23/2026-06/23/2026', billed='$141.60', quantity='10', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    assert result[0]['confidence'] == ''
    assert result[1]['confidence'] == MATCH_FLAG_MANUAL_REVIEW
    assert result[1]['units_mismatch'] is True


@patch('availity_app.utils.log_to_gui')
def test_unresolved_rows_are_flagged_for_review(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100'],
        'VisitDate': ['06/22/2026', '06/23/2026', '06/24/2026'],
        'Amount': [100, 200, 300],
        'Units': [1, 2, 3],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/22/2026-06/22/2026', billed='$100.00', quantity='1', idx=0),
        _line('06/23/2026-06/23/2026', billed='$200.00', quantity='2', idx=1),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    
        enable_pool_tiers=False)
    unresolved = [v for v in result.values() if v['unresolved']]
    assert len(unresolved) == 1
    assert unresolved[0]['confidence'] == MATCH_FLAG_MANUAL_REVIEW

    # No portal lines at all â†’ every row needs review.
    empty = match_invoice_lines_to_inputs(
        sorted_inputs, [], dates_aligned=False,
    
        enable_pool_tiers=False)
    assert all(
        v['confidence'] == MATCH_FLAG_MANUAL_REVIEW for v in empty.values()
    )


@patch('availity_app.utils.log_to_gui')
def test_split_bill_entry_reports_split_bill_confidence(mock_log):
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
    
        enable_pool_tiers=False)
    assert result[0]['split_bill'] is True
    assert result[0]['confidence'] == ''


def _bundle_line(billed, paid='$50.00', quantity='1', status='Paid', idx=0):
    return _line(
        '06/22/2026-06/26/2026', billed=billed, paid=paid,
        quantity=quantity, status=status, idx=idx,
    )


@patch('availity_app.utils.log_to_gui')
def test_bundle_span_picks_line_by_visit_day_offset(mock_log):
    """When every portal line shows the claim span, map by VisitDate day offset."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100'],
        'VisitDate': ['6/24/2026'],
        'Amount': ['$169.92'],
        'Units': ['24'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _bundle_line('$169.92', quantity='24', idx=0),
        _bundle_line('$169.92', quantity='24', idx=1),
        _bundle_line('$169.92', quantity='24', idx=2),
        _bundle_line('$141.60', quantity='20', idx=3),
        _bundle_line('$141.60', quantity='20', idx=4),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=True,
    
        enable_pool_tiers=False)
    assert result[0]['line']['idx'] == 2
    assert result[0]['line']['date_range'] == '06/24/2026-06/24/2026'


@patch('availity_app.utils.log_to_gui')
def test_bundle_span_filters_excess_by_day_offset(mock_log):
    """Two inputs among five identical-range lines use day offset, not first match."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['5/30/2026', '5/31/2026'],
        'Amount': ['$254.88', '$247.80'],
        'Units': ['36', '35'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('05/27/2026-05/31/2026', billed='$254.88', quantity='36', idx=0),
        _line('05/27/2026-05/31/2026', billed='$254.88', quantity='36', idx=1),
        _line('05/27/2026-05/31/2026', billed='$240.72', quantity='34', idx=2),
        _line('05/27/2026-05/31/2026', billed='$254.88', quantity='36', idx=3),
        _line('05/27/2026-05/31/2026', billed='$247.80', quantity='35', idx=4),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
    
        enable_pool_tiers=False)
    assert result[0]['line']['idx'] == 3
    assert result[0]['line']['date_range'] == '05/30/2026-05/30/2026'
    assert result[1]['line']['idx'] == 4
    assert result[1]['line']['date_range'] == '05/31/2026-05/31/2026'


# ---------------------------------------------------------------------------
# Open Pay. Status â†’ Denied portal line fallback
# ---------------------------------------------------------------------------

def test_invoice_visit_dates_cover_panel_range():
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/04/2026'],
        'Amount': [1, 2, 3],
        'Units': [1, 1, 1],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    assert invoice_visit_dates_cover_panel_range(
        sorted_inputs, '06/01/2026', '06/04/2026',
    ) is False  # missing 06/03
    df2 = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/04/2026'],
        'Amount': [1, 2, 3, 4],
        'Units': [1, 1, 1, 1],
    })
    assert invoice_visit_dates_cover_panel_range(
        get_sorted_invoice_rows(df2, '100'), '06/01/2026', '06/04/2026',
    ) is True
    assert invoice_visit_dates_cover_panel_range(
        sorted_inputs, None, '06/04/2026',
    ) is False


@patch('availity_app.utils.log_to_gui')
def test_open_denied_direct_when_excess_lines_single_open_input(mock_log):
    """Production case: 1 Open input, 3 identical portal lines (2 Paid, 1 Denied).

    filter_excess would collapse to the first Paid line; direct match must pick
    Denied before filtering (invoice 4646589 pattern).
    """
    df = pd.DataFrame({
        'InvoiceNumber': ['4646589'],
        'VisitDate': ['6/3/2026'],
        'Amount': ['$169.68'],
        'Units': ['24'],
        'Pay. Status': ['Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '4646589')
    lines = [
        _line('05/31/2026-05/31/2026', billed='$169.68', quantity='24',
              status='Paid', idx=0),
        _line('05/31/2026-05/31/2026', billed='$169.68', quantity='24',
              status='Denied', idx=1),
        _line('05/31/2026-05/31/2026', billed='$169.68', quantity='24',
              status='Paid', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    entry = result[0]
    assert entry['unresolved'] is False
    assert entry['line']['idx'] == 1
    assert entry['line']['status'] == 'Denied'
    assert entry['open_denied'] is True
    assert entry['confidence'] == ''


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_matches_single_open_leftover(mock_log):
    """4 inputs missing a panel day + 3 lines (2 Paid, 1 Denied) â†’ Openâ†’Denied."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$999.00', quantity='9',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    # Open leftover is the second 06/03 row (df idx 3, Amount 400)
    # Amount/Units disagree with the Denied line â†’ Need Manual Review.
    open_entry = result[3]
    assert open_entry['unresolved'] is False
    assert open_entry['line']['idx'] == 2
    assert open_entry['line']['status'] == 'Denied'
    assert open_entry['confidence'] == MATCH_FLAG_MANUAL_REVIEW
    assert result[0]['line']['idx'] == 0
    assert result[1]['line']['idx'] == 1


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_multiple_denied(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Denied', idx=1),
        _line('06/03/2026-06/03/2026', billed='$999.00', quantity='9',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    open_entry = result[3]
    # Fallback must not fire; Open may still get a greedy Denied later.
    assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
        open_entry.get('confidence') or '',
    )


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_zero_denied(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$300.00', quantity='3',
              status='Paid', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    assert result[3]['unresolved'] is True
    assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
        result[3].get('confidence') or '',
    )


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_multiple_open_leftovers(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', 'Open', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$999.00', quantity='9',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    # Both leftovers are Open â†’ fallback must not fire.
    for entry in result.values():
        assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
            entry.get('confidence') or '',
        )


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_panel_fully_covered(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/04/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$999.00', quantity='9',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    for entry in result.values():
        assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
            entry.get('confidence') or '',
        )


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_pay_status_missing_or_not_open(mock_log):
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Paid'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$999.00', quantity='9',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
        result[3].get('confidence') or '',
    )

    df_no_col = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/03/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
    })
    result2 = match_invoice_lines_to_inputs(
        get_sorted_invoice_rows(df_no_col, '100'), lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    for entry in result2.values():
        assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
            entry.get('confidence') or '',
        )


@patch('availity_app.utils.log_to_gui')
def test_open_denied_fallback_skips_when_denied_consumed_after_gate_fail(mock_log):
    """When panel is fully covered, Denied is greedily taken; Open stays unresolved."""
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '100', '100'],
        'VisitDate': ['06/01/2026', '06/02/2026', '06/03/2026', '06/04/2026'],
        'Amount': [100, 200, 300, 400],
        'Units': [1, 2, 3, 4],
        'Pay. Status': ['', '', '', 'Open'],
    })
    sorted_inputs = get_sorted_invoice_rows(df, '100')
    lines = [
        _line('06/01/2026-06/01/2026', billed='$100.00', quantity='1',
              status='Paid', idx=0),
        _line('06/02/2026-06/02/2026', billed='$200.00', quantity='2',
              status='Paid', idx=1),
        _line('06/03/2026-06/03/2026', billed='$300.00', quantity='3',
              status='Denied', idx=2),
    ]
    result = match_invoice_lines_to_inputs(
        sorted_inputs, lines, dates_aligned=False,
        panel_range_start='06/01/2026', panel_range_end='06/04/2026',
    
        enable_pool_tiers=False)
    # Denied greedily matched to Amount=300; Open Amount=400 left unresolved.
    assert result[2]['unresolved'] is False
    assert result[2]['line']['status'] == 'Denied'
    assert result[3]['unresolved'] is True
    assert MATCH_CONFIDENCE_OPEN_DENIED not in str(
        result[3].get('confidence') or '',
    )
