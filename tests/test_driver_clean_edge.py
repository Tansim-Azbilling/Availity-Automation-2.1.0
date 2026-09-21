"""Tests for isolated Edge launch (never touches other Edge instances)."""
from unittest.mock import MagicMock, patch

from availity_app import driver
from availity_app import state
from availity_app.driver import ensure_edge_with_cdp


def _reset_state():
    state.managed_edge_process = None
    state.browser_owned_by_app = False
    state.cdp_port = None
    state.cdp_endpoint = None


def test_reuses_our_own_live_instance():
    _reset_state()
    state.managed_edge_process = MagicMock()
    state.managed_edge_process.poll.return_value = None
    state.cdp_port = 9222
    with patch("availity_app.driver.is_cdp_available", return_value=True):
        with patch("availity_app.driver._launch_managed_edge_with_cdp") as launch_mock:
            assert ensure_edge_with_cdp() is True
            launch_mock.assert_not_called()


def test_launches_fresh_when_no_managed_instance():
    _reset_state()
    with patch("availity_app.driver._launch_managed_edge_with_cdp", return_value=True) as launch_mock:
        assert ensure_edge_with_cdp() is True
        launch_mock.assert_called_once()


def test_relaunches_when_managed_alive_but_cdp_down():
    _reset_state()
    state.managed_edge_process = MagicMock()
    state.managed_edge_process.poll.return_value = None
    with patch("availity_app.driver.is_cdp_available", return_value=False):
        with patch("availity_app.driver._wait_for_cdp_ready", return_value=False):
            with patch(
                "availity_app.driver._launch_managed_edge_with_cdp", return_value=True
            ) as launch_mock:
                assert ensure_edge_with_cdp() is True
                launch_mock.assert_called_once()


def test_never_kills_other_edge_processes():
    # The kill-all-Edge helpers must no longer exist on the driver module.
    assert not hasattr(driver, "_close_all_edge_processes")
    assert not hasattr(driver, "_prepare_clean_edge_launch")
    assert not hasattr(driver, "_edge_processes_running")
