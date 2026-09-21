"""SWHNY Denial Reason comes from the remittance viewer, not line remarks."""
from unittest.mock import MagicMock, patch

from availity_app.automation import (
    _harvest_remittance_amounts,
    _payment_skips_remittance,
    _should_harvest_line_denials,
    _should_open_remittance,
    harvest_open_claim_snapshot,
)
from availity_app.claim_snapshots import make_claim_snapshot, materialize_claim_for_visit
from availity_app.constants import (
    FULLY_PAID_DENIAL_REASON,
    FULLY_PAID_OVERPAYMENT_DENIAL_REASON,
    IN_PROCESS_DENIAL_REASON,
    NEED_MANUAL_REVIEW_STATUS,
    PAYER_CONFIG,
)
from availity_app.utils import join_remit_lines_to_claim_lines

_SWHNY = PAYER_CONFIG['SWHNY']


def _align(snapshot, row_index=0):
    snapshot['_align_map'] = {
        row_index: {
            'line': snapshot['lines'][0],
            'unresolved': False,
            'confidence': 'HIGH',
        },
    }
    return snapshot


def test_swhny_is_remittance_first():
    assert _SWHNY.get('uses_remittance') is True
    assert _SWHNY.get('remittance_when_remarks_blank', False) is False
    assert _SWHNY.get('uses_line_level') is True
    assert _SWHNY.get('read_hipaa_codes', False) is False
    assert _should_harvest_line_denials(_SWHNY) is False
    assert _should_open_remittance(_SWHNY, 'C1', 'line remarks') is True


def test_swhny_skips_paid_overpayment_and_pending():
    assert _payment_skips_remittance('$100.00', '$100.00') == 'fully_paid'
    assert _payment_skips_remittance('$100.00', '$120.00') == 'overpayment'
    assert _payment_skips_remittance('$100.00', '$0.00') is None
    denied = {'Claim ID': 'C1', 'Claim Status': 'DENIED'}
    pending = {'Claim ID': 'C1', 'Claim Status': 'PENDING'}
    paid = {'Claim ID': 'C1', 'Claim Status': 'PAID'}
    assert _should_open_remittance(_SWHNY, 'C1', claim_data=denied) is True
    assert _should_open_remittance(_SWHNY, 'C1', claim_data=pending) is False
    assert _should_open_remittance(_SWHNY, 'C1', claim_data=paid) is True


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_swhny_harvest_skips_remark_codes_and_uses_remittance(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    mock_header.return_value = {
        'Claim ID': '262031020904',
        'Claim Status': 'DENIED',
        'Billed Amount': '$169.92',
        'Paid Amount': '$0.00',
        'Check Number': '--',
        'Check Date': '--',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '06/22/2026-06/22/2026',
        'billed': '$169.92',
        'paid': '$0.00',
        'quantity': '1',
        'hipaa_codes': '',
    }]
    mock_harvest.return_value = {
        'denial': (
            'The number of Days or Units of Service exceeds our acceptable maximum.'
        ),
        'lines': [],
        'not_found': False,
        'error': None,
    }

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), _SWHNY)

    mock_denial_pairs.assert_not_called()
    mock_harvest.assert_called_once()
    assert snapshot['remittance_denial'] == (
        'The number of Days or Units of Service exceeds our acceptable maximum.'
    )
    assert snapshot['lines'][0]['denial_pairs'] == []
    assert snapshot['lines'][0]['billed'] == '$169.92'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_swhny_harvest_skips_remittance_when_pending(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, mock_log,
):
    mock_header.return_value = {
        'Claim ID': '262331020268',
        'Claim Status': 'PENDING',
        'Billed Amount': '$100.00',
        'Paid Amount': '--',
        'Check Number': '--',
        'Check Date': '--',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '06/27/2026-06/27/2026',
        'billed': '$100.00',
        'paid': '$0.00',
        'hipaa_codes': '',
    }]

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), _SWHNY)

    mock_denial_pairs.assert_not_called()
    mock_harvest.assert_not_called()
    assert snapshot['remittance_denial'] is None
    assert snapshot['lines'][0]['denial_pairs'] == []
    log_text = ''.join(str(call.args[0]) for call in mock_log.call_args_list)
    assert 'Searching remittance' not in log_text
    assert 'PENDING' in log_text


@patch('availity_app.automation.log_to_gui')
def test_swhny_materialize_uses_remittance_not_remark_pairs(_log):
    snapshot = _align(make_claim_snapshot(
        header={
            'Claim ID': '262031020904',
            'Billed Amount': '$169.92',
            'Paid Amount': '$0.00',
            'Claim Status': 'DENIED',
        },
        lines=[{
            'date_range': '06/22/2026-06/22/2026',
            'billed': '$169.92',
            'paid': '$0.00',
            'hipaa_codes': '',
            'denial_pairs': [('G18', 'Remark code should not be used')],
        }],
        remittance_denial='The number of Days or Units of Service exceeds our acceptable maximum.',
    ))
    claim = materialize_claim_for_visit(
        snapshot, '06/22/2026', _SWHNY, row_index=0,
    )
    assert claim['Denial Reason'] == (
        'The number of Days or Units of Service exceeds our acceptable maximum.'
    )
    assert 'G18' not in claim['Denial Reason']
    assert claim['Claim Status'] == 'Denied'


@patch('availity_app.automation.log_to_gui')
def test_swhny_materialize_pending_stays_in_process(_log):
    snapshot = _align(make_claim_snapshot(
        header={
            'Claim ID': '262331020268',
            'Billed Amount': '$100.00',
            'Paid Amount': '--',
            'Claim Status': 'PENDING',
        },
        lines=[{
            'date_range': '06/27/2026-06/27/2026',
            'billed': '$100.00',
            'paid': '$0.00',
            'hipaa_codes': '',
            'denial_pairs': [],
        }],
    ))
    claim = materialize_claim_for_visit(
        snapshot, '06/27/2026', _SWHNY, row_index=0,
    )
    assert claim['Claim Status'] == 'PENDING'
    assert claim['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
def test_swhny_materialize_paid_and_overpayment_use_canned_text(_log):
    paid = _align(make_claim_snapshot(
        header={
            'Claim ID': 'P1',
            'Billed Amount': '$100.00',
            'Paid Amount': '$100.00',
            'Claim Status': 'PAID',
        },
        lines=[{
            'date_range': '07/06/2026-07/06/2026',
            'billed': '$100.00',
            'paid': '$100.00',
            'hipaa_codes': '',
            'denial_pairs': [],
        }],
        remittance_denial='--',
    ))
    over = _align(make_claim_snapshot(
        header={
            'Claim ID': 'O1',
            'Billed Amount': '$100.00',
            'Paid Amount': '$120.00',
            'Claim Status': 'PAID',
        },
        lines=[{
            'date_range': '07/06/2026-07/06/2026',
            'billed': '$100.00',
            'paid': '$120.00',
            'hipaa_codes': '',
            'denial_pairs': [],
        }],
        remittance_denial='--',
    ))
    paid_claim = materialize_claim_for_visit(paid, '07/06/2026', _SWHNY, row_index=0)
    over_claim = materialize_claim_for_visit(over, '07/06/2026', _SWHNY, row_index=0)
    assert paid_claim['Denial Reason'] == FULLY_PAID_DENIAL_REASON
    assert over_claim['Denial Reason'] == FULLY_PAID_OVERPAYMENT_DENIAL_REASON
    assert over_claim['Claim Status'] == 'Paid (Overpayment)'


def _align_at(snapshot, row_index, line_idx):
    snapshot['_align_map'] = {
        row_index: {
            'line': snapshot['lines'][line_idx],
            'unresolved': False,
            'confidence': 'HIGH',
        },
    }
    return snapshot


@patch('availity_app.automation.log_to_gui')
def test_swhny_materialize_paid_line_overrides_claim_remittance(_log):
    carc = 'The number of Days or Units of Service exceeds our acceptable maximum.'
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': 'MIX1',
            'Billed Amount': '$269.92',
            'Paid Amount': '$100.00',
            'Claim Status': 'PAID',
        },
        lines=[
            {
                'date_range': '07/06/2026-07/06/2026',
                'billed': '$169.92',
                'paid': '$0.00',
                'quantity': '1',
                'hipaa_codes': '',
                'denial_pairs': [],
                'remittance_denial': carc,
            },
            {
                'date_range': '07/06/2026-07/06/2026',
                'billed': '$100.00',
                'paid': '$100.00',
                'quantity': '1',
                'hipaa_codes': '',
                'denial_pairs': [],
                'remittance_denial': carc,
            },
        ],
        remittance_denial=carc,
    )
    denied = materialize_claim_for_visit(
        _align_at(snapshot, 0, 0), '07/06/2026', _SWHNY, row_index=0,
    )
    paid = materialize_claim_for_visit(
        _align_at(snapshot, 1, 1), '07/06/2026', _SWHNY, row_index=1,
    )
    assert denied['Denial Reason'] == carc
    assert denied['Claim Status'] == 'Denied'
    assert paid['Denial Reason'] == FULLY_PAID_DENIAL_REASON
    assert paid['Claim Status'].upper() == 'PAID'


@patch('availity_app.automation.log_to_gui')
def test_swhny_materialize_uses_claim_level_remittance_when_line_has_no_carc(_log):
    carc = 'Precertification/authorization absent.'
    snapshot = _align(make_claim_snapshot(
        header={
            'Claim ID': 'C1',
            'Billed Amount': '$169.92',
            'Paid Amount': '$0.00',
            'Claim Status': 'DENIED',
        },
        lines=[{
            'date_range': '06/22/2026-06/22/2026',
            'billed': '$169.92',
            'paid': '$0.00',
            'quantity': '1',
            'hipaa_codes': '',
            'denial_pairs': [],
        }],
        remittance_denial=carc,
    ))
    claim = materialize_claim_for_visit(
        snapshot, '06/22/2026', _SWHNY, row_index=0,
    )
    assert claim['Denial Reason'] == carc


def test_join_attaches_unique_billed_qty():
    claim_lines = [
        {'billed': '$169.92', 'paid': '$0.00', 'quantity': '1'},
        {'billed': '$100.00', 'paid': '$100.00', 'quantity': '1'},
    ]
    remit_lines = [
        {
            'billed': '$100.00', 'paid': '$100.00', 'quantity': '1',
            'denial': 'Fully paid CARC',
        },
        {
            'billed': '$169.92', 'paid': '$0.00', 'quantity': '1',
            'denial': 'Days or Units exceed maximum.',
        },
    ]
    lines, claim_level, unresolved = join_remit_lines_to_claim_lines(
        claim_lines, remit_lines,
    )
    assert unresolved is False
    assert lines[0]['remittance_denial'] == 'Days or Units exceed maximum.'
    assert lines[1]['remittance_denial'] == 'Fully paid CARC'
    assert claim_level is None


def test_join_duplicate_signatures_are_unresolved():
    claim_lines = [
        {'billed': '$100.00', 'paid': '$0.00', 'quantity': '1'},
        {'billed': '$100.00', 'paid': '$0.00', 'quantity': '1'},
    ]
    remit_lines = [
        {'billed': '$100.00', 'paid': '$0.00', 'quantity': '1', 'denial': 'CARC A'},
        {'billed': '$100.00', 'paid': '$0.00', 'quantity': '1', 'denial': 'CARC B'},
    ]
    _lines, _claim_level, unresolved = join_remit_lines_to_claim_lines(
        claim_lines, remit_lines,
    )
    assert unresolved is True


def test_reversal_billed_is_skipped_for_remittance_open_decision():
    header = {'Billed Amount': '$0.00', 'Paid Amount': '$0.00'}
    lines = [
        {'billed': '$-226.24', 'paid': '$0.00', 'quantity': '1'},
        {'billed': '$169.92', 'paid': '$0.00', 'quantity': '1'},
    ]
    billed, paid = _harvest_remittance_amounts(header, lines)
    assert billed == '$169.92'
    assert paid == '$0.00'
    assert _payment_skips_remittance(billed, paid) is None


@patch('availity_app.automation.log_to_gui')
def test_swhny_mixed_combo_paid_and_denied_is_need_manual_review(_log):
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': 'SPLIT1',
            'Billed Amount': '$269.92',
            'Paid Amount': '$100.00',
            'Claim Status': 'PAID',
        },
        lines=[
            {
                'date_range': '07/06/2026-07/06/2026',
                'billed': '$169.92',
                'paid': '$0.00',
                'denial_pairs': [],
                'remittance_denial': 'Days or Units exceed maximum.',
            },
            {
                'date_range': '07/06/2026-07/06/2026',
                'billed': '$100.00',
                'paid': '$100.00',
                'denial_pairs': [],
                'remittance_denial': None,
            },
        ],
    )
    snapshot['_align_map'] = {
        0: {
            'line': snapshot['lines'][0],
            'consumed_lines': snapshot['lines'],
            'split_bill': True,
            'unresolved': False,
            'confidence': 'HIGH',
            'billed': '$269.92',
            'paid': '$100.00',
        },
    }
    claim = materialize_claim_for_visit(
        snapshot, '07/06/2026', _SWHNY, row_index=0,
    )
    assert claim['Claim Status'] == NEED_MANUAL_REVIEW_STATUS
    assert claim['Billed Amount'] == '--'
