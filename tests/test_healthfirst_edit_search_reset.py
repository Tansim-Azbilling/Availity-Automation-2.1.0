"""Healthfirst Edit Search + Clear Form reset for the next invoice group."""
import pytest
from unittest.mock import MagicMock, patch

from availity_app import state
from availity_app.automation import (
    _click_first_visible,
    _payer_resets_via_edit_search,
    _recycle_open_remit_tab,
    _reset_or_return_to_results,
    _reset_search_for_next_row,
    _reset_search_via_edit_search,
)
from availity_app.constants import CLAIM_SPA_REMOUNT_EVERY, PAYER_CONFIG, get_selector_profile


@pytest.fixture(autouse=True)
def _reset_remount_counter():
    state.invoice_searches_since_remount = 0
    state.remit_page = None
    yield
    state.invoice_searches_since_remount = 0
    state.remit_page = None


def test_healthfirst_uses_edit_search_reset_flag():
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Healthfirst']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Aetna Better Health']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Villagecaremax']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Integra']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Anthem BCBS']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Empire']) is True
    assert _payer_resets_via_edit_search(PAYER_CONFIG['Fidelis']) is False
    sel = get_selector_profile(PAYER_CONFIG['Healthfirst'])
    assert sel['edit_search'] == 'a:has-text("Edit Search")'
    assert sel['clear_form'] == '#clear-by276'
    assert 'Clear Form' in sel['clear_form_fallback']
    vcm_sel = get_selector_profile(PAYER_CONFIG['Villagecaremax'])
    assert vcm_sel['edit_search'] == 'a:has-text("Edit Search")'
    assert vcm_sel['clear_form'] == '#clear-by276'
    assert vcm_sel['back_to_results'] == 'a:has-text("Back to Results")'


def test_aetna_uses_edit_search_and_clear_form_ids():
    sel = get_selector_profile(PAYER_CONFIG['Aetna Better Health'])
    assert sel['edit_search'] == '#editSearch'
    assert sel['clear_form'] == '#clearForm'
    assert 'Clear Form' in sel['clear_form_fallback']


def _iframe_recording_clicks(clicks, fail_substrings=()):
    """iframe.locator(sel) records successful clicks and can fail by substring."""
    iframe = MagicMock()

    def locator(selector):
        loc = MagicMock()
        if any(part in selector for part in fail_substrings):
            loc.first.wait_for.side_effect = Exception('not visible')
            loc.first.click.side_effect = Exception('not visible')
            return loc
        loc.first.wait_for.return_value = None

        def _click(**_kwargs):
            clicks.append(selector)

        loc.first.click.side_effect = _click
        return loc

    iframe.locator.side_effect = locator
    return iframe


def test_click_first_visible_skips_duplicate_and_failed_selectors():
    clicks = []
    iframe = _iframe_recording_clicks(clicks, fail_substrings=('#missing',))
    matched = _click_first_visible(
        iframe, ('#missing', '#clear-by276', '#clear-by276'), timeout_ms=100,
    )
    assert matched == '#clear-by276'
    assert clicks == ['#clear-by276']


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_for_search_form_cleared')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation.get_live_page')
def test_reset_via_edit_search_clicks_edit_then_clear(
    mock_live, mock_iframe, mock_ready, mock_wait_cleared, _log,
):
    clicks = []
    iframe = _iframe_recording_clicks(clicks)
    page = MagicMock()
    mock_live.return_value = page
    mock_iframe.return_value = iframe
    mock_ready.return_value = iframe

    result = _reset_search_via_edit_search(page, PAYER_CONFIG['Healthfirst'])

    assert result is page
    assert clicks == ['a:has-text("Edit Search")', '#clear-by276']
    mock_wait_cleared.assert_called_once()
    assert mock_ready.call_count == 2
    mock_ready.assert_called_with(page, payer_config=PAYER_CONFIG['Healthfirst'], quiet=True)


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_for_search_form_cleared')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation.get_live_page')
def test_integra_anthem_empire_reset_clicks_edit_then_clear(
    mock_live, mock_iframe, mock_ready, mock_wait_cleared, _log,
):
    for name in ('Integra', 'Anthem BCBS', 'Empire'):
        clicks = []
        iframe = _iframe_recording_clicks(clicks)
        page = MagicMock()
        mock_live.return_value = page
        mock_iframe.return_value = iframe
        mock_ready.return_value = iframe
        mock_wait_cleared.reset_mock()
        mock_ready.reset_mock()
        payer = PAYER_CONFIG[name]

        result = _reset_search_via_edit_search(page, payer)

        assert result is page
        assert clicks == ['a:has-text("Edit Search")', '#clear-by276']
        mock_wait_cleared.assert_called_once()
        assert mock_ready.call_count == 2
        mock_ready.assert_called_with(page, payer_config=payer, quiet=True)


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_for_search_form_in_iframe')
@patch('availity_app.automation._wait_for_search_form_cleared')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation.get_live_page')
def test_reset_via_edit_search_skips_edit_when_already_on_form(
    mock_live, mock_iframe, mock_ready, mock_wait_cleared, mock_wait_form, _log,
):
    clicks = []
    iframe = _iframe_recording_clicks(clicks, fail_substrings=('Edit Search',))
    page = MagicMock()
    mock_live.return_value = page
    mock_iframe.return_value = iframe
    mock_ready.return_value = iframe

    result = _reset_search_via_edit_search(page, PAYER_CONFIG['Healthfirst'])

    assert result is page
    mock_wait_form.assert_called_once()
    mock_wait_cleared.assert_called_once()
    assert clicks == ['#clear-by276']
    mock_ready.assert_called_once_with(
        page, payer_config=PAYER_CONFIG['Healthfirst'], quiet=True,
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_healthfirst_reset_uses_edit_search_not_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page

    result = _reset_search_for_next_row(page, PAYER_CONFIG['Healthfirst'])

    assert result is page
    mock_edit.assert_called_once_with(page, PAYER_CONFIG['Healthfirst'])
    mock_reload.assert_not_called()
    mock_retry.assert_not_called()
    mock_ready.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_healthfirst_reset_falls_back_to_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.side_effect = Exception('Edit Search not found')
    mock_reload.return_value = page

    result = _reset_search_for_next_row(page, PAYER_CONFIG['Healthfirst'])

    assert result is page
    mock_reload.assert_called_once_with(page, PAYER_CONFIG['Healthfirst'])
    mock_retry.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_integra_reset_uses_edit_search_not_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    payer = PAYER_CONFIG['Integra']

    result = _reset_search_for_next_row(page, payer)

    assert result is page
    mock_edit.assert_called_once_with(page, payer)
    mock_reload.assert_not_called()
    mock_retry.assert_not_called()
    mock_ready.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_anthem_and_empire_reset_uses_edit_search_not_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    for name in ('Anthem BCBS', 'Empire'):
        mock_edit.reset_mock()
        mock_reload.reset_mock()
        payer = PAYER_CONFIG[name]

        result = _reset_search_for_next_row(page, payer)

        assert result is page
        mock_edit.assert_called_once_with(page, payer)
        mock_reload.assert_not_called()
        mock_retry.assert_not_called()
        mock_ready.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_for_search_form_cleared')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation.get_live_page')
def test_aetna_reset_clicks_edit_search_then_clear_form(
    mock_live, mock_iframe, mock_ready, mock_wait_cleared, _log,
):
    clicks = []
    iframe = _iframe_recording_clicks(clicks)
    page = MagicMock()
    mock_live.return_value = page
    mock_iframe.return_value = iframe
    mock_ready.return_value = iframe
    payer = PAYER_CONFIG['Aetna Better Health']

    result = _reset_search_via_edit_search(page, payer)

    assert result is page
    assert clicks == ['#editSearch', '#clearForm']
    mock_wait_cleared.assert_called_once()
    assert mock_ready.call_count == 2
    mock_ready.assert_called_with(page, payer_config=payer, quiet=True)


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_aetna_reset_uses_edit_search_not_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    payer = PAYER_CONFIG['Aetna Better Health']

    result = _reset_search_for_next_row(page, payer)

    assert result is page
    mock_edit.assert_called_once_with(page, payer)
    mock_reload.assert_not_called()
    mock_retry.assert_not_called()
    mock_ready.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_villagecaremax_reset_uses_edit_search_not_reload(
    mock_edit, mock_reload, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    payer = PAYER_CONFIG['Villagecaremax']

    result = _reset_search_for_next_row(page, payer)

    assert result is page
    mock_edit.assert_called_once_with(page, payer)
    mock_reload.assert_not_called()
    mock_retry.assert_not_called()
    mock_ready.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.get_live_page')
@patch('availity_app.automation._reset_search_for_next_row')
@patch('availity_app.automation._return_to_results_view')
def test_reset_after_invoice_uses_edit_search_not_back_to_results(
    mock_back, mock_reset, mock_live, mock_visible, mock_ready, mock_sort, _log,
):
    page = MagicMock()
    mock_live.return_value = page
    mock_reset.return_value = page
    payer = PAYER_CONFIG['Anthem BCBS']

    result = _reset_or_return_to_results(page, payer, skip_row_reset=False)

    assert result is page
    mock_reset.assert_called_once_with(page, payer)
    mock_back.assert_not_called()
    mock_visible.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._recycle_open_remit_tab')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_edit_search_resets_stay_cheap_until_remount_interval(
    mock_edit, mock_reload, mock_recycle, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    payer = PAYER_CONFIG['Healthfirst']

    for _ in range(CLAIM_SPA_REMOUNT_EVERY - 1):
        result = _reset_search_for_next_row(page, payer)
        assert result is page

    assert mock_edit.call_count == CLAIM_SPA_REMOUNT_EVERY - 1
    mock_reload.assert_not_called()
    mock_recycle.assert_not_called()
    assert state.invoice_searches_since_remount == CLAIM_SPA_REMOUNT_EVERY - 1


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._recycle_open_remit_tab')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_25th_reset_remounts_recycles_remit_then_edit_search_resumes(
    mock_edit, mock_reload, mock_recycle, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    mock_reload.return_value = page
    payer = PAYER_CONFIG['Healthfirst']

    for _ in range(CLAIM_SPA_REMOUNT_EVERY - 1):
        _reset_search_for_next_row(page, payer)

    result = _reset_search_for_next_row(page, payer)

    assert result is page
    mock_reload.assert_called_once_with(page, payer)
    mock_recycle.assert_called_once_with(page)
    mock_retry.assert_called()
    assert mock_edit.call_count == CLAIM_SPA_REMOUNT_EVERY - 1
    assert state.invoice_searches_since_remount == 0

    mock_edit.reset_mock()
    mock_reload.reset_mock()
    mock_recycle.reset_mock()
    mock_retry.reset_mock()

    result = _reset_search_for_next_row(page, payer)

    assert result is page
    mock_edit.assert_called_once_with(page, payer)
    mock_reload.assert_not_called()
    mock_recycle.assert_not_called()
    mock_retry.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._recycle_open_remit_tab')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_scheduled_remount_does_not_open_remittance(
    mock_edit, mock_reload, mock_recycle, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_edit.return_value = page
    mock_reload.return_value = page
    payer = PAYER_CONFIG['Healthfirst']
    state.invoice_searches_since_remount = CLAIM_SPA_REMOUNT_EVERY - 1
    state.remit_page = None

    _reset_search_for_next_row(page, payer)

    mock_reload.assert_called_once()
    mock_recycle.assert_called_once_with(page)
    mock_edit.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.retry_with_backoff')
@patch('availity_app.automation.wait_for_page_ready')
@patch('availity_app.automation._recycle_open_remit_tab')
@patch('availity_app.automation._reload_and_navigate')
@patch('availity_app.automation._reset_search_via_edit_search')
def test_fidelis_recycles_remit_only_on_scheduled_remount(
    mock_edit, mock_reload, mock_recycle, mock_ready, mock_retry, _log,
):
    page = MagicMock()
    mock_reload.return_value = page
    payer = PAYER_CONFIG['Fidelis']

    for _ in range(CLAIM_SPA_REMOUNT_EVERY - 1):
        _reset_search_for_next_row(page, payer)

    mock_edit.assert_not_called()
    assert mock_reload.call_count == CLAIM_SPA_REMOUNT_EVERY - 1
    mock_recycle.assert_not_called()

    _reset_search_for_next_row(page, payer)

    assert mock_reload.call_count == CLAIM_SPA_REMOUNT_EVERY
    mock_recycle.assert_called_once_with(page)
    assert state.invoice_searches_since_remount == 0


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._goto_remit_home_fallback')
def test_recycle_skips_when_remit_tab_missing(mock_home, mock_ensure, _log):
    claim = MagicMock()
    state.remit_page = None

    _recycle_open_remit_tab(claim)

    mock_home.assert_not_called()
    mock_ensure.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._goto_remit_home_fallback')
def test_recycle_skips_when_remit_tab_closed(mock_home, mock_ensure, _log):
    claim = MagicMock()
    remit = MagicMock()
    remit.is_closed.return_value = True
    state.remit_page = remit

    _recycle_open_remit_tab(claim)

    mock_home.assert_not_called()
    mock_ensure.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._goto_remit_home_fallback')
def test_recycle_reloads_live_remit_tab(mock_home, mock_ensure, _log):
    claim = MagicMock()
    claim.is_closed.return_value = False
    remit = MagicMock()
    remit.is_closed.return_value = False
    state.remit_page = remit

    _recycle_open_remit_tab(claim)

    mock_home.assert_called_once_with(remit)
    mock_ensure.assert_not_called()
    claim.bring_to_front.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._ensure_remit_page')
@patch('availity_app.automation._goto_remit_home_fallback')
def test_recycle_reopens_remit_tab_when_home_reload_fails(
    mock_home, mock_ensure, _log,
):
    claim = MagicMock()
    claim.is_closed.return_value = False
    remit = MagicMock()
    remit.is_closed.return_value = False
    state.remit_page = remit
    mock_home.side_effect = Exception('home failed')

    _recycle_open_remit_tab(claim)

    remit.close.assert_called_once()
    mock_ensure.assert_called_once_with(claim)
    claim.bring_to_front.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.get_live_page')
@patch('availity_app.automation._reset_search_for_next_row')
@patch('availity_app.automation._return_to_results_view')
@patch('availity_app.automation._back_to_results_link_is_visible')
def test_keep_results_counts_harvest_toward_remount(
    mock_back_visible, mock_back, mock_reset, mock_live, mock_visible,
    mock_ready, mock_sort, _log,
):
    page = MagicMock()
    mock_live.return_value = page
    mock_reset.return_value = page
    payer = PAYER_CONFIG['Villagecaremax']

    result = _reset_or_return_to_results(page, payer, skip_row_reset=True)

    assert result is page
    mock_reset.assert_not_called()
    mock_back.assert_not_called()
    mock_back_visible.assert_not_called()
    mock_visible.assert_called()
    mock_sort.assert_called_once()
    assert state.invoice_searches_since_remount == 1


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible', return_value=True)
@patch('availity_app.automation.get_live_page')
@patch('availity_app.automation._reset_search_for_next_row')
@patch('availity_app.automation._return_to_results_view')
@patch('availity_app.automation._back_to_results_link_is_visible')
def test_keep_results_remounts_at_interval_instead_of_staying(
    mock_back_visible, mock_back, mock_reset, mock_live, mock_visible,
    mock_ready, mock_sort, _log,
):
    page = MagicMock()
    mock_live.return_value = page
    mock_reset.return_value = page
    payer = PAYER_CONFIG['Villagecaremax']
    state.invoice_searches_since_remount = CLAIM_SPA_REMOUNT_EVERY - 1

    result = _reset_or_return_to_results(page, payer, skip_row_reset=True)

    assert result is page
    mock_reset.assert_called_once_with(page, payer, already_counted=True)
    mock_back.assert_not_called()
    mock_visible.assert_not_called()
    mock_sort.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible', return_value=False)
@patch('availity_app.automation.get_live_page')
@patch('availity_app.automation._reset_search_for_next_row')
@patch('availity_app.automation._return_to_results_view')
@patch('availity_app.automation._back_to_results_link_is_visible', return_value=False)
def test_keep_results_resets_when_back_to_results_missing(
    mock_back_visible, mock_back, mock_reset, mock_live, mock_visible,
    mock_ready, mock_sort, _log,
):
    page = MagicMock()
    mock_live.return_value = page
    mock_reset.return_value = page
    payer = PAYER_CONFIG['Villagecaremax']

    result = _reset_or_return_to_results(page, payer, skip_row_reset=True)

    assert result is page
    mock_back.assert_not_called()
    mock_reset.assert_called_once_with(page, payer, already_counted=True)
    mock_sort.assert_not_called()
    assert state.invoice_searches_since_remount == 1


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.sort_claim_results_by_finalized_date')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible', return_value=False)
@patch('availity_app.automation.get_live_page')
@patch('availity_app.automation._reset_search_for_next_row')
@patch('availity_app.automation._return_to_results_view')
@patch('availity_app.automation._back_to_results_link_is_visible', return_value=True)
def test_keep_results_clicks_back_to_results_from_claim_detail(
    mock_back_visible, mock_back, mock_reset, mock_live, mock_visible,
    mock_ready, mock_sort, _log,
):
    page = MagicMock()
    mock_live.return_value = page
    mock_reset.return_value = page
    mock_back.return_value = page
    payer = PAYER_CONFIG['Villagecaremax']

    result = _reset_or_return_to_results(page, payer, skip_row_reset=True)

    assert result is page
    mock_back.assert_called_once_with(page, payer)
    mock_reset.assert_not_called()
    mock_sort.assert_called_once()
    assert state.invoice_searches_since_remount == 1
