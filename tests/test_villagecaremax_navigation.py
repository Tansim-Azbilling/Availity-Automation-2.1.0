"""Villagecaremax Back to Results navigation and Edit Search selectors."""
from unittest.mock import MagicMock, patch

from availity_app.automation import (
    _payer_returns_via_back_to_results,
    _return_to_results_view,
)
from availity_app.constants import PAYER_CONFIG, get_selector_profile


def test_villagecaremax_uses_back_to_results_flag():
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Villagecaremax']) is True
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Integra']) is True
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Anthem BCBS']) is True
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Empire']) is True
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Healthfirst']) is False
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Aetna Better Health']) is False
    assert _payer_returns_via_back_to_results(PAYER_CONFIG['Fidelis']) is False
    sel = get_selector_profile(PAYER_CONFIG['Villagecaremax'])
    assert sel['back_to_results'] == 'a:has-text("Back to Results")'
    for name in ('Integra', 'Anthem BCBS', 'Empire'):
        assert get_selector_profile(PAYER_CONFIG[name])['back_to_results'] == (
            'a:has-text("Back to Results")'
        )


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


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation._resolve_page')
def test_villagecaremax_return_clicks_back_to_results_only(
    mock_resolve, mock_iframe, mock_visible, mock_ready, _log,
):
    clicks = []
    iframe = _iframe_recording_clicks(clicks)
    page = MagicMock()
    page.url = 'https://example.com/#/element/standard-details'
    mock_resolve.return_value = page
    mock_iframe.return_value = iframe
    mock_visible.return_value = True
    mock_ready.return_value = iframe
    payer = PAYER_CONFIG['Villagecaremax']

    result = _return_to_results_view(page, payer)

    assert result is iframe
    assert clicks == ['a:has-text("Back to Results")']
    page.go_back.assert_not_called()
    mock_ready.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation._resolve_page')
def test_integra_anthem_empire_return_clicks_back_to_results_only(
    mock_resolve, mock_iframe, mock_visible, mock_ready, _log,
):
    page = MagicMock()
    page.url = 'https://example.com/#/element/standard-details'
    mock_resolve.return_value = page
    mock_visible.return_value = True
    mock_ready.return_value = 'results-iframe'
    for name in ('Integra', 'Anthem BCBS', 'Empire'):
        clicks = []
        iframe = _iframe_recording_clicks(clicks)
        mock_iframe.return_value = iframe
        page.go_back.reset_mock()
        mock_ready.reset_mock()

        result = _return_to_results_view(page, PAYER_CONFIG[name])

        assert result == 'results-iframe'
        assert clicks == ['a:has-text("Back to Results")']
        page.go_back.assert_not_called()
        mock_ready.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible')
@patch('availity_app.automation._is_on_claim_details_page')
@patch('availity_app.automation._resolve_page')
def test_healthfirst_return_still_uses_browser_back(
    mock_resolve, mock_details, mock_visible, mock_ready, _log,
):
    page = MagicMock()
    page.url = 'https://example.com/#/element/standard-details'
    mock_resolve.return_value = page
    mock_details.return_value = True
    mock_visible.return_value = True
    mock_ready.return_value = 'results-iframe'

    result = _return_to_results_view(page, PAYER_CONFIG['Healthfirst'])

    assert result == 'results-iframe'
    page.go_back.assert_called_once_with(
        wait_until='domcontentloaded', timeout=20000,
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.wait_for_claim_results_ready')
@patch('availity_app.automation._results_table_is_visible')
@patch('availity_app.automation._claim_search_iframe')
@patch('availity_app.automation._resolve_page')
def test_villagecaremax_return_raises_when_results_table_missing(
    mock_resolve, mock_iframe, mock_visible, mock_ready, _log,
):
    clicks = []
    iframe = _iframe_recording_clicks(clicks)
    page = MagicMock()
    page.url = 'https://example.com/#/element/standard-details'
    mock_resolve.return_value = page
    mock_iframe.return_value = iframe
    mock_visible.return_value = False

    try:
        _return_to_results_view(page, PAYER_CONFIG['Villagecaremax'])
        raise AssertionError('expected RuntimeError')
    except RuntimeError as exc:
        assert 'Back to Results' in str(exc)

    page.go_back.assert_not_called()
    mock_ready.assert_not_called()
    assert clicks == ['a:has-text("Back to Results")']
