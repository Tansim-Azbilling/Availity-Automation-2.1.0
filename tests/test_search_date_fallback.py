"""In-place StartDate/EndDate search fallback."""
from unittest.mock import MagicMock, patch

import pandas as pd

from availity_app.automation import (
    apply_start_end_search_fallback,
    process_one_row,
    process_search_group,
    refill_service_search_dates,
    resolve_matching_claim_indices,
    should_retry_search_with_row_dates,
    _resolve_search_outcome,
)
from availity_app.constants import (
    CLAIM_NOT_FOUND_DENIAL_REASON,
    CLAIM_NOT_FOUND_STATUS,
    OUTPUT_COLUMNS,
    PAYER_CONFIG,
    STATUS_COLUMNS,
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
        'Claim Status': '',
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        base.setdefault(col, '--')
    base.update(overrides)
    return base


def _df(**overrides):
    return pd.DataFrame([_row(**overrides)])


def test_should_retry_when_aggregation_differs_from_row_dates():
    assert should_retry_search_with_row_dates(
        True, '01/05/2025', '01/20/2025', '01/01/2025', '01/31/2025',
    ) is True


def test_should_not_retry_when_dates_match():
    assert should_retry_search_with_row_dates(
        True, '01/01/2025', '01/31/2025', '01/01/2025', '01/31/2025',
    ) is False


def test_should_not_retry_when_not_from_aggregation():
    assert should_retry_search_with_row_dates(
        False, '01/05/2025', '01/05/2025', '01/01/2025', '01/31/2025',
    ) is False


def test_should_not_retry_when_row_dates_missing():
    assert should_retry_search_with_row_dates(
        True, '01/05/2025', '01/05/2025', '', '',
    ) is False


@patch('availity_app.automation.fill_date_range')
@patch('availity_app.automation.resolve_provider_npi')
def test_refill_service_search_dates_does_not_fill_npi(mock_npi, mock_fill_range):
    page = MagicMock()
    iframe = MagicMock()
    payer = PAYER_CONFIG['Healthfirst']

    ok = refill_service_search_dates(
        page, iframe, '01/01/2025', '01/31/2025', payer,
    )

    assert ok is True
    mock_npi.assert_not_called()
    iframe.locator.assert_not_called()
    mock_fill_range.assert_called_once()
    args, _kwargs = mock_fill_range.call_args
    assert args[2] == '01/01/2025'
    assert args[3] == '01/31/2025'


@patch('availity_app.automation._fill_plain_date_inputs')
def test_refill_plain_inputs_skips_dob_and_npi(mock_plain):
    page = MagicMock()
    iframe = MagicMock()
    payer = PAYER_CONFIG['Aetna Better Health']

    refill_service_search_dates(
        page, iframe, '02/01/2025', '02/28/2025', payer,
    )

    mock_plain.assert_called_once()
    args = mock_plain.call_args.args
    assert args[2] == ''
    assert args[3] == '02/01/2025'
    assert args[4] == '02/28/2025'
    iframe.locator.assert_not_called()


@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.submit_search_and_wait', return_value='results')
@patch('availity_app.automation.refill_service_search_dates', return_value=True)
def test_apply_fallback_retries_when_dates_differ(
    mock_refill, mock_submit, mock_sort,
):
    row = _row()
    did, outcome = apply_start_end_search_fallback(
        MagicMock(), MagicMock(), row, PAYER_CONFIG['Healthfirst'],
        from_aggregation=True,
        searched_start='01/05/2025',
        searched_end='01/05/2025',
    )
    assert did is True
    assert outcome == 'results'
    mock_refill.assert_called_once()
    assert mock_submit.call_args.kwargs.get('expect_fresh') is True
    mock_sort.assert_called_once()


@patch('availity_app.automation.submit_search_and_wait')
@patch('availity_app.automation.refill_service_search_dates')
def test_apply_fallback_skips_when_dates_match(mock_refill, mock_submit):
    row = _row(VisitDate='01/01/2025', StartDate='01/01/2025', EndDate='01/01/2025')
    did, outcome = apply_start_end_search_fallback(
        MagicMock(), MagicMock(), row, PAYER_CONFIG['Healthfirst'],
        from_aggregation=True,
        searched_start='01/01/2025',
        searched_end='01/01/2025',
    )
    assert did is False
    assert outcome is None
    mock_refill.assert_not_called()
    mock_submit.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices')
@patch('availity_app.automation.submit_search_and_wait')
@patch('availity_app.automation.refill_service_search_dates', return_value=True)
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_retries_on_portal_error(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_match, mock_sort, mock_reload, mock_prepare, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_submit.side_effect = ['not_found', 'not_found']

    df = _df()
    page = MagicMock()
    ok, _page = process_one_row(
        page, df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    assert df.at[0, 'Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert df.at[0, 'Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    mock_refill.assert_called_once()
    assert mock_submit.call_count == 2
    assert mock_submit.call_args_list[1].kwargs.get('expect_fresh') is True
    mock_reload.assert_not_called()
    mock_prepare.assert_called_once()
    mock_match.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.submit_search_and_wait', return_value='not_found')
@patch('availity_app.automation.refill_service_search_dates')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_skips_retry_when_same_dates(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_sort, mock_reload, mock_prepare, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe

    df = _df(VisitDate='01/01/2025', StartDate='01/01/2025', EndDate='01/01/2025')
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    mock_refill.assert_not_called()
    mock_submit.assert_called_once()
    mock_reload.assert_not_called()
    mock_prepare.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices')
@patch('availity_app.automation.submit_search_and_wait')
@patch('availity_app.automation.refill_service_search_dates', return_value=True)
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_retries_when_table_has_no_match(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_match, mock_sort, mock_reload, mock_prepare, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_submit.side_effect = ['results', 'results']
    mock_match.side_effect = [([], ''), ([], '')]

    df = _df()
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    assert df.at[0, 'Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert df.at[0, 'Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    mock_refill.assert_called_once()
    assert mock_submit.call_count == 2
    assert mock_match.call_count == 2
    mock_reload.assert_not_called()
    mock_prepare.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices')
@patch('availity_app.automation.submit_search_and_wait', return_value='unknown')
@patch('availity_app.automation.refill_service_search_dates')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_unknown_search_outcome_writes_claim_not_found(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_match, mock_sort, mock_reload, mock_prepare, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe

    df = _df(VisitDate='01/01/2025', StartDate='01/01/2025', EndDate='01/01/2025')
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    assert df.at[0, 'Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert df.at[0, 'Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    mock_match.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._results_table_is_visible', return_value=False)
@patch('availity_app.automation.process_one_row')
def test_grouped_follow_on_rows_search_when_no_table(
    mock_process, mock_visible, mock_state, mock_log,
):
    mock_state.is_running = True
    mock_process.side_effect = [
        (True, 'page1'),
        (True, 'page2'),
    ]
    df = pd.DataFrame([_row(), _row(InvoiceNumber='INV2', VisitDate='01/20/2025')])

    process_search_group(
        MagicMock(), df, [0, 1], '/tmp', PAYER_CONFIG['Healthfirst'],
    )

    assert mock_process.call_count == 2
    first_kwargs = mock_process.call_args_list[0].kwargs
    assert first_kwargs.get('skip_row_reset') is False
    second_kwargs = mock_process.call_args_list[1].kwargs
    assert second_kwargs.get('skip_search') is False
    assert second_kwargs.get('skip_initial_navigate') is True
    assert second_kwargs.get('skip_row_reset') is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.process_one_row')
def test_grouped_follow_on_rows_reuse_visible_table(
    mock_process, mock_visible, mock_state, mock_log,
):
    mock_state.is_running = True
    mock_process.side_effect = [
        (True, 'page1'),
        (True, 'page2'),
    ]
    df = pd.DataFrame([_row(), _row(InvoiceNumber='INV2', VisitDate='01/20/2025')])

    process_search_group(
        MagicMock(), df, [0, 1], '/tmp', PAYER_CONFIG['Healthfirst'],
    )

    first_kwargs = mock_process.call_args_list[0].kwargs
    assert first_kwargs.get('skip_row_reset') is False
    second_kwargs = mock_process.call_args_list[1].kwargs
    assert second_kwargs.get('skip_search') is True
    assert second_kwargs.get('skip_row_reset') is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.process_one_row')
def test_patient_group_keeps_table_for_follow_on_rows(
    mock_process, mock_visible, mock_reset, mock_state, mock_log,
):
    mock_state.is_running = True
    mock_reset.side_effect = lambda page, payer_config, *, skip_row_reset=False: page

    def _first_then_second(page, frame_df, idx, *args, **kwargs):
        meta = kwargs.get('search_meta_out')
        if meta is not None:
            meta['date_fallback_attempted'] = False
        return True, page

    mock_process.side_effect = _first_then_second
    df = pd.DataFrame([
        _row(),
        _row(InvoiceNumber='INV2', VisitDate='01/20/2025'),
    ])
    payer = dict(PAYER_CONFIG['Villagecaremax'])

    process_search_group(MagicMock(), df, [0, 1], '/tmp', payer)

    first_kwargs = mock_process.call_args_list[0].kwargs
    assert first_kwargs.get('skip_row_reset') is True
    assert first_kwargs.get('skip_search') is False
    second_kwargs = mock_process.call_args_list[1].kwargs
    assert second_kwargs.get('skip_search') is True


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.process_one_row')
def test_patient_group_does_not_reuse_after_date_fallback(
    mock_process, mock_visible, mock_reset, mock_state, mock_log,
):
    mock_state.is_running = True
    mock_reset.side_effect = lambda page, payer_config, *, skip_row_reset=False: page

    def _fallback_then_solo(page, frame_df, idx, *args, **kwargs):
        meta = kwargs.get('search_meta_out')
        if meta is not None:
            meta['date_fallback_attempted'] = True
        return True, page

    mock_process.side_effect = _fallback_then_solo
    df = pd.DataFrame([
        _row(),
        _row(InvoiceNumber='INV2', VisitDate='01/20/2025'),
    ])
    payer = dict(PAYER_CONFIG['Villagecaremax'])

    process_search_group(MagicMock(), df, [0, 1], '/tmp', payer)

    second_kwargs = mock_process.call_args_list[1].kwargs
    assert second_kwargs.get('skip_search') is False


@patch('availity_app.automation._claims_table_has_results', return_value=True)
@patch('availity_app.automation.is_claim_not_found_alert', return_value=True)
@patch('availity_app.automation.is_invalid_member_id_alert', return_value=False)
def test_resolve_search_outcome_prefers_visible_table_over_alert(
    mock_invalid, mock_not_found, mock_has_results,
):
    assert _resolve_search_outcome(MagicMock()) == 'results'
    mock_not_found.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._find_claim_row_indices_by_visit_date', return_value=[2])
@patch('availity_app.automation._find_claim_row_indices_by_service_dates', return_value=[])
@patch('availity_app.automation.aggregate_invoice_visit_date_range', return_value='01/05/2025-01/05/2025')
@patch('availity_app.automation._find_claim_row_indices_by_invoice', return_value=[])
def test_matching_uses_visit_date_when_invoice_and_bundle_miss(
    mock_invoice, mock_agg, mock_bundle, mock_visit, mock_log,
):
    iframe = MagicMock()
    iframe.locator.return_value.count.return_value = 1
    df = _df()

    indices, mode = resolve_matching_claim_indices(
        iframe, df, 'INV1', visit_date='01/05/2025',
    )

    assert indices == [2]
    assert mode == 'visit_date'
    mock_visit.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.process_all_claims_for_invoice')
@patch('availity_app.automation.format_multi_claim_results', return_value={'Claim Status': 'Paid'})
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices', return_value=([0], 'invoice'))
@patch('availity_app.automation._claims_table_has_results', return_value=True)
@patch('availity_app.automation.submit_search_and_wait')
@patch('availity_app.automation.refill_service_search_dates', return_value=True)
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_selects_claim_after_fallback_table(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_has_results, mock_match, mock_sort, mock_format, mock_extract,
    mock_reload, mock_prepare, mock_reset, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_submit.side_effect = ['not_found', 'not_found']
    mock_extract.return_value = ([{
        'header': {
            'Claim ID': 'C1',
            'Claim Status': 'Paid',
            'Billed Amount': '$10.00',
            'Paid Amount': '$10.00',
            'Finalized Date': '--',
            'Check Number': '--',
            'Check Date': '--',
        },
        'lines': [],
        'fingerprint': 'c:C1',
        'results_finalized_date': None,
        'remittance_denial': None,
        'harvest_complete': True,
    }], True)
    mock_reset.side_effect = lambda page, *_args, **_kwargs: page

    df = _df()
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
        skip_initial_navigate=True,
    )

    assert ok is True
    mock_refill.assert_called_once()
    mock_match.assert_called()
    mock_extract.assert_called_once()
    mock_prepare.assert_not_called()
    mock_reload.assert_not_called()
    assert mock_reset.call_args.kwargs.get('skip_row_reset') is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation.write_invoice_snapshots_to_rows', return_value=[0, 1, 2])
@patch('availity_app.automation.process_all_claims_for_invoice')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices', return_value=([0], 'invoice'))
@patch('availity_app.automation.submit_search_and_wait', return_value='results')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_keeps_results_when_skip_row_reset(
    mock_state, mock_retry, mock_ready, mock_fill, mock_submit,
    mock_match, mock_sort, mock_extract, mock_write, mock_reset,
    mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_extract.return_value = ([{
        'header': {
            'Claim ID': 'C1',
            'Claim Status': 'Paid',
            'Billed Amount': '$10.00',
            'Paid Amount': '$10.00',
            'Finalized Date': '--',
            'Check Number': '--',
            'Check Date': '--',
        },
        'lines': [],
        'fingerprint': 'c:C1',
        'results_finalized_date': None,
        'remittance_denial': None,
        'harvest_complete': True,
    }], True)
    mock_reset.side_effect = lambda page, *_args, **_kwargs: page

    df = _df()
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Anthem BCBS'],
        skip_initial_navigate=True,
        skip_row_reset=True,
    )

    assert ok is True
    mock_reset.assert_called_once()
    assert mock_reset.call_args.kwargs.get('skip_row_reset') is True
    mock_submit.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices')
@patch('availity_app.automation.submit_search_and_wait', return_value='unknown')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_search_miss_does_not_keep_results(
    mock_state, mock_retry, mock_ready, mock_fill, mock_submit,
    mock_match, mock_sort, mock_reload, mock_prepare, mock_reset,
    mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_reset.side_effect = lambda page, *_args, **_kwargs: page

    df = _df()
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Villagecaremax'],
        skip_initial_navigate=True,
        skip_row_reset=True,
    )

    assert ok is True
    assert df.at[0, 'Claim Status'] == CLAIM_NOT_FOUND_STATUS
    mock_prepare.assert_called_once()
    mock_reset.assert_not_called()
    mock_match.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation.process_all_claims_for_invoice', return_value=([], True))
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.resolve_matching_claim_indices', return_value=([0], 'invoice'))
@patch('availity_app.automation.submit_search_and_wait', return_value='results')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_harvest_failure_resets_form(
    mock_state, mock_retry, mock_ready, mock_fill, mock_submit,
    mock_match, mock_sort, mock_extract, mock_reset, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe
    mock_reset.side_effect = lambda page, *_args, **_kwargs: page

    df = _df()
    ok, _page = process_one_row(
        MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Villagecaremax'],
        skip_initial_navigate=True,
        skip_row_reset=True,
    )

    assert ok is False
    mock_reset.assert_called_once()
    assert mock_reset.call_args.kwargs.get('skip_row_reset') is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.state')
@patch('availity_app.automation._reset_or_return_to_results')
@patch('availity_app.automation._results_table_is_visible', return_value=False)
@patch('availity_app.automation.process_one_row')
def test_patient_group_first_row_error_remaining_search_themselves(
    mock_process, mock_visible, mock_reset, mock_state, mock_log,
):
    mock_state.is_running = True
    mock_reset.side_effect = lambda page, payer_config, *, skip_row_reset=False: page
    mock_process.side_effect = [
        (False, 'page1'),
        (True, 'page2'),
    ]
    df = pd.DataFrame([
        _row(),
        _row(InvoiceNumber='INV2', VisitDate='01/20/2025'),
    ])
    payer = dict(PAYER_CONFIG['Villagecaremax'])

    process_search_group(MagicMock(), df, [0, 1], '/tmp', payer)

    first_kwargs = mock_process.call_args_list[0].kwargs
    assert first_kwargs.get('skip_row_reset') is True
    second_kwargs = mock_process.call_args_list[1].kwargs
    assert second_kwargs.get('skip_search') is not True
    assert second_kwargs.get('skip_initial_navigate') is True
    assert second_kwargs.get('skip_row_reset') is not True

