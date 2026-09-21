"""Tests for CDP port availability helpers and free-port selection."""
from unittest.mock import MagicMock, patch

from availity_app import state
from availity_app.driver import (
    _find_free_cdp_port,
    cdp_has_availity_homepage,
    ensure_edge_with_cdp,
    is_cdp_available,
    is_cdp_port_free,
)


def _reset_state():
    state.managed_edge_process = None
    state.browser_owned_by_app = False
    state.cdp_port = None
    state.cdp_endpoint = None


def test_is_cdp_available_when_version_endpoint_responds():
    with patch(
        "availity_app.driver._cdp_version_info",
        return_value={"webSocketDebuggerUrl": "ws://x"},
    ):
        assert is_cdp_available() is True


def test_is_cdp_available_when_version_endpoint_missing():
    with patch("availity_app.driver._cdp_version_info", return_value=None):
        assert is_cdp_available() is False


def test_is_cdp_port_free_when_tcp_port_closed():
    with patch("availity_app.driver._is_cdp_port_open", return_value=False):
        assert is_cdp_port_free() is True


def test_is_cdp_port_free_when_tcp_port_open():
    with patch("availity_app.driver._is_cdp_port_open", return_value=True):
        assert is_cdp_port_free() is False


def test_find_free_cdp_port_skips_occupied_port():
    # 9222 is in use (e.g. by another automation), 9223 is free.
    def _open(port):
        return port == 9222

    with patch("availity_app.driver._is_cdp_port_open", side_effect=_open):
        assert _find_free_cdp_port() == 9223


def test_find_free_cdp_port_returns_none_when_all_occupied():
    with patch("availity_app.driver._is_cdp_port_open", return_value=True):
        assert _find_free_cdp_port(start=9222, count=3) is None


def test_ensure_edge_with_cdp_launches_when_no_managed_instance():
    _reset_state()
    with patch("availity_app.driver._launch_managed_edge_with_cdp", return_value=True) as launch_mock:
        assert ensure_edge_with_cdp() is True
        launch_mock.assert_called_once()


def test_ensure_edge_with_cdp_reuses_our_live_instance():
    _reset_state()
    state.managed_edge_process = MagicMock()
    state.managed_edge_process.poll.return_value = None
    with patch("availity_app.driver.is_cdp_available", return_value=True):
        with patch("availity_app.driver._launch_managed_edge_with_cdp") as launch_mock:
            assert ensure_edge_with_cdp() is True
            launch_mock.assert_not_called()


def test_cdp_has_availity_homepage_when_navigation_tab_listed():
    tabs = [
        {"type": "page", "url": "https://essentials.availity.com/static/public/onb/onboarding-ui-apps/availity-fr-ui/#/login"},
        {"type": "page", "url": "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/#/"},
    ]
    with patch("availity_app.driver._cdp_http_get", return_value=tabs):
        assert cdp_has_availity_homepage() is True


def test_cdp_has_availity_homepage_false_on_login_only():
    tabs = [
        {"type": "page", "url": "https://essentials.availity.com/static/public/onb/onboarding-ui-apps/availity-fr-ui/#/login"},
    ]
    with patch("availity_app.driver._cdp_http_get", return_value=tabs):
        assert cdp_has_availity_homepage() is False


def test_cdp_has_availity_homepage_false_when_cdp_unavailable():
    with patch("availity_app.driver._cdp_http_get", return_value=None):
        assert cdp_has_availity_homepage() is False
