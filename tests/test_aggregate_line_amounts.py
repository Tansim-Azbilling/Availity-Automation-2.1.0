"""Tests for summing billed/paid across multi-line VisitDate matches."""
from unittest.mock import patch

from availity_app.automation import (
    aggregate_line_match_amounts,
    derive_claim_status,
    find_line_by_visit_date,
)
from availity_app.constants import PAYER_CONFIG


def test_aggregate_multi_match_sum():
    """Two lines on same VisitDate: sum billed and paid."""
    matches = [
        {'billed': '$226.56', 'paid': '$226.56', 'date_range': '04/29/2026-04/29/2026'},
        {'billed': '$28.32', 'paid': '$0.00', 'date_range': '04/29/2026-04/29/2026'},
    ]
    billed, paid = aggregate_line_match_amounts(matches)
    assert billed == '$254.88'
    assert paid == '$226.56'


def test_aggregate_single_match_identity():
    """One line: sum equals that line's amounts."""
    matches = [
        {'billed': '$100.00', 'paid': '$40.00'},
    ]
    billed, paid = aggregate_line_match_amounts(matches)
    assert billed == '$100.00'
    assert paid == '$40.00'


def test_aggregate_blank_and_dash_treated_as_zero():
    """Blank / '--' amounts do not crash; treated as 0.0 in the sum."""
    matches = [
        {'billed': '$50.00', 'paid': '--'},
        {'billed': '', 'paid': '$10.00'},
        {'billed': '--', 'paid': None},
    ]
    billed, paid = aggregate_line_match_amounts(matches)
    assert billed == '$50.00'
    assert paid == '$10.00'


@patch('availity_app.automation.log_to_gui')
def test_aggregated_totals_derive_partially_paid(mock_log):
    """Aggregated $226.56 paid of $254.88 billed → Partially Paid."""
    matches = [
        {'billed': '$226.56', 'paid': '$226.56'},
        {'billed': '$28.32', 'paid': '$0.00'},
    ]
    billed, paid = aggregate_line_match_amounts(matches)
    claim_data = {'Claim Status': 'PAID'}
    payer_config = PAYER_CONFIG['Healthfirst']

    derive_claim_status(claim_data, billed, paid, payer_config)

    assert claim_data['Claim Status'] == 'Partially Paid'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.find_all_lines_by_visit_date')
def test_find_line_by_visit_date_sums_multi_match(mock_find_all, mock_log):
    """find_line_by_visit_date returns aggregated amounts for multiple matches."""
    mock_find_all.return_value = [
        {
            'row': 'row0',
            'idx': 0,
            'billed': '$226.56',
            'paid': '$226.56',
            'date_range': '04/29/2026-04/29/2026',
        },
        {
            'row': 'row1',
            'idx': 1,
            'billed': '$28.32',
            'paid': '$0.00',
            'date_range': '04/29/2026-04/29/2026',
        },
    ]

    row, idx, billed, paid, is_adjusted = find_line_by_visit_date(
        None, '04/29/2026',
    )

    assert row == 'row0'
    assert idx == 0
    assert billed == '$254.88'
    assert paid == '$226.56'
    assert is_adjusted is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.find_all_lines_by_visit_date')
def test_find_line_by_visit_date_single_match_unchanged(mock_find_all, mock_log):
    """Single match: billed/paid come from that line unchanged."""
    mock_find_all.return_value = [
        {
            'row': 'row0',
            'idx': 0,
            'billed': '$100.00',
            'paid': '$100.00',
            'date_range': '01/15/2026-01/15/2026',
        },
    ]

    row, idx, billed, paid, is_adjusted = find_line_by_visit_date(
        None, '01/15/2026',
    )

    assert row == 'row0'
    assert idx == 0
    assert billed == '$100.00'
    assert paid == '$100.00'
    assert is_adjusted is False
