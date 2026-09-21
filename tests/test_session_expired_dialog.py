"""Themed Availity session-expired re-login dialog."""
from unittest.mock import patch

import tkinter as tk

import pytest

from availity_app import state
from availity_app.ui_control import _prompt_user_relogin, _show_session_expired_dialog


def _walk_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk_widgets(child)


def _find_button(root, text):
    for widget in _walk_widgets(root):
        if isinstance(widget, tk.Button) and widget.cget("text") == text:
            return widget
    return None


def _find_label_containing(root, text):
    for widget in _walk_widgets(root):
        if isinstance(widget, tk.Label) and text in str(widget.cget("text")):
            return widget
    return None


def _click_dialog_button(root, text):
    for child in root.winfo_children():
        if not isinstance(child, tk.Toplevel):
            continue
        button = _find_button(child, text)
        if button is not None:
            button.invoke()
            return True
    return False


@pytest.fixture(scope="module")
def tk_root():
    """One Tk root for the module — Windows cannot recreate Tk after destroy."""
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def session_root(tk_root):
    previous_root = state.root
    previous_decision = state.resume_decision
    state.root = tk_root
    yield tk_root
    state.root = previous_root
    state.resume_decision = previous_decision
    for child in list(tk_root.winfo_children()):
        if isinstance(child, tk.Toplevel):
            try:
                child.destroy()
            except tk.TclError:
                pass


def test_session_expired_dialog_resume_sets_decision(session_root):
    session_root.after(20, lambda: _click_dialog_button(session_root, "Resume"))
    with patch("availity_app.ui_control.cdp_has_availity_homepage", return_value=True):
        _prompt_user_relogin("Search returned no results table or payer alert after retries")
    assert state.resume_decision == "resume"


def test_session_expired_dialog_stop_sets_decision(session_root):
    session_root.after(20, lambda: _click_dialog_button(session_root, "Stop"))
    with patch("availity_app.ui_control.cdp_has_availity_homepage", return_value=False):
        _prompt_user_relogin("Claim-status shell failed to load")
    assert state.resume_decision == "cancel"


def test_session_expired_dialog_ignores_resume_until_homepage(session_root):
    session_root.after(20, lambda: _click_dialog_button(session_root, "Resume"))
    session_root.after(40, lambda: _click_dialog_button(session_root, "Stop"))
    with patch("availity_app.ui_control.cdp_has_availity_homepage", return_value=False):
        _prompt_user_relogin("LoginError")
    assert state.resume_decision == "cancel"


def test_session_expired_dialog_enables_resume_after_homepage(session_root):
    homepage_ready = {"n": 0}

    def _homepage():
        homepage_ready["n"] += 1
        return homepage_ready["n"] >= 2

    def _click_resume_when_enabled():
        for child in session_root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                continue
            button = _find_button(child, "Resume")
            if button is not None and str(button.cget("state")) == "normal":
                button.invoke()
                return
        session_root.after(20, _click_resume_when_enabled)

    session_root.after(20, _click_resume_when_enabled)
    session_root.after(2000, lambda: _click_dialog_button(session_root, "Stop"))
    with patch("availity_app.ui_control._HOMEPAGE_POLL_MS", 30):
        with patch(
            "availity_app.ui_control.cdp_has_availity_homepage",
            side_effect=_homepage,
        ):
            _prompt_user_relogin("blank search")
    assert state.resume_decision == "resume"


def test_session_expired_dialog_shows_reason_and_actions(session_root):
    reason = "CDP navigation to the payer search page failed"
    seen = {}

    def _inspect():
        for child in session_root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                continue
            seen["title"] = _find_label_containing(child, "Availity session expired")
            seen["reason"] = _find_label_containing(child, reason)
            seen["resume"] = _find_button(child, "Resume")
            seen["stop"] = _find_button(child, "Stop")
            seen["ok"] = _find_button(child, "OK")
            seen["cancel"] = _find_button(child, "Cancel")
            seen["waiting"] = _find_label_containing(child, "Resume enables after")
            child.update_idletasks()
            width = max(child.winfo_width(), child.winfo_reqwidth())
            height = max(child.winfo_height(), child.winfo_reqheight())
            seen["size"] = (width, height)
            if seen["resume"] is not None:
                seen["resume_state"] = str(seen["resume"].cget("state"))
            child.destroy()
            return

    session_root.after(20, _inspect)
    with patch("availity_app.ui_control.cdp_has_availity_homepage", return_value=False):
        proceed = _show_session_expired_dialog(reason)
    assert proceed is False
    assert seen["title"] is not None
    assert seen["reason"] is not None
    assert seen["resume"] is not None
    assert seen["stop"] is not None
    assert seen["waiting"] is not None
    assert seen["ok"] is None
    assert seen["cancel"] is None
    assert seen["resume_state"] == "disabled"
    width, height = seen["size"]
    assert width <= 460
    assert height <= 340
