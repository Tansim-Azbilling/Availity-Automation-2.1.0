"""Invoice-level claim snapshot fan-out (VisitDate reuse)."""
from unittest.mock import MagicMock, patch

import pandas as pd

from availity_app.automation import (
    format_multi_claim_results,
    process_one_row,
    process_search_group,
    row_should_process,
    write_invoice_snapshots_to_rows,
)
from availity_app.claim_snapshots import (
    make_claim_snapshot,
    materialize_claim_for_visit,
    materialize_row_results,
    pending_invoice_sibling_indices,
    unresolved_alignment_result,
)
from availity_app.constants import (
    CLAIM_NOT_FOUND_DENIAL_REASON,
    CLAIM_NOT_FOUND_STATUS,
    FULLY_PAID_DENIAL_REASON,
    KNOWN_ERROR_STATUSES,
    NEED_MANUAL_REVIEW_STATUS,
    OUTPUT_COLUMNS,
    PAYER_CONFIG,
    STATUS_COLUMNS,
)


def _header(**overrides):
    data = {
        'Claim ID': 'C1',
        'Billed Amount': '$100.00',
        'Paid Amount': '$0.00',
        'Claim Status': 'FINALIZED',
        'Finalized Date': '01/10/2025',
        'Check Number': 'CHK1',
        'Check Date': '01/11/2025',
    }
    data.update(overrides)
    return data


def _line(date, billed='$50.00', paid='$0.00', denial_pairs=None, hipaa=''):
    return {
        'date_range': f'{date}-{date}',
        'billed': billed,
        'paid': paid,
        'hipaa_codes': hipaa,
        'denial_pairs': list(denial_pairs or []),
    }


def _snapshot(claim_id='C1', lines=None, header=None, remittance_denial=None):
    hdr = _header(**{'Claim ID': claim_id})
    if header:
        hdr.update(header)
    return make_claim_snapshot(
        header=hdr,
        lines=lines,
        remittance_denial=remittance_denial,
        fingerprint=f'c:{claim_id}',
    )


def _row(**overrides):
    base = {
        'AltPatientID': 'M1',
        'Last_Name': 'Doe',
        'First_Name': 'Jane',
        'DOB': '01/01/1990',
        'StartDate': '01/01/2025',
        'EndDate': '01/31/2025',
        'InvoiceNumber': 'INV1',
        'VisitDate': '01/05/2025',
        'AutomationStatus': 'Pending',
        'LastError': '',
        'Recheck': '',
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        base.setdefault(col, '--')
    base.update(overrides)
    return base


def _invoice_df(rows):
    return pd.DataFrame(rows)


@patch('availity_app.automation.log_to_gui')
def test_two_submissions_both_have_visit(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshots = [
        _snapshot('A', lines=[_line('01/05/2025', billed='$10.00', paid='$0.00')]),
        _snapshot('B', lines=[_line('01/05/2025', billed='$20.00', paid='$0.00')]),
    ]
    claims = materialize_row_results(snapshots, '01/05/2025', payer)
    assert len(claims) == 2
    merged = format_multi_claim_results(claims)
    assert merged['Claim ID'] == '1. A\n2. B'
    assert '1. $10.00' in merged['Billed Amount']
    assert '2. $20.00' in merged['Billed Amount']


@patch('availity_app.automation.log_to_gui')
def test_missing_visit_on_one_submission_is_omitted(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshots = [
        _snapshot('A', lines=[_line('01/05/2025')]),
        _snapshot('B', lines=[_line('01/06/2025')]),
    ]
    claims = materialize_row_results(snapshots, '01/05/2025', payer)
    assert len(claims) == 1
    merged = format_multi_claim_results(claims)
    assert merged['Claim ID'] == 'A'
    assert merged['Claim ID'] != '1. A'


@patch('availity_app.automation.log_to_gui')
def test_visit_on_no_submission_returns_empty(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshots = [
        _snapshot('A', lines=[_line('01/05/2025')]),
        _snapshot('B', lines=[_line('01/06/2025')]),
    ]
    claims = materialize_row_results(snapshots, '01/07/2025', payer)
    assert claims == []
    assert materialize_claim_for_visit(snapshots[0], '01/07/2025', payer) is None


@patch('availity_app.automation.log_to_gui')
def test_header_only_snapshot_applies_to_every_visit(_log):
    payer = PAYER_CONFIG['Villagecaremax']
    snapshot = _snapshot(
        'V1',
        lines=[],
        remittance_denial='Denied: CO-4',
        header={'Billed Amount': '$100.00', 'Paid Amount': '$0.00'},
    )
    for visit in ('01/01/2025', '01/02/2025', '01/03/2025'):
        claim = materialize_claim_for_visit(snapshot, visit, payer)
        assert claim is not None
        assert claim['Claim ID'] == 'V1'
        assert claim['Denial Reason'] == 'Denied: CO-4'


@patch('availity_app.automation.log_to_gui')
def test_empty_line_table_on_line_level_payer_uses_header(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshot = _snapshot(
        'H1',
        lines=[],
        header={'Billed Amount': '$75.00', 'Paid Amount': '$75.00'},
    )
    claim = materialize_claim_for_visit(snapshot, '01/05/2025', payer)
    assert claim is not None
    assert claim['Claim ID'] == 'H1'
    assert claim['Billed Amount'] == '$75.00'
    assert claim['Paid Amount'] == '$75.00'


@patch('availity_app.automation.log_to_gui')
def test_line_level_does_not_header_fallback_when_visit_missing(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshot = _snapshot(
        'H1',
        lines=[_line('01/01/2025')],
        header={'Billed Amount': '$999.00', 'Paid Amount': '$999.00'},
    )
    assert materialize_claim_for_visit(snapshot, '01/05/2025', payer) is None


@patch('availity_app.automation.log_to_gui')
def test_sole_line_fallback_is_disabled_on_reuse_path(_log):
    payer = PAYER_CONFIG['Healthfirst']
    snapshot = _snapshot('H1', lines=[_line('01/01/2025', billed='$5.00')])
    assert materialize_claim_for_visit(snapshot, '01/20/2025', payer) is None


@patch('availity_app.automation.log_to_gui')
def test_hipaa_fully_paid_denial_reason(_log):
    payer = PAYER_CONFIG['Integra']
    snapshot = _snapshot(
        'I1',
        lines=[_line('01/05/2025', billed='$40.00', paid='$40.00')],
    )
    claim = materialize_claim_for_visit(snapshot, '01/05/2025', payer)
    assert claim['Denial Reason'] == FULLY_PAID_DENIAL_REASON
    assert claim['Billed Amount'] == '$40.00'


@patch('availity_app.automation.log_to_gui')
def test_hipaa_101_fanout_uses_harvested_remark_pairs(_log):
    payer = PAYER_CONFIG['Anthem BCBS']
    snapshot = _snapshot(
        'A1',
        lines=[_line(
            '01/05/2025',
            hipaa='F3:101',
            denial_pairs=[(
                'Y3Z',
                'This was not paid because a prior authorization was required.',
            )],
        )],
    )
    claim = materialize_claim_for_visit(snapshot, '01/05/2025', payer)
    assert claim['Denial Reason'] == (
        'This was not paid because a prior authorization was required.'
    )


@patch('availity_app.automation.log_to_gui')
def test_hipaa_585_fanout_uses_harvested_remark_pairs(_log):
    payer = PAYER_CONFIG['Anthem BCBS']
    snapshot = _snapshot(
        'A1',
        lines=[_line(
            '01/06/2025',
            hipaa='F2:585',
            denial_pairs=[(
                'G18',
                'This was not paid because it was submitted for services '
                'that are not allowed based on our contract.',
            )],
        )],
    )
    claim = materialize_claim_for_visit(snapshot, '01/06/2025', payer)
    assert claim['Denial Reason'] == (
        'This was not paid because it was submitted for services that are '
        'not allowed based on our contract.'
    )


def test_pending_siblings_include_same_invoice_and_skip_done():
    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/06/2025'),
        _row(VisitDate='01/07/2025', AutomationStatus='Done'),
        _row(VisitDate='01/08/2025', InvoiceNumber='INV2'),
        _row(VisitDate='01/09/2025', AltPatientID='M2', Last_Name='Smith'),
    ])
    siblings = pending_invoice_sibling_indices(df, 0, row_should_process)
    assert siblings == [0, 1]


def test_recheck_no_sibling_is_not_selected():
    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/06/2025', Recheck='no', AutomationStatus='Skipped'),
    ])
    siblings = pending_invoice_sibling_indices(df, 0, row_should_process)
    assert siblings == [0]


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
def test_write_does_not_overwrite_done_sibling(mock_save, _log):
    payer = PAYER_CONFIG['Healthfirst']
    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(
            VisitDate='01/06/2025',
            AutomationStatus='Done',
            **{'Claim ID': 'KEEP', 'Claim Status': 'Paid'},
        ),
        _row(VisitDate='01/05/2025'),
    ])
    snapshots = [_snapshot('A', lines=[_line('01/05/2025')])]
    filled = write_invoice_snapshots_to_rows(df, 0, snapshots, payer, '/tmp')
    assert 1 not in filled
    assert df.at[1, 'Claim ID'] == 'KEEP'
    assert 0 in filled
    assert 2 in filled
    assert df.at[0, 'Claim ID'] == 'A'
    assert df.at[2, 'Claim ID'] == 'A'
    mock_save.assert_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
def test_write_marks_unmatched_visit_not_found(mock_save, _log):
    payer = PAYER_CONFIG['Healthfirst']
    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/20/2025'),
    ])
    snapshots = [_snapshot('A', lines=[_line('01/05/2025')])]
    write_invoice_snapshots_to_rows(df, 0, snapshots, payer, '/tmp')
    assert df.at[0, 'Claim ID'] == 'A'
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[1, 'Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert df.at[1, 'Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON
    assert df.at[1, 'AutomationStatus'] == 'Done'
    assert df.at[1, 'LastError'] == ''


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.process_one_row')
def test_search_group_skips_follow_on_rows_already_filled(
    mock_process, _visible, mock_state, _log,
):
    mock_state.is_running = True
    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/06/2025'),
    ])

    def _first_fills_siblings(page, frame_df, idx, *args, **kwargs):
        frame_df.at[0, 'AutomationStatus'] = 'Done'
        frame_df.at[1, 'AutomationStatus'] = 'Done'
        filled_out = kwargs.get('filled_indices_out')
        if filled_out is not None:
            filled_out.extend([0, 1])
        return True, page

    mock_process.side_effect = _first_fills_siblings
    _, _page, row_results = process_search_group(
        MagicMock(), df, [0, 1], '/tmp', PAYER_CONFIG['Healthfirst'],
    )
    assert mock_process.call_count == 1
    assert row_results[0] is True
    assert row_results[1] is True


@patch('availity_app.automation.log_to_gui')
def test_healthfirst_materialize_falls_back_to_line_denial_when_remit_blank(_log):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    payer['uses_remittance'] = True
    snapshot = _snapshot(
        '0201092659677',
        header={
            'Claim Status': 'PAID',
            'Billed Amount': '$100.00',
            'Paid Amount': '$40.00',
            'Check Number': '--',
        },
        lines=[_line(
            '01/01/2026',
            billed='$100.00',
            paid='$40.00',
            denial_pairs=[('45', 'Charge exceeds fee schedule')],
        )],
        remittance_denial='--',
    )
    claim = materialize_claim_for_visit(snapshot, '01/01/2026', payer)
    assert claim['Claim Status'] == 'Partially Paid'
    assert claim['Denial Reason'] == 'Charge exceeds fee schedule'


@patch('availity_app.automation.log_to_gui')
def test_healthfirst_materialize_falls_back_when_remittance_not_found(_log):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    payer['uses_remittance'] = True
    snapshot = _snapshot(
        '0201092659677',
        header={'Claim Status': 'PAID'},
        lines=[_line(
            '01/01/2026',
            billed='$100.00',
            paid='$40.00',
            denial_pairs=[('45', 'Charge exceeds fee schedule')],
        )],
        remittance_denial='Remittance not found',
    )
    claim = materialize_claim_for_visit(snapshot, '01/01/2026', payer)
    assert claim['Denial Reason'] == 'Charge exceeds fee schedule'


@patch('availity_app.automation.log_to_gui')
def test_healthfirst_materialize_prefers_line_over_remittance(_log):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    snapshot = _snapshot(
        '0201092659677',
        header={
            'Claim Status': 'PAID',
            'Billed Amount': '$100.00',
            'Paid Amount': '$40.00',
        },
        lines=[_line(
            '01/01/2026',
            billed='$100.00',
            paid='$40.00',
            denial_pairs=[('45', 'Charge exceeds fee schedule')],
        )],
        remittance_denial='From remittance viewer',
    )
    claim = materialize_claim_for_visit(snapshot, '01/01/2026', payer)
    assert claim['Denial Reason'] == 'Charge exceeds fee schedule'


@patch('availity_app.automation.log_to_gui')
def test_healthfirst_materialize_uses_remittance_when_remarks_blank(_log):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    snapshot = _snapshot(
        '0201092659677',
        header={
            'Claim Status': 'DENIED',
            'Billed Amount': '$100.00',
            'Paid Amount': '$0.00',
        },
        lines=[_line(
            '01/01/2026',
            billed='$100.00',
            paid='$0.00',
            denial_pairs=[],
        )],
        remittance_denial='Precertification absent.',
    )
    claim = materialize_claim_for_visit(snapshot, '01/01/2026', payer)
    assert claim['Denial Reason'] == 'Precertification absent.'


@patch('availity_app.automation.log_to_gui')
def test_integra_materialize_uses_hipaa_remarks_over_remittance(_log):
    payer = dict(PAYER_CONFIG['Integra'])
    snapshot = _snapshot(
        'INT1',
        header={'Claim Status': 'DENIED'},
        lines=[_line(
            '01/01/2026',
            billed='$100.00',
            paid='$0.00',
            denial_pairs=[('N59', 'Service not payable per managed care contract')],
            hipaa='F2:585',
        )],
        remittance_denial='From remittance viewer',
    )
    claim = materialize_claim_for_visit(snapshot, '01/01/2026', payer)
    assert claim['Denial Reason'] == 'Service not payable per managed care contract'


def test_need_manual_review_is_a_known_error_status():
    assert NEED_MANUAL_REVIEW_STATUS in KNOWN_ERROR_STATUSES


@patch('availity_app.automation.log_to_gui')
def test_unresolved_alignment_writes_need_manual_review(_log):
    payer = PAYER_CONFIG['SWHNY']
    snapshot = _snapshot('C1', lines=[_line('01/05/2025')])
    snapshot['_align_map'] = {
        0: {
            'unresolved': True,
            'line': None,
            'note': 'duplicate Amount+Units',
        },
    }
    result = unresolved_alignment_result([snapshot], 0, payer)
    assert result['Claim Status'] == NEED_MANUAL_REVIEW_STATUS
    assert result['Billed Amount'] == '--'
    assert result['Paid Amount'] == '--'
    assert result['Claim ID'] == '--'
    assert 'Match Confidence' not in result


@patch('availity_app.automation.log_to_gui')
def test_ambiguous_remit_join_materializes_need_manual_review(_log):
    payer = PAYER_CONFIG['SWHNY']
    snapshot = _snapshot('C1', lines=[_line('01/05/2025')])
    snapshot['remit_join_unresolved'] = True
    results = materialize_row_results([snapshot], '01/05/2025', payer, row_index=0)
    assert len(results) == 1
    assert results[0]['Claim Status'] == NEED_MANUAL_REVIEW_STATUS
    assert results[0]['Billed Amount'] == '--'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation.write_invoice_snapshots_to_rows')
@patch('availity_app.automation.process_all_claims_for_invoice')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch(
    'availity_app.automation.resolve_matching_claim_indices',
    return_value=(list(range(10)), 'invoice'),
)
@patch('availity_app.automation.submit_search_and_wait', return_value='results')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_incomplete_harvest_writes_need_manual_review_not_partial_merge(
    mock_state, mock_retry, mock_ready, mock_fill, mock_submit,
    mock_match, mock_sort, mock_extract, mock_write, mock_reset,
    mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_extract.return_value = (
        [_snapshot('A', lines=[_line('01/05/2025')]) for _ in range(4)],
        False,
    )
    mock_reset.side_effect = lambda page, *_args, **_kwargs: page

    df = _invoice_df([
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/06/2025'),
        _row(VisitDate='01/07/2025'),
    ])
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    mock_write.assert_not_called()
    for idx in (0, 1, 2):
        assert df.at[idx, 'Claim Status'] == NEED_MANUAL_REVIEW_STATUS
        assert df.at[idx, 'AutomationStatus'] == 'Error'
        assert df.at[idx, 'Billed Amount'] == '--'
        assert 'harvest incomplete 4/10' in str(df.at[idx, 'LastError'])
