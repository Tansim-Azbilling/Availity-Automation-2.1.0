"""Claim Status iframe LoginError prompts the existing session-expired dialog."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from availity_app.automation import (
    AvailitySessionInactiveError,
    IFRAME_LOGIN_NEEDED_REASON,
    _await_user_session_recovery,
    _iframe_shows_login_needed,
    _raise_if_iframe_login_needed,
    _verify_payer_search_session,
    _wait_for_search_form_in_iframe,
    process_one_row,
    wait_for_page_ready,
)
from availity_app.constants import OUTPUT_COLUMNS, PAYER_CONFIG, STATUS_COLUMNS, get_selector_profile
from availity_app.resilience import BatchCircuitBreaker
from availity_app.utils import retry_with_backoff


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


def _locator(visible=False, wait_error=None):
    loc = MagicMock()
    loc.count.return_value = 1 if visible else 0
    loc.first.is_visible.return_value = bool(visible)
    loc.or_.return_value = loc
    if wait_error is None:
        loc.first.wait_for.return_value = None
    else:
        loc.first.wait_for.side_effect = wait_error
    return loc


def _iframe(*, login_visible=False, form_visible=True):
    def locator(selector):
        is_login = any(
            token in selector
            for token in ('loginNeeded', 'LoginError', 'must login')
        )
        if is_login:
            return _locator(visible=login_visible)
        wait_error = None if form_visible else TimeoutError('form not ready')
        return _locator(visible=form_visible, wait_error=wait_error)

    iframe = MagicMock()
    iframe.locator.side_effect = locator
    return iframe


def test_iframe_shows_login_needed_when_card_visible():
    assert _iframe_shows_login_needed(_iframe(login_visible=True, form_visible=False))
    assert not _iframe_shows_login_needed(_iframe(login_visible=False, form_visible=True))


def test_raise_if_iframe_login_needed_uses_session_reason():
    with pytest.raises(AvailitySessionInactiveError, match='LoginError') as exc_info:
        _raise_if_iframe_login_needed(_iframe(login_visible=True, form_visible=False))
    assert exc_info.value.reason == IFRAME_LOGIN_NEEDED_REASON
    assert exc_info.value.fatal is True


def test_wait_for_search_form_raises_immediately_on_login_needed():
    sel = get_selector_profile(PAYER_CONFIG['Healthfirst'])
    with pytest.raises(AvailitySessionInactiveError, match='You must login to proceed'):
        _wait_for_search_form_in_iframe(
            _iframe(login_visible=True, form_visible=False), sel, timeout_ms=15000,
        )


def test_wait_for_search_form_returns_member_id_when_form_ready():
    sel = get_selector_profile(PAYER_CONFIG['Healthfirst'])
    matched = _wait_for_search_form_in_iframe(
        _iframe(login_visible=False, form_visible=True), sel, timeout_ms=5000,
    )
    assert matched == sel['member_id']


@patch('availity_app.automation.time.sleep')
@patch('availity_app.automation.is_claim_search_page', return_value=True)
@patch('availity_app.automation._iframe_shows_login_needed', return_value=False)
@patch(
    'availity_app.automation._wait_for_search_form_in_iframe',
    side_effect=AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON),
)
def test_wait_for_page_ready_reraises_login_needed(_wait_form, _login, _is_search, _sleep):
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = 'https://essentials.availity.com/enhanced-claim-status-ui/'
    with pytest.raises(AvailitySessionInactiveError) as exc_info:
        wait_for_page_ready(page, PAYER_CONFIG['Healthfirst'], quiet=True)
    assert 'LoginError' in str(exc_info.value)


@patch('availity_app.automation.navigate_to_payer_page')
def test_verify_session_reraises_login_needed(mock_nav):
    mock_nav.side_effect = AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON)
    with pytest.raises(AvailitySessionInactiveError, match='LoginError'):
        _verify_payer_search_session(MagicMock(), PAYER_CONFIG['Healthfirst'])


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.save_progress_results')
@patch('availity_app.automation._prepare_search_form_for_next_row')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation.submit_search_and_wait')
@patch('availity_app.automation.fill_search_form')
@patch(
    'availity_app.automation.wait_for_page_ready',
    side_effect=AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON),
)
@patch('availity_app.automation.retry_with_backoff', side_effect=lambda name, fn, **kw: fn())
@patch('availity_app.automation.state')
def test_process_one_row_raises_on_iframe_login_needed(
    mock_state, _retry, _ready, _fill, _submit, _reload, _prepare, mock_save, _log,
):
    mock_state.is_running = True
    df = _df()
    with pytest.raises(AvailitySessionInactiveError, match='LoginError'):
        process_one_row(
            MagicMock(), df, 0, df.iloc[0], '/tmp', PAYER_CONFIG['Healthfirst'],
            skip_initial_navigate=True,
        )
    assert 'Navigation failed' not in str(df.at[0, 'LastError'])
    mock_save.assert_not_called()
    _fill.assert_not_called()
    _submit.assert_not_called()


@patch('availity_app.automation._ensure_remit_page')
@patch(
    'availity_app.automation._verify_payer_search_session',
    side_effect=AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON),
)
@patch('availity_app.automation._prompt_user_relogin')
@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.setup_browser')
@patch('availity_app.automation.ensure_playwright_disconnected')
@patch('availity_app.automation.state')
def test_await_recovery_stops_if_login_needed_after_resume(
    mock_state, mock_disconnect, mock_setup, _log, _prompt, _verify, _remit,
):
    mock_state.is_running = True
    mock_state.root = None
    mock_state.resume_decision = 'cancel'
    mock_state.resume_event = MagicMock()
    mock_state.resume_event.wait.side_effect = (
        lambda: setattr(mock_state, 'resume_decision', 'resume')
    )
    breaker = BatchCircuitBreaker()
    mock_setup.return_value = MagicMock()

    recovered, _page = _await_user_session_recovery(
        MagicMock(), PAYER_CONFIG['Healthfirst'], breaker, IFRAME_LOGIN_NEEDED_REASON,
    )

    assert recovered is False
    mock_disconnect.assert_called_once()
    mock_setup.assert_called_once()
    _verify.assert_called_once()


def test_retry_with_backoff_does_not_retry_login_needed():
    calls = {'n': 0}

    def boom():
        calls['n'] += 1
        raise AvailitySessionInactiveError(IFRAME_LOGIN_NEEDED_REASON)

    with pytest.raises(AvailitySessionInactiveError):
        retry_with_backoff('Wait for page ready', boom, attempts=3, base_delay=0.01, jitter=0)
    assert calls['n'] == 1
