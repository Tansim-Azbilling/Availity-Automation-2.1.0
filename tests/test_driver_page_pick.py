"""Tests for Availity tab selection with multiple CDP-connected tabs."""
from unittest.mock import patch

from availity_app.constants import (
    AVAILITY_LOGIN_URL,
    AVAILITY_NAVIGATION_HOME,
    REMITTANCE_URLS,
)
from availity_app.driver import (
    _find_best_availity_page,
    _find_logged_in_page,
    is_availity_homepage_url,
    is_availity_login_url,
    is_remittance_viewer_page,
)


class _FakePage:
    def __init__(self, url, closed=False):
        self.url = url
        self._closed = closed

    def is_closed(self):
        return self._closed


def test_prefers_claim_search_over_other_tabs():
    pages = [
        _FakePage("https://example.com"),
        _FakePage(
            "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/"
            "navigation/#/loadApp/?appUrl=%2Fstatic%2Fweb%2Fpost%2Fcs%2F"
            "enhanced-claim-status-ui%2F%23%2Fdashboard"
        ),
        _FakePage(AVAILITY_NAVIGATION_HOME),
    ]
    picked = _find_best_availity_page(pages)
    assert picked is pages[1]


def test_prefers_navigation_shell_over_login_tab():
    pages = [
        _FakePage("https://example.com"),
        _FakePage(AVAILITY_LOGIN_URL),
        _FakePage(AVAILITY_NAVIGATION_HOME),
    ]
    picked = _find_best_availity_page(pages)
    assert picked is pages[2]


def test_skips_closed_pages():
    pages = [
        _FakePage(AVAILITY_NAVIGATION_HOME, closed=True),
        _FakePage(AVAILITY_LOGIN_URL),
    ]
    picked = _find_best_availity_page(pages)
    assert picked is pages[1]


_CLAIM_SEARCH_URL = (
    "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/"
    "navigation/#/loadApp/?appUrl=%2Fstatic%2Fweb%2Fpost%2Fcs%2F"
    "enhanced-claim-status-ui%2F%23%2Fdashboard"
)


def test_prefers_claim_search_over_remittance_tab():
    pages = [
        _FakePage(REMITTANCE_URLS['search']),
        _FakePage(_CLAIM_SEARCH_URL),
        _FakePage(AVAILITY_NAVIGATION_HOME),
    ]
    picked = _find_best_availity_page(pages)
    assert picked is pages[1]


def test_navigation_fallback_skips_remittance_tab():
    pages = [
        _FakePage(REMITTANCE_URLS['search']),
        _FakePage(AVAILITY_NAVIGATION_HOME),
    ]
    picked = _find_best_availity_page(pages)
    assert picked is pages[1]
    assert not is_remittance_viewer_page(picked)


@patch('availity_app.driver.validate_navigation_page_ready', return_value=(True, 'ready'))
@patch('availity_app.driver._get_all_open_pages')
def test_find_logged_in_page_prefers_claim_search_over_remittance(
    mock_pages, _ready,
):
    remit = _FakePage(REMITTANCE_URLS['search'])
    claim = _FakePage(_CLAIM_SEARCH_URL)
    mock_pages.return_value = [remit, claim]
    picked = _find_logged_in_page(remit)
    assert picked is claim


@patch('availity_app.driver.validate_navigation_page_ready', return_value=(True, 'ready'))
@patch('availity_app.driver._get_all_open_pages')
def test_find_logged_in_page_skips_remittance_in_shell_fallback(
    mock_pages, _ready,
):
    remit = _FakePage(REMITTANCE_URLS['search'])
    nav = _FakePage(AVAILITY_NAVIGATION_HOME)
    mock_pages.return_value = [remit, nav]
    picked = _find_logged_in_page(remit)
    assert picked is nav
    assert picked is not remit


def test_is_availity_login_url_matches_login_page():
    assert is_availity_login_url(AVAILITY_LOGIN_URL)
    assert not is_availity_login_url(AVAILITY_NAVIGATION_HOME)
    assert not is_availity_login_url("https://example.com")


def test_is_availity_homepage_url_matches_shell_and_claim_search():
    assert is_availity_homepage_url(AVAILITY_NAVIGATION_HOME)
    assert is_availity_homepage_url(_CLAIM_SEARCH_URL)
    assert not is_availity_homepage_url(AVAILITY_LOGIN_URL)
    assert not is_availity_homepage_url("https://example.com")
