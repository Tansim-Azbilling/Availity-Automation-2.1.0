"""Fill-if-blank Denial Reason from Claim Status for every payer."""
from unittest.mock import MagicMock, patch

import pytest

from availity_app.automation import (
    _canned_paid_denial_reason,
    _fill_blank_status_denial,
    enrich_claim_with_denial,
)
from availity_app.claim_snapshots import make_claim_snapshot, materialize_claim_for_visit
from availity_app.constants import (
    ADJUSTED_STATUS,
    FULLY_PAID_DENIAL_REASON,
    FULLY_PAID_OVERPAYMENT_DENIAL_REASON,
    IN_PROCESS_DENIAL_REASON,
    PAID_OVERPAYMENT_STATUS,
    PAYER_CONFIG,
)


def test_canned_mapping_for_all_payers():
    for payer in PAYER_CONFIG.values():
        assert _canned_paid_denial_reason('PAID', payer) == FULLY_PAID_DENIAL_REASON
        assert _canned_paid_denial_reason(
            PAID_OVERPAYMENT_STATUS, payer,
        ) == FULLY_PAID_OVERPAYMENT_DENIAL_REASON
        assert _canned_paid_denial_reason('PENDING', payer) == IN_PROCESS_DENIAL_REASON
        assert _canned_paid_denial_reason('FINALIZED', payer) == IN_PROCESS_DENIAL_REASON
        assert _canned_paid_denial_reason(ADJUSTED_STATUS, payer) is None
        assert _canned_paid_denial_reason('Denied', payer) is None


def test_finalized_fully_paid_maps_to_fully_paid():
    assert _canned_paid_denial_reason(
        'FINALIZED', billed='$100.00', paid='$100.00',
    ) == FULLY_PAID_DENIAL_REASON


def test_pending_fully_paid_stays_in_process():
    assert _canned_paid_denial_reason(
        'PENDING', billed='$100.00', paid='$100.00',
    ) == IN_PROCESS_DENIAL_REASON


def test_overpayment_wording():
    assert FULLY_PAID_OVERPAYMENT_DENIAL_REASON == 'Fully Paid with Overpayment'


def test_fill_blank_does_not_overwrite_existing_reason():
    claim = {'Claim Status': 'PAID', 'Denial Reason': 'CO-16'}
    _fill_blank_status_denial(claim, '$100.00', '$100.00')
    assert claim['Denial Reason'] == 'CO-16'


def test_fill_blank_writes_when_dash_dash():
    claim = {'Claim Status': 'PENDING', 'Denial Reason': '--'}
    _fill_blank_status_denial(claim, '$100.00', '$0.00')
    assert claim['Denial Reason'] == IN_PROCESS_DENIAL_REASON


def _line(date, billed, paid):
    return {
        'date_range': f'{date}-{date}',
        'billed': billed,
        'paid': paid,
        'hipaa_codes': '',
        'denial_pairs': [],
    }


@pytest.mark.parametrize('payer_name', list(PAYER_CONFIG))
@patch('availity_app.automation.log_to_gui')
def test_materialize_paid_blank_is_fully_paid(_log, payer_name):
    payer = PAYER_CONFIG[payer_name]
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[_line('01/05/2025', '$40.00', '$40.00')],
    )
    claim = materialize_claim_for_visit(snapshot, '01/05/2025', payer)
    if claim is None:
        return
    assert claim['Denial Reason'] == FULLY_PAID_DENIAL_REASON


@pytest.mark.parametrize('payer_name', list(PAYER_CONFIG))
@patch('availity_app.automation.log_to_gui')
def test_materialize_pending_blank_is_in_process(_log, payer_name):
    payer = PAYER_CONFIG[payer_name]
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PENDING'},
        lines=[_line('01/05/2025', '$40.00', '$0.00')],
        remittance_denial='--',
    )
    claim = materialize_claim_for_visit(snapshot, '01/05/2025', payer)
    if claim is None:
        return
    assert claim['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
def test_materialize_finalized_partial_stays_extracted_or_blank(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'FINALIZED'},
        lines=[_line('01/05/2025', '$40.00', '$10.00')],
        remittance_denial='--',
    )
    claim = materialize_claim_for_visit(
        snapshot, '01/05/2025', PAYER_CONFIG['Fidelis'],
    )
    assert claim['Claim Status'] == 'Partially Paid'
    assert claim['Denial Reason'] == '--'


@patch('availity_app.automation.log_to_gui')
def test_villagecaremax_finalized_blank_amounts_is_in_process(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'FINALIZED'},
        remittance_denial='--',
    )
    claim = materialize_claim_for_visit(
        snapshot, '01/05/2025', PAYER_CONFIG['Villagecaremax'],
    )
    assert claim['Claim Status'] == 'FINALIZED'
    assert claim['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
def test_materialize_finalized_fully_paid_is_fully_paid(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'FINALIZED'},
        lines=[_line('01/05/2025', '$40.00', '$40.00')],
        remittance_denial='--',
    )
    claim = materialize_claim_for_visit(
        snapshot, '01/05/2025', PAYER_CONFIG['Fidelis'],
    )
    assert claim['Denial Reason'] == FULLY_PAID_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
def test_materialize_keeps_extracted_denial(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[{
            'date_range': '01/05/2025-01/05/2025',
            'billed': '$40.00',
            'paid': '$40.00',
            'hipaa_codes': '',
            'denial_pairs': [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')],
        }],
    )
    claim = materialize_claim_for_visit(
        snapshot, '01/05/2025', PAYER_CONFIG['Healthfirst'],
    )
    assert claim['Denial Reason'] == 'ADJUSTMENT TO ORIGINAL CLAIM'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_enrich_does_not_fill_during_remittance_harvest(mock_extract, mock_log):
    mock_extract.return_value = '--'
    claim_data = {
        'Claim ID': 'C123',
        'Claim Status': 'FINALIZED',
        'Check Date': '07/16/2026',
        'Denial Reason': '--',
    }
    enrich_claim_with_denial(
        None, None, claim_data, '$100.00', '$0.00',
        None, -1, PAYER_CONFIG['Villagecaremax'],
        allow_line_fallback=False,
    )
    assert claim_data['Denial Reason'] == '--'
