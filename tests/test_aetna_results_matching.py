"""Aetna #claimsTable matching by VisitDate + Amount."""
from unittest.mock import MagicMock, patch

import pandas as pd

from availity_app.automation import (
    _find_claim_row_indices_by_visit_date_and_amount,
    _harvest_remittance_amounts,
    _payment_skips_remittance,
    _should_harvest_line_denials,
    _should_open_remittance,
    harvest_open_claim_snapshot,
    resolve_matching_claim_indices,
    write_invoice_snapshots_to_rows,
)
from availity_app.claim_snapshots import make_claim_snapshot, materialize_claim_for_visit
from availity_app.constants import OUTPUT_COLUMNS, PAYER_CONFIG, STATUS_COLUMNS
from availity_app.utils import (
    results_row_matches_visit_and_amount,
    validate_dataframe,
)

_AETNA = PAYER_CONFIG['Aetna Better Health']
_HEALTHFIRST = PAYER_CONFIG['Healthfirst']

_AETNA_TABLE_ROWS = (
    ('05/25/2025-05/25/2025', '$109.44'),
    ('05/25/2025-05/25/2025', '$82.08'),
    ('05/25/2025-05/25/2025', '$60.40'),
    ('05/25/2025-05/25/2025', '$30.20'),
)


def test_aetna_flag_and_healthfirst_unchanged():
    assert _AETNA.get('match_results_by_visit_and_amount') is True
    assert _AETNA.get('uses_remittance') is True
    assert _AETNA.get('remittance_when_remarks_blank', False) is False
    assert _AETNA.get('uses_line_level') is True
    assert _HEALTHFIRST.get('match_results_by_visit_and_amount', False) is False
    assert _HEALTHFIRST.get('uses_remittance') is True
    assert _HEALTHFIRST.get('remittance_when_remarks_blank') is True


def test_should_harvest_line_denials_follows_flags():
    assert _should_harvest_line_denials(_AETNA) is False
    assert _should_harvest_line_denials(PAYER_CONFIG['SWHNY']) is False
    assert _should_harvest_line_denials(_HEALTHFIRST) is True
    assert _should_harvest_line_denials(PAYER_CONFIG['Integra']) is True
    assert _should_harvest_line_denials(PAYER_CONFIG['Fidelis']) is True
    assert _should_harvest_line_denials(PAYER_CONFIG['Villagecaremax']) is False


def test_should_open_remittance_uses_claim_id_and_blank_mode():
    assert _should_open_remittance(PAYER_CONFIG['Villagecaremax'], 'C1') is True
    assert _should_open_remittance(PAYER_CONFIG['Villagecaremax'], '--') is False
    assert _should_open_remittance(_HEALTHFIRST, 'C1', '--') is True
    assert _should_open_remittance(_HEALTHFIRST, 'C1', 'Remark text') is False
    assert _should_open_remittance(PAYER_CONFIG['Fidelis'], 'C1') is False
    assert _should_open_remittance(_AETNA, 'C1', 'Remark text') is True
    assert _should_open_remittance(PAYER_CONFIG['SWHNY'], 'C1', 'Remark text') is True
    assert _should_open_remittance(
        PAYER_CONFIG['SWHNY'], 'C1',
        claim_data={'Claim ID': 'C1', 'Claim Status': 'PENDING'},
    ) is False
    unposted = {
        'Claim ID': 'C1',
        'Check Number': '--',
        'Check Date': '--',
        'Paid Amount': '--',
    }
    assert _should_open_remittance(
        PAYER_CONFIG['Villagecaremax'], 'C1', claim_data=unposted,
    ) is False
    unposted_paid = dict(unposted, **{'Paid Amount': '$0.00'})
    assert _should_open_remittance(
        PAYER_CONFIG['Villagecaremax'], 'C1', claim_data=unposted_paid,
    ) is True
    assert _should_open_remittance(_AETNA, 'C1', claim_data=unposted) is True


def test_payment_skips_remittance_keeps_partially_paid():
    assert _payment_skips_remittance('$106.40', '$66.50') is None
    assert _payment_skips_remittance('$106.40', '$0.00') is None
    assert _payment_skips_remittance('$326.88', '$326.88') == 'fully_paid'
    assert _payment_skips_remittance('$100.00', '$120.00') == 'overpayment'


def test_harvest_remittance_amounts_prefers_partial_line_over_header():
    billed, paid = _harvest_remittance_amounts(
        {'Billed Amount': '$326.88', 'Paid Amount': '$326.88'},
        [
            {'billed': '$218.00', 'paid': '$218.00'},
            {'billed': '$108.88', 'paid': '$66.50'},
        ],
    )
    assert billed == '$108.88'
    assert paid == '$66.50'


def test_validate_dataframe_requires_amount_only_for_aetna():
    cols = [
        'AltPatientID', 'PatientName', 'DOB',
        'StartDate', 'EndDate', 'InvoiceNumber', 'VisitDate',
    ]
    df = pd.DataFrame([{col: 'x' for col in cols}])
    assert validate_dataframe(df) == []
    assert validate_dataframe(df, _HEALTHFIRST) == []
    assert validate_dataframe(df, _AETNA) == ['Amount']
    df['Amount'] = ['109.44']
    assert validate_dataframe(df, _AETNA) == []


def test_sample_row_matches_visit_and_dollar_amount():
    assert results_row_matches_visit_and_amount(
        '05/25/2025-05/25/2025', '$109.44', '05/25/2025', '$109.44',
    )


def test_sample_row_matches_amount_without_dollar_sign():
    assert results_row_matches_visit_and_amount(
        '05/25/2025-05/25/2025', '$109.44', '05/25/2025', '109.44',
    )


def test_sample_rows_are_distinguished_by_billed_amount():
    visit = '05/25/2025'
    matched = [
        i for i, (date_range, billed) in enumerate(_AETNA_TABLE_ROWS)
        if results_row_matches_visit_and_amount(
            date_range, billed, visit, '$82.08',
        )
    ]
    assert matched == [1]
    for amount, expected in (
        ('$109.44', [0]),
        ('60.40', [2]),
        ('$30.20', [3]),
    ):
        matched = [
            i for i, (date_range, billed) in enumerate(_AETNA_TABLE_ROWS)
            if results_row_matches_visit_and_amount(
                date_range, billed, visit, amount,
            )
        ]
        assert matched == expected


def test_no_match_when_amount_or_visit_differs():
    assert not results_row_matches_visit_and_amount(
        '05/25/2025-05/25/2025', '$109.44', '05/25/2025', '$82.08',
    )
    assert not results_row_matches_visit_and_amount(
        '05/25/2025-05/25/2025', '$109.44', '05/26/2025', '$109.44',
    )
    assert not results_row_matches_visit_and_amount(
        '05/25/2025-05/25/2025', '$109.44', '05/25/2025', '',
    )


@patch('availity_app.automation.log_to_gui')
@patch(
    'availity_app.automation._results_billed_amount_from_row',
    side_effect=[billed for _date, billed in _AETNA_TABLE_ROWS],
)
@patch(
    'availity_app.automation._results_date_range_from_row',
    side_effect=[date_range for date_range, _billed in _AETNA_TABLE_ROWS],
)
@patch(
    'availity_app.automation._claims_table_identity_columns',
    return_value={'billed': 6},
)
@patch(
    'availity_app.automation._claims_table_service_dates_col_index',
    return_value=2,
)
@patch('availity_app.automation._claims_table_body_rows')
def test_finder_selects_only_matching_billed_row(
    mock_rows, _col, _ids, _dates, _billed, _log,
):
    rows = MagicMock()
    rows.count.return_value = 4
    rows.nth.side_effect = lambda i: MagicMock(name=f'row{i}')
    mock_rows.return_value = rows

    indices = _find_claim_row_indices_by_visit_date_and_amount(
        MagicMock(), '05/25/2025', '109.44',
    )
    assert indices == [0]


@patch('availity_app.automation.log_to_gui')
@patch(
    'availity_app.automation._results_billed_amount_from_row',
    side_effect=[billed for _date, billed in _AETNA_TABLE_ROWS],
)
@patch(
    'availity_app.automation._results_date_range_from_row',
    side_effect=[date_range for date_range, _billed in _AETNA_TABLE_ROWS],
)
@patch(
    'availity_app.automation._claims_table_identity_columns',
    return_value={'billed': 6},
)
@patch(
    'availity_app.automation._claims_table_service_dates_col_index',
    return_value=2,
)
@patch('availity_app.automation._claims_table_body_rows')
def test_finder_returns_empty_when_amount_missing_from_table(
    mock_rows, _col, _ids, _dates, _billed, _log,
):
    rows = MagicMock()
    rows.count.return_value = 4
    rows.nth.side_effect = lambda i: MagicMock(name=f'row{i}')
    mock_rows.return_value = rows

    indices = _find_claim_row_indices_by_visit_date_and_amount(
        MagicMock(), '05/25/2025', '$999.99',
    )
    assert indices == []


@patch('availity_app.automation.log_to_gui')
@patch(
    'availity_app.automation._find_claim_row_indices_by_visit_date_and_amount',
    return_value=[0],
)
@patch('availity_app.automation._find_claim_row_indices_by_invoice')
def test_aetna_resolve_skips_invoice_matching(
    mock_invoice, mock_visit_amount, _log,
):
    iframe = MagicMock()
    iframe.locator.return_value.count.return_value = 1
    indices, mode = resolve_matching_claim_indices(
        iframe, pd.DataFrame(), 'INV1',
        visit_date='05/25/2025',
        payer_config=_AETNA,
        amount='$109.44',
    )
    mock_invoice.assert_not_called()
    mock_visit_amount.assert_called_once()
    assert indices == [0]
    assert mode == 'visit_amount'


@patch('availity_app.automation.log_to_gui')
@patch(
    'availity_app.automation._find_claim_row_indices_by_visit_date_and_amount',
)
@patch(
    'availity_app.automation._find_claim_row_indices_by_invoice',
    return_value=[2],
)
def test_healthfirst_still_matches_invoice(
    mock_invoice, mock_visit_amount, _log,
):
    iframe = MagicMock()
    iframe.locator.return_value.count.return_value = 1
    indices, mode = resolve_matching_claim_indices(
        iframe, pd.DataFrame(), 'INV1',
        visit_date='05/25/2025',
        payer_config=_HEALTHFIRST,
        amount='$109.44',
    )
    mock_invoice.assert_called_once()
    mock_visit_amount.assert_not_called()
    assert indices == [2]
    assert mode == 'invoice'


def _snapshot_row(**overrides):
    base = {
        'AltPatientID': 'M1',
        'Last_Name': 'Doe',
        'First_Name': 'Jane',
        'DOB': '01/01/1990',
        'StartDate': '01/01/2025',
        'EndDate': '01/31/2025',
        'InvoiceNumber': 'INV1',
        'VisitDate': '05/25/2025',
        'Amount': '109.44',
        'AutomationStatus': 'Pending',
        'LastError': '',
        'Recheck': '',
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        base.setdefault(col, '--')
    base.update(overrides)
    return base


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
def test_aetna_write_fills_only_current_row(mock_save, _log):
    df = pd.DataFrame([
        _snapshot_row(Amount='109.44'),
        _snapshot_row(Amount='82.08'),
    ])
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': '25353Z0042260',
            'Billed Amount': '$109.44',
            'Paid Amount': '$0.00',
            'Claim Status': 'DENIED',
        },
        lines=[{
            'date_range': '05/25/2025-05/25/2025',
            'billed': '$109.44',
            'paid': '$0.00',
            'hipaa_codes': '',
            'denial_pairs': [('G18', 'Should be ignored for Aetna')],
        }],
        remittance_denial='Precertification/authorization absent.',
    )
    filled = write_invoice_snapshots_to_rows(
        df, 0, [snapshot], _AETNA, '/tmp',
    )
    assert filled == [0]
    assert df.at[0, 'Claim ID'] == '25353Z0042260'
    assert df.at[0, 'Denial Reason'] == 'Precertification/authorization absent.'
    assert df.at[1, 'Claim ID'] == '--'
    assert df.at[1, 'AutomationStatus'] == 'Pending'
    mock_save.assert_called()


@patch('availity_app.automation.log_to_gui')
def test_aetna_materialize_uses_remittance_not_remark_pairs(_log):
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': '25353Z0042260',
            'Billed Amount': '$109.44',
            'Paid Amount': '$0.00',
            'Claim Status': 'DENIED',
        },
        lines=[{
            'date_range': '05/25/2025-05/25/2025',
            'billed': '$109.44',
            'paid': '$0.00',
            'hipaa_codes': '',
            'denial_pairs': [('G18', 'Remark code should not be used')],
        }],
        remittance_denial='Precertification/authorization absent.',
    )
    claim = materialize_claim_for_visit(snapshot, '05/25/2025', _AETNA)
    assert claim['Denial Reason'] == 'Precertification/authorization absent.'
    assert 'G18' not in claim['Denial Reason']


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_aetna_harvest_skips_remark_codes_and_uses_remittance(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    mock_header.return_value = {
        'Claim ID': '25353Z0042260',
        'Claim Status': 'DENIED',
        'Billed Amount': '$109.44',
        'Paid Amount': '$0.00',
        'Check Number': 'CHK1',
        'Check Date': '12/23/2025',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '05/25/2025-05/25/2025',
        'billed': '$109.44',
        'paid': '$0.00',
        'hipaa_codes': '',
    }]
    mock_harvest.return_value = {
        'denial': 'Precertification/authorization absent.',
        'lines': [],
        'not_found': False,
        'error': None,
    }

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), _AETNA)

    mock_denial_pairs.assert_not_called()
    mock_harvest.assert_called_once()
    assert snapshot['remittance_denial'] == 'Precertification/authorization absent.'
    assert snapshot['lines'][0]['denial_pairs'] == []
    assert snapshot['lines'][0]['billed'] == '$109.44'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_aetna_harvest_uses_partial_line_amounts_for_remittance(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    mock_header.return_value = {
        'Claim ID': '25297Z0048849',
        'Claim Status': 'PAID',
        'Billed Amount': '$106.40',
        'Paid Amount': '$106.40',
        'Check Number': 'EFT205006',
        'Check Date': '10/30/2025',
    }
    mock_scrape.return_value = [
        {
            'row': 'line0',
            'idx': 0,
            'date_range': '10/11/2025-10/11/2025',
            'billed': '$39.90',
            'paid': '$39.90',
            'hipaa_codes': '',
        },
        {
            'row': 'line1',
            'idx': 1,
            'date_range': '10/11/2025-10/11/2025',
            'billed': '$66.50',
            'paid': '$26.60',
            'hipaa_codes': '',
        },
    ]
    mock_harvest.return_value = {
        'denial': (
            'Charge exceeds fee schedule/maximum allowable or '
            'contracted/legislated fee arrangement.'
        ),
        'lines': [],
        'not_found': False,
        'error': None,
    }

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), _AETNA)

    mock_denial_pairs.assert_not_called()
    mock_harvest.assert_called_once()
    assert snapshot['remittance_denial'] == (
        'Charge exceeds fee schedule/maximum allowable or '
        'contracted/legislated fee arrangement.'
    )


@patch('availity_app.automation.log_to_gui')
def test_aetna_materialize_partial_uses_remittance(_log):
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': '25297Z0048849',
            'Billed Amount': '$106.40',
            'Paid Amount': '$66.50',
            'Claim Status': 'PAID',
        },
        lines=[{
            'date_range': '10/11/2025-10/11/2025',
            'billed': '$66.50',
            'paid': '$26.60',
            'hipaa_codes': '',
            'denial_pairs': [],
        }],
        remittance_denial=(
            'Charge exceeds fee schedule/maximum allowable or '
            'contracted/legislated fee arrangement.'
        ),
    )
    claim = materialize_claim_for_visit(snapshot, '10/11/2025', _AETNA)
    assert claim['Claim Status'] == 'Partially Paid'
    assert claim['Denial Reason'] == (
        'Charge exceeds fee schedule/maximum allowable or '
        'contracted/legislated fee arrangement.'
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_healthfirst_harvest_line_denials_when_remittance_on_without_check(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    payer = dict(_HEALTHFIRST)
    payer['uses_remittance'] = True
    mock_header.return_value = {
        'Claim ID': '0201092659677',
        'Claim Status': 'PAID',
        'Billed Amount': '$100.00',
        'Paid Amount': '$40.00',
        'Check Number': '--',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '01/01/2026-01/01/2026',
        'billed': '$100.00',
        'paid': '$40.00',
        'hipaa_codes': '',
    }]
    mock_denial_pairs.return_value = [('45', 'Charge exceeds fee schedule')]

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), payer)

    mock_denial_pairs.assert_called_once()
    mock_harvest.assert_not_called()
    assert snapshot['lines'][0]['denial_pairs'] == [
        ('45', 'Charge exceeds fee schedule'),
    ]
    assert snapshot['remittance_denial'] is None


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_healthfirst_harvest_opens_remittance_when_remarks_blank(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    payer = dict(_HEALTHFIRST)
    mock_header.return_value = {
        'Claim ID': '0201092659677',
        'Claim Status': 'DENIED',
        'Billed Amount': '$100.00',
        'Paid Amount': '$0.00',
        'Check Number': '--',
        'Check Date': '--',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '01/01/2026-01/01/2026',
        'billed': '$100.00',
        'paid': '$0.00',
        'hipaa_codes': '',
    }]
    mock_denial_pairs.return_value = []
    mock_harvest.return_value = {
        'denial': 'From remittance',
        'lines': [],
        'not_found': False,
        'error': None,
    }

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), payer)

    mock_harvest.assert_called_once()
    assert snapshot['remittance_denial'] == 'From remittance'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation._denial_pairs_from_line_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_integra_harvest_skips_remittance_when_hipaa_remarks_present(
    mock_header, mock_scrape, mock_denial_pairs, mock_harvest, _log,
):
    payer = dict(PAYER_CONFIG['Integra'])
    mock_header.return_value = {
        'Claim ID': 'INT1',
        'Claim Status': 'DENIED',
        'Billed Amount': '$100.00',
        'Paid Amount': '$0.00',
    }
    mock_scrape.return_value = [{
        'row': 'line0',
        'idx': 0,
        'date_range': '01/01/2026-01/01/2026',
        'billed': '$100.00',
        'paid': '$0.00',
        'hipaa_codes': 'F2:585',
    }]
    mock_denial_pairs.return_value = [
        ('N59', 'Service not payable per managed care contract'),
    ]

    snapshot = harvest_open_claim_snapshot(MagicMock(), MagicMock(), payer)

    mock_denial_pairs.assert_called_once()
    mock_harvest.assert_not_called()
    assert snapshot['remittance_denial'] is None
    assert snapshot['lines'][0]['denial_pairs'] == [
        ('N59', 'Service not payable per managed care contract'),
    ]
