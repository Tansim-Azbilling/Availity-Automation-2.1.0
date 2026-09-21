"""Tk button states and mid-batch re-login dialog (main thread)."""
import tkinter as tk

from availity_app import state as app_state
from availity_app.constants import (
    UI_STATE_IDLE,
    UI_STATE_OPENING,
    UI_STATE_READY,
    UI_STATE_RUNNING,
)
from availity_app.driver import cdp_has_availity_homepage
from availity_app.user_settings import get_startup_payer

# Shared with create_gui() so the session dialog matches the main window.
COLOR_SURFACE = "#FFFFFF"
COLOR_TEXT = "#111827"
COLOR_TEXT_BODY = "#374151"
COLOR_TEXT_MUTED = "#6B7280"
COLOR_WARNING = "#D97706"
COLOR_WARNING_SOFT = "#FEF3C7"
COLOR_WARNING_BG = "#FFFBEB"
COLOR_WARNING_BORDER = "#FCD34D"
COLOR_SUCCESS = "#15803D"

_HOMEPAGE_POLL_MS = 500
_WAITING_FOR_HOMEPAGE_TEXT = (
    "Sign in on the Availity login page. Resume enables after the homepage "
    "loads. Click Stop to end the batch."
)
_HOMEPAGE_READY_TEXT = (
    "Availity homepage detected. Click Resume to reconnect and continue "
    "the batch."
)

FONT_DIALOG_TITLE = ("Segoe UI", 12, "bold")
FONT_DIALOG_BODY = ("Segoe UI", 9)
FONT_DIALOG_REASON_LABEL = ("Segoe UI", 9, "bold")
FONT_DIALOG_REASON = ("Segoe UI", 9)
FONT_BUTTON = ("Segoe UI", 9, "bold")

# Per-button color tokens for active / disabled states.
# Centralized so _apply_button_style() is the single source of truth for
# button appearance across UI state transitions (idle → opening → ready → running).
# Disabled states keep each button's hue (indigo/green/red) but use a soft
# tinted background + lighter border + readable mid-tone text. The colored
# fill keeps each button identifiable at a glance while signaling "inactive."
BUTTON_STYLES = {
    "open_edge": {
        "active":   {"bg": "#4F46E5", "fg": "white",   "border": "#4F46E5"},
        "disabled": {"bg": "#E0E7FF", "fg": "#818CF8", "border": "#E0E7FF"},
    },
    "start": {
        "active":   {"bg": "#16A34A", "fg": "white",   "border": "#16A34A"},
        "disabled": {"bg": "#DCFCE7", "fg": "#16A34A", "border": "#86EFAC"},
    },
    "stop": {
        "active":   {"bg": "#DC2626", "fg": "white",   "border": "#DC2626"},
        "disabled": {"bg": "#FEE2E2", "fg": "#DC2626", "border": "#FCA5A5"},
    },
}


def _apply_button_style(btn, variant, enabled):
    """Apply a variant's active or disabled color set to a Tk button.

    Tk's default disabled rendering ignores ``bg``/``fg`` overrides and
    produces low-contrast OS gray. We instead set both ``fg`` and
    ``disabledforeground`` to the slate text color so the disabled state
    stays readable and consistent across Windows themes.
    """
    if btn is None:
        return
    palette = BUTTON_STYLES[variant]["active" if enabled else "disabled"]
    btn.config(
        state="normal" if enabled else "disabled",
        bg=palette["bg"],
        activebackground=palette["bg"],
        fg=palette["fg"],
        activeforeground=palette["fg"],
        disabledforeground=palette["fg"],
        highlightbackground=palette["border"],
        highlightcolor=palette["border"],
    )


def _set_ui_state(ui_state):
    """Flip the button enables on the Tk main thread.

    All button enable/disable logic lives here so the rest of the code never
    fiddles with Tk widgets from worker threads. Always schedules the actual
    config call via ``app_state.root.after(0, ...)``.
    """
    def _apply():
        if ui_state == UI_STATE_IDLE:
            _apply_button_style(app_state.open_edge_button, "open_edge", True)
            _apply_button_style(app_state.start_button,     "start",     False)
            _apply_button_style(app_state.stop_button,      "stop",      False)
        elif ui_state == UI_STATE_OPENING:
            _apply_button_style(app_state.open_edge_button, "open_edge", False)
            _apply_button_style(app_state.start_button,     "start",     False)
            _apply_button_style(app_state.stop_button,      "stop",      True)
        elif ui_state == UI_STATE_READY:
            _apply_button_style(app_state.open_edge_button, "open_edge", False)
            _apply_button_style(app_state.start_button,     "start",     True)
            _apply_button_style(app_state.stop_button,      "stop",      True)
        elif ui_state == UI_STATE_RUNNING:
            _apply_button_style(app_state.open_edge_button, "open_edge", False)
            _apply_button_style(app_state.start_button,     "start",     False)
            _apply_button_style(app_state.stop_button,      "stop",      True)

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def reset_automation_progress(status="Waiting to start…", percent=0):
    """Reset the automation progress bar to an idle/neutral state."""
    def _apply():
        if app_state.progress_bar is None:
            return
        try:
            app_state.progress_bar.stop()
        except tk.TclError:
            pass
        app_state.progress_bar.configure(mode='determinate', maximum=100, value=percent)
        if app_state.progress_percent_var is not None:
            app_state.progress_percent_var.set(f"{percent}%")
        if app_state.progress_status_var is not None:
            app_state.progress_status_var.set(status)

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def update_automation_progress(current, total, detail=""):
    """Update progress during a batch (call from worker threads).

    Args:
        current: Number of rows completed so far.
        total: Total rows in this batch.
        detail: Optional status line (e.g. current invoice).
    """
    def _apply():
        if app_state.progress_bar is None:
            return
        total_safe = max(int(total), 1)
        current_safe = max(0, min(int(current), total_safe))
        pct = int(round(100 * current_safe / total_safe))
        app_state.progress_bar.configure(maximum=total_safe, value=current_safe)
        if app_state.progress_percent_var is not None:
            app_state.progress_percent_var.set(f"{pct}%")
        if app_state.progress_status_var is not None:
            if detail:
                app_state.progress_status_var.set(detail)
            else:
                app_state.progress_status_var.set(
                    f"{current_safe} of {total_safe} claims processed"
                )

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def finish_automation_progress(current, total, *, stopped=False, complete=False):
    """Set final progress state after batch ends or is stopped."""
    def _apply():
        if app_state.progress_bar is None:
            return
        total_safe = max(int(total), 1)
        current_safe = max(0, min(int(current), total_safe))
        app_state.progress_bar.configure(maximum=total_safe, value=current_safe)
        pct = int(round(100 * current_safe / total_safe)) if total_safe else 0
        if app_state.progress_percent_var is not None:
            app_state.progress_percent_var.set(f"{pct}%")
        if app_state.progress_status_var is not None:
            if complete and current_safe >= total_safe:
                app_state.progress_status_var.set(
                    f"Complete — {current_safe} of {total_safe} claims processed"
                )
            elif stopped:
                app_state.progress_status_var.set(
                    f"Stopped — {current_safe} of {total_safe} claims saved"
                )
            else:
                app_state.progress_status_var.set(
                    f"Finished — {current_safe} of {total_safe} claims processed"
                )

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def set_save_progress_status(status):
    """Update the progress status line during save lock waits (worker or Stop thread)."""
    def _apply():
        if app_state.progress_status_var is not None:
            app_state.progress_status_var.set(status)

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def set_save_progress_indeterminate(active):
    """Switch the progress bar to indeterminate mode during long saves."""
    def _apply():
        if app_state.progress_bar is None:
            return
        if active:
            app_state.progress_bar.configure(mode='indeterminate')
            app_state.progress_bar.start(12)
            if app_state.progress_percent_var is not None:
                app_state.progress_percent_var.set("…")
        else:
            app_state.progress_bar.stop()
            app_state.progress_bar.configure(mode='determinate')

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def update_excel_row_stats(stats):
    """Update the read-only row summary labels on the Tk main thread."""
    if stats is None:
        return

    def _apply():
        if app_state.total_rows_var is not None:
            app_state.total_rows_var.set(str(stats.get('total', 0)))
        if app_state.recheck_rows_var is not None:
            app_state.recheck_rows_var.set(str(stats.get('recheck_yes', 0)))
        if app_state.new_rows_var is not None:
            app_state.new_rows_var.set(str(stats.get('recheck_empty', 0)))

    if app_state.root is not None:
        app_state.root.after(0, _apply)


def reset_ui_state(clear_all=False):
    """Return the UI to idle state.

    Args:
        clear_all: If True (Stop button), clears input fields, row stats, and the log.
                   If False (batch finished naturally), only resets buttons.
    """
    app_state.is_running = False
    app_state.selected_payer_config = None
    app_state.loaded_df = None
    app_state.loaded_excel_path = None
    app_state.excel_row_stats = None
    app_state.current_batch_df = None
    app_state.current_batch_output_folder = None
    app_state.stop_save_requested = False
    app_state.rows_since_last_save = 0
    app_state.last_save_monotonic = 0.0
    app_state.dirty_row_indices = set()
    app_state.resume_event.set()  # release any worker still blocked on the mid-batch dialog

    def _reset():
        _apply_button_style(app_state.open_edge_button, "open_edge", True)
        _apply_button_style(app_state.start_button,     "start",     False)
        _apply_button_style(app_state.stop_button,      "stop",      False)
        if clear_all:
            app_state.collection_file_var.set("")
            app_state.output_folder_var.set("")
            app_state.batch_size_var.set("10")
            app_state.payer_var.set(get_startup_payer())
            if app_state.total_rows_var is not None:
                app_state.total_rows_var.set("—")
            if app_state.recheck_rows_var is not None:
                app_state.recheck_rows_var.set("—")
            if app_state.new_rows_var is not None:
                app_state.new_rows_var.set("—")
            app_state.log_text.config(state="normal")
            app_state.log_text.delete(1.0, tk.END)
            app_state.log_text.config(state="disabled")
        reset_automation_progress()

    if app_state.root is not None:
        app_state.root.after(0, _reset)


def _center_over_parent(dialog, parent, width, height):
    """Place ``dialog`` over the main window, falling back to screen center."""
    try:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        if pw < 50 or ph < 50:
            raise tk.TclError("parent not mapped")
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 2)
    except tk.TclError:
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2)
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def _draw_warning_icon(parent, bg):
    """Amber circle with an exclamation mark — no emoji/font dependency."""
    size = 28
    canvas = tk.Canvas(
        parent, width=size, height=size, bg=bg,
        highlightthickness=0, bd=0,
    )
    pad = 2
    canvas.create_oval(
        pad, pad, size - pad, size - pad,
        fill=COLOR_WARNING_SOFT, outline=COLOR_WARNING, width=2,
    )
    canvas.create_text(
        size / 2, size / 2 - 1,
        text="!", font=("Segoe UI", 12, "bold"), fill=COLOR_WARNING,
    )
    return canvas


def _show_session_expired_dialog(reason):
    """Themed modal: Resume continues the batch, Stop ends it.

    CDP is paused while this dialog is open. Resume stays disabled until a
    CDP tab reaches the Availity homepage. Returns True when the user chooses
    Resume. Modal only to the Tk app so Edge stays usable for re-login.
    """
    parent = app_state.root
    if parent is None:
        return False

    result = {"proceed": False}
    homepage_ready = {"value": False}
    poll_after_id = {"id": None}
    running_at_open = bool(app_state.is_running)
    reason_text = (reason or "").strip() or "Unknown session failure"

    dialog = tk.Toplevel(parent)
    dialog.title("Availity session expired")
    dialog.configure(bg=COLOR_SURFACE)
    dialog.resizable(False, False)
    dialog.transient(parent)

    def _cancel_homepage_poll():
        after_id = poll_after_id["id"]
        if after_id is None:
            return
        poll_after_id["id"] = None
        try:
            dialog.after_cancel(after_id)
        except tk.TclError:
            pass

    def _close(proceed):
        if proceed and not homepage_ready["value"]:
            return
        _cancel_homepage_poll()
        result["proceed"] = proceed
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        try:
            dialog.destroy()
        except tk.TclError:
            pass

    dialog.protocol("WM_DELETE_WINDOW", lambda: _close(False))
    dialog.bind("<Escape>", lambda _event: _close(False))
    dialog.bind("<Return>", lambda _event: _close(True))

    tk.Frame(dialog, bg=COLOR_WARNING, height=4).pack(fill=tk.X)

    body = tk.Frame(dialog, bg=COLOR_SURFACE)
    body.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)

    header = tk.Frame(body, bg=COLOR_SURFACE)
    header.pack(fill=tk.X, pady=(0, 8))
    _draw_warning_icon(header, COLOR_SURFACE).pack(side=tk.LEFT, padx=(0, 10))
    title_col = tk.Frame(header, bg=COLOR_SURFACE)
    title_col.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Label(
        title_col, text="Availity session expired",
        font=FONT_DIALOG_TITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT, anchor="w",
    ).pack(fill=tk.X)
    tk.Label(
        title_col, text="Sign in again to keep this batch running",
        font=FONT_DIALOG_BODY, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED, anchor="w",
    ).pack(fill=tk.X)

    tk.Label(
        body,
        text="Your Availity session is no longer usable.",
        font=FONT_DIALOG_BODY, bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        wraplength=360, justify="left", anchor="w",
    ).pack(fill=tk.X, pady=(0, 8))

    reason_card = tk.Frame(
        body, bg=COLOR_WARNING_BG,
        highlightthickness=1, highlightbackground=COLOR_WARNING_BORDER,
    )
    reason_card.pack(fill=tk.X, pady=(0, 8))
    reason_inner = tk.Frame(reason_card, bg=COLOR_WARNING_BG)
    reason_inner.pack(fill=tk.X, padx=10, pady=8)
    tk.Label(
        reason_inner, text="Reason",
        font=FONT_DIALOG_REASON_LABEL, bg=COLOR_WARNING_BG, fg=COLOR_WARNING,
        anchor="w",
    ).pack(fill=tk.X)
    tk.Label(
        reason_inner, text=reason_text,
        font=FONT_DIALOG_REASON, bg=COLOR_WARNING_BG, fg=COLOR_TEXT,
        wraplength=340, justify="left", anchor="w",
    ).pack(fill=tk.X, pady=(2, 0))

    instruction = tk.Label(
        body,
        text=_WAITING_FOR_HOMEPAGE_TEXT,
        font=FONT_DIALOG_BODY, bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        wraplength=360, justify="left", anchor="w",
    )
    instruction.pack(fill=tk.X, pady=(0, 12))

    btn_row = tk.Frame(body, bg=COLOR_SURFACE)
    btn_row.pack(fill=tk.X)

    button_kwargs = dict(
        font=FONT_BUTTON, relief="flat", bd=0,
        padx=16, pady=6, cursor="hand2",
        highlightthickness=2,
    )

    resume_btn = tk.Button(
        btn_row, text="Resume", command=lambda: _close(True), **button_kwargs,
    )
    _apply_button_style(resume_btn, "start", False)
    resume_btn.pack(side=tk.RIGHT)

    stop_btn = tk.Button(
        btn_row, text="Stop", command=lambda: _close(False), **button_kwargs,
    )
    _apply_button_style(stop_btn, "stop", True)
    stop_btn.pack(side=tk.RIGHT, padx=(0, 8))

    def _mark_homepage_ready():
        if homepage_ready["value"]:
            return
        homepage_ready["value"] = True
        _cancel_homepage_poll()
        instruction.config(text=_HOMEPAGE_READY_TEXT, fg=COLOR_SUCCESS)
        _apply_button_style(resume_btn, "start", True)
        resume_btn.focus_set()

    def _poll_homepage():
        poll_after_id["id"] = None
        try:
            if not dialog.winfo_exists():
                return
        except tk.TclError:
            return
        if running_at_open and not app_state.is_running:
            _close(False)
            return
        try:
            homepage_visible = cdp_has_availity_homepage()
        except Exception:
            homepage_visible = False
        if homepage_visible:
            _mark_homepage_ready()
            return
        try:
            poll_after_id["id"] = dialog.after(_HOMEPAGE_POLL_MS, _poll_homepage)
        except tk.TclError:
            pass

    dialog.update_idletasks()
    _center_over_parent(
        dialog, parent, dialog.winfo_reqwidth(), dialog.winfo_reqheight(),
    )

    dialog.deiconify()
    dialog.lift()
    try:
        dialog.grab_set()
    except tk.TclError:
        pass
    stop_btn.focus_set()
    _poll_homepage()
    dialog.wait_window()
    return result["proceed"]


def _prompt_user_relogin(reason):
    """Show a modal asking the user to re-login, then unblock the worker.

    Always invoked on the Tk main thread via ``app_state.root.after(0, ...)``.
    """
    try:
        proceed = _show_session_expired_dialog(reason)
        app_state.resume_decision = "resume" if proceed else "cancel"
    except Exception:
        app_state.resume_decision = "cancel"
    finally:
        app_state.resume_event.set()
