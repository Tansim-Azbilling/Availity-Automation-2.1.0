"""Blank Search after retries prompts session re-login instead of skipping the row."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from availity_app.automation import (
    AvailitySessionInactiveError,
    SEARCH_RESPONSE_TIMEOUT_MS,
    _await_user_session_recovery,
    _wait_for_search_response,
    process_one_row,
    submit_search_and_wait,
)
from availity_app.constants import OUTPUT_COLUMNS, PAYER_CONFIG, STATUS_COLUMNS
from availity_app.resilience import BatchCircuitBreaker


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


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.submit_search_and_wait', return_value='failed')
@patch('availity_app.automation.refill_service_search_dates')
@patch('availity_app.automation.fill_search_form', return_value=True)
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.state')
def test_process_one_row_raises_on_blank_search(
    mock_state, mock_retry, mock_ready, mock_fill, mock_refill, mock_submit,
    mock_reload, mock_prepare, mock_save, mock_log,
):
    mock_state.is_running = True
    iframe = MagicMock()
    mock_retry.return_value = iframe
    mock_ready.return_value = iframe

    df = _df()
    with pytest.raises(AvailitySessionInactiveError) as exc_info:
        process_one_row(
            MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
            skip_initial_navigate=True,
        )

    assert 'no results table or payer alert' in str(exc_info.value).lower()
    assert df.at[0, 'AutomationStatus'] in ('Pending', 'InProgress', '--', '')
    assert 'Search failed' not in str(df.at[0, 'LastError'])
    mock_prepare.assert_not_called()
    mock_save.assert_not_called()
    mock_refill.assert_not_called()
    mock_submit.assert_called_once()


@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._verify_payer_search_session')
@patch('availity_app.automation._prompt_user_relogin')
@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.setup_browser')
@patch('availity_app.automation.ensure_playwright_disconnected')
@patch('availity_app.automation.state')
def test_await_recovery_stops_on_cancel(
    mock_state, mock_disconnect, mock_setup, mock_log, mock_prompt, mock_verify, mock_remit,
):
    mock_state.is_running = True
    mock_state.root = None
    mock_state.resume_decision = 'cancel'
    mock_state.resume_event = MagicMock()
    breaker = BatchCircuitBreaker()

    recovered, _page = _await_user_session_recovery(
        MagicMock(), PAYER_CONFIG['Healthfirst'], breaker, 'blank search',
    )

    assert recovered is False
    mock_disconnect.assert_called_once()
    mock_setup.assert_not_called()
    mock_verify.assert_not_called()
    assert breaker.consecutive_session_errors == 1


@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._verify_payer_search_session', return_value=(True, MagicMock()))
@patch('availity_app.automation._prompt_user_relogin')
@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.setup_browser')
@patch('availity_app.automation.ensure_playwright_disconnected')
@patch('availity_app.automation.state')
def test_await_recovery_continues_after_resume(
    mock_state, mock_disconnect, mock_setup, mock_log, mock_prompt, mock_verify, mock_remit,
):
    mock_state.is_running = True
    mock_state.root = None
    mock_state.resume_decision = 'cancel'
    mock_state.resume_event = MagicMock()
    mock_state.resume_event.wait.side_effect = (
        lambda: setattr(mock_state, 'resume_decision', 'resume')
    )
    breaker = BatchCircuitBreaker()
    page = MagicMock()
    reconnected = MagicMock()
    mock_setup.return_value = reconnected

    recovered, out_page = _await_user_session_recovery(
        page, PAYER_CONFIG['Healthfirst'], breaker, 'blank search',
    )

    assert recovered is True
    assert out_page is mock_verify.return_value[1]
    mock_disconnect.assert_called_once()
    mock_setup.assert_called_once()
    mock_verify.assert_called_once()
    assert mock_verify.call_args[0][0] is reconnected
    assert breaker.consecutive_session_errors == 0


@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._verify_payer_search_session')
@patch('availity_app.automation._prompt_user_relogin')
@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.setup_browser', side_effect=RuntimeError('CDP port did not become ready'))
@patch('availity_app.automation.ensure_playwright_disconnected')
@patch('availity_app.automation.state')
def test_await_recovery_stops_if_cdp_resume_fails(
    mock_state, mock_disconnect, mock_setup, mock_log, mock_prompt, mock_verify, mock_remit,
):
    mock_state.is_running = True
    mock_state.root = None
    mock_state.resume_decision = 'cancel'
    mock_state.resume_event = MagicMock()
    mock_state.resume_event.wait.side_effect = (
        lambda: setattr(mock_state, 'resume_decision', 'resume')
    )
    breaker = BatchCircuitBreaker()

    recovered, _page = _await_user_session_recovery(
        MagicMock(), PAYER_CONFIG['Healthfirst'], breaker, 'blank search',
    )

    assert recovered is False
    mock_disconnect.assert_called_once()
    mock_setup.assert_called_once()
    mock_verify.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff', side_effect=lambda name, fn, **kw: fn())
@patch('availity_app.automation._resolve_search_outcome', return_value='results')
@patch('availity_app.automation._wait_for_search_response')
@patch('availity_app.automation._search_response_ready', return_value=False)
def test_submit_search_waits_timeout_per_attempt(
    mock_ready, mock_wait, mock_resolve, mock_retry, _log,
):
    iframe = MagicMock()
    outcome = submit_search_and_wait(iframe)

    assert outcome == 'results'
    assert SEARCH_RESPONSE_TIMEOUT_MS == 30000
    mock_wait.assert_called_once_with(iframe, timeout_ms=SEARCH_RESPONSE_TIMEOUT_MS)


@patch('availity_app.automation._interruptible_sleep')
@patch('availity_app.automation.state')
@patch('availity_app.automation._search_response_ready', return_value=False)
def test_wait_for_search_response_caps_at_timeout(mock_ready, mock_state, mock_sleep):
    mock_state.is_running = True
    clock = {'t': 0.0}

    def fake_sleep(seconds):
        clock['t'] += float(seconds)

    mock_sleep.side_effect = fake_sleep
    with patch('availity_app.automation.time.time', side_effect=lambda: clock['t']):
        _wait_for_search_response(MagicMock(), timeout_ms=SEARCH_RESPONSE_TIMEOUT_MS)

    timeout_s = SEARCH_RESPONSE_TIMEOUT_MS / 1000.0
    assert timeout_s <= clock['t'] < timeout_s + 2.0
