"""Within-range day-position line selection (legacy helpers).

SWHNY now uses match_by_invoice_alignment; these tests keep an explicit
day-position config so the shared helpers remain covered.
"""
from unittest.mock import patch

import pandas as pd

from availity_app.constants import CLAIM_UNIDENTIFIED_STATUS, PAYER_CONFIG
from availity_app.utils import (
    day_offset_in_range,
    filter_line_rows_by_visit_date,
    resolve_invoice_line_context,
    resolve_line_level_config,
)


def _day_position_cfg():
    """Config that still exercises within-range day-position selection."""
    return {
        'filter_by_visit_date': True,
        'match_mode': 'exact',
        'fallback_within_range': True,
        'fallback_to_sole_line': True,
        'range_match_by_day_position': True,
    }


def _line(date_range, billed='$100.00', paid='$50.00', idx=0):
    return {
        'row': object(),
        'idx': idx,
        'billed': billed,
        'paid': paid,
        'date_range': date_range,
    }


def _image_lines():
    """Two identical multi-day lines as in the Availity screenshot."""
    rng = '06/13/2026-06/14/2026'
    return [
        _line(rng, billed='$254.52', paid='$255.44', idx=0),
        _line(rng, billed='$254.52', paid='$255.44', idx=1),
    ]


@patch('availity_app.utils.log_to_gui')
def test_exact_match_preferred_over_within_range(mock_log):
    lines = [
        _line('06/13/2026-06/13/2026', idx=0),
        _line('06/13/2026-06/14/2026', idx=1),
    ]
    matches, unresolved = filter_line_rows_by_visit_date(
        lines, '06/13/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=0,
    )
    assert unresolved is False
    assert len(matches) == 1
    assert matches[0]['idx'] == 0


@patch('availity_app.utils.log_to_gui')
def test_day_position_picks_first_row_for_range_start(mock_log):
    matches, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=0,
        visit_date_duplicated=False,
    )
    assert unresolved is False
    assert len(matches) == 1
    assert matches[0]['idx'] == 0


@patch('availity_app.utils.log_to_gui')
def test_day_position_picks_second_row_for_range_end(mock_log):
    matches, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/14/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=1,
        visit_date_duplicated=False,
    )
    assert unresolved is False
    assert len(matches) == 1
    assert matches[0]['idx'] == 1


@patch('availity_app.utils.log_to_gui')
def test_count_mismatch_unresolved(mock_log):
    matches, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=_day_position_cfg(),
        invoice_row_count=3, invoice_row_occurrence=0,
    )
    assert unresolved is True
    assert matches == []


@patch('availity_app.utils.log_to_gui')
def test_day_offset_out_of_bounds_uses_invoice_occurrence(mock_log):
    """3-day span with only 2 lines: fall back to invoice row order.

    A day offset past the last line means the invoice's service dates are
    non-consecutive, so the offset is meaningless and row order is the better
    signal. Only when row order is also out of bounds do we refuse to guess.
    """
    lines = [
        _line('06/13/2026-06/15/2026', idx=0),
        _line('06/13/2026-06/15/2026', idx=1),
    ]
    matches, unresolved = filter_line_rows_by_visit_date(
        lines, '06/15/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=0,
        visit_date_duplicated=False,
    )
    assert unresolved is False
    assert [m['idx'] for m in matches] == [0]


@patch('availity_app.utils.log_to_gui')
def test_day_offset_and_row_order_both_out_of_bounds_unresolved(mock_log):
    """No usable signal at all: refuse to guess a line."""
    lines = [
        _line('06/13/2026-06/15/2026', idx=0),
        _line('06/13/2026-06/15/2026', idx=1),
    ]
    matches, unresolved = filter_line_rows_by_visit_date(
        lines, '06/15/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=5,
        visit_date_duplicated=False,
    )
    assert unresolved is True
    assert matches == []


@patch('availity_app.utils.log_to_gui')
def test_duplicate_visit_date_uses_invoice_occurrence(mock_log):
    first, _ = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=0,
        visit_date_duplicated=True,
    )
    second, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=_day_position_cfg(),
        invoice_row_count=2, invoice_row_occurrence=1,
        visit_date_duplicated=True,
    )
    assert unresolved is False
    assert first[0]['idx'] == 0
    assert second[0]['idx'] == 1


@patch('availity_app.utils.log_to_gui')
def test_flag_off_returns_all_within_range(mock_log):
    cfg = {
        'filter_by_visit_date': True,
        'match_mode': 'exact',
        'fallback_within_range': True,
        'fallback_to_sole_line': True,
        'range_match_by_day_position': False,
    }
    matches, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=cfg,
    )
    assert unresolved is False
    assert len(matches) == 2


def test_swhny_config_disables_day_position():
    cfg = resolve_line_level_config(PAYER_CONFIG['SWHNY'])
    assert cfg.get('match_by_invoice_alignment') is True
    assert cfg.get('range_match_by_day_position') is False


def test_claim_unidentified_status_constant():
    assert 'search manually' in CLAIM_UNIDENTIFIED_STATUS.lower()


def test_day_offset_in_range():
    assert day_offset_in_range('06/13/2026', '06/13/2026-06/14/2026') == 0
    assert day_offset_in_range('06/14/2026', '06/13/2026-06/14/2026') == 1
    assert day_offset_in_range('06/12/2026', '06/13/2026-06/14/2026') is None


def test_resolve_invoice_line_context_unique_and_duplicate():
    df = pd.DataFrame({
        'InvoiceNumber': ['100', '100', '200'],
        'VisitDate': ['06/13/2026', '06/14/2026', '06/13/2026'],
    })
    ctx0 = resolve_invoice_line_context(df, 0)
    assert ctx0['invoice_row_count'] == 2
    assert ctx0['invoice_row_occurrence'] == 0
    assert ctx0['visit_date_duplicated'] is False

    ctx1 = resolve_invoice_line_context(df, 1)
    assert ctx1['invoice_row_occurrence'] == 1
    assert ctx1['visit_date_duplicated'] is False

    df_dup = pd.DataFrame({
        'InvoiceNumber': ['100', '100'],
        'VisitDate': ['06/13/2026', '06/13/2026'],
    })
    ctx_dup = resolve_invoice_line_context(df_dup, 1)
    assert ctx_dup['visit_date_duplicated'] is True
    assert ctx_dup['invoice_row_occurrence'] == 1


@patch('availity_app.utils.log_to_gui')
def test_missing_invoice_context_unresolved(mock_log):
    matches, unresolved = filter_line_rows_by_visit_date(
        _image_lines(), '06/13/2026', cfg=_day_position_cfg(),
    )
    assert unresolved is True
    assert matches == []
