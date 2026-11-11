"""Tkinter layout and application callbacks."""
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from availity_app import state
from availity_app.automation import open_edge_and_connect, run_batch, save_results
from availity_app.constants import get_gui_payer_names
from availity_app.driver import close_managed_edge_if_owned
from availity_app.logging_gui import log_to_gui, start_session_log
from availity_app.resources import _load_brand_logo, _make_settings_icon, _resource_path
from availity_app.user_settings import (
    MATCH_MODES_ALLOWED,
    SEARCH_MISS_OUTCOME_LABELS,
    SELECTOR_PROFILES_ALLOWED,
    clear_payer_ids,
    clear_settings_load_error,
    get_startup_payer,
    global_form_values,
    payer_form_values,
    save_all_settings,
    search_miss_outcome_key,
    settings_file_path,
    settings_load_error,
)
from availity_app.ui_control import (
    UI_STATE_OPENING,
    UI_STATE_RUNNING,
    _apply_button_style,
    _set_ui_state,
    reset_automation_progress,
    reset_ui_state,
    set_save_progress_indeterminate,
    set_save_progress_status,
    update_excel_row_stats,
)


def _run_open_browser_in_background(payer, excel_path):
    """Spawn the Open Edge & Login worker thread."""
    thread = threading.Thread(
        target=open_edge_and_connect,
        args=(payer, excel_path, state.output_folder_var.get()),
        daemon=True,
    )
    thread.start()


def _run_batch_in_background(batch_size, excel_path, output_folder):
    """Spawn the Excel automation worker thread (Phase 2)."""
    thread = threading.Thread(
        target=run_batch,
        args=(batch_size, excel_path, output_folder),
        daemon=True,
    )
    thread.start()


def browse_excel():
    """Open a file picker restricted to Excel workbooks."""
    filename = filedialog.askopenfilename(
        title="Select Collection File",
        filetypes=[
            ("Excel files", "*.xlsx"),
            ("All files", "*.*"),
        ],
    )
    if filename:
        state.collection_file_var.set(filename)


def browse_folder():
    """Open a folder picker for the output directory."""
    folder = filedialog.askdirectory(title="Select Output Folder")
    if folder:
        state.output_folder_var.set(folder)


def _validate_all_inputs():
    """Return validated (batch, excel, output_dir, payer) or None.

    Pops a messagebox and returns None if anything is missing/invalid.
    """
    excel_file = state.collection_file_var.get()
    output_dir = state.output_folder_var.get()
    payer = state.payer_var.get()

    try:
        batch = int(state.batch_size_var.get())
        if batch <= 0:
            raise ValueError
    except ValueError:
        messagebox.showerror("Error", "Batch size must be a positive integer")
        return None

    if not excel_file:
        messagebox.showerror("Error", "Please select a collection file")
        return None
    if not output_dir:
        messagebox.showerror("Error", "Please select an output folder")
        return None
    if not payer or payer not in get_gui_payer_names():
        messagebox.showerror("Error", "Please select a valid payer")
        return None

    return batch, excel_file, output_dir, payer


def validate_and_open_browser():
    """Phase 1 callback: validate inputs, load Excel, then launch Edge."""

    validated = _validate_all_inputs()
    if validated is None:
        return
    _, excel_file, output_dir, payer = validated

    state.is_running = True
    state.log_text.config(state="normal")
    state.log_text.delete(1.0, tk.END)
    state.log_text.config(state="disabled")

    log_path = start_session_log(output_dir)
    if log_path:
        log_to_gui(f"📝 Activity log file: {log_path}\n", "info")
    else:
        log_to_gui(
            "⚠️ Could not create activity log file in the output folder.\n",
            "error",
        )

    _set_ui_state(UI_STATE_OPENING)
    _run_open_browser_in_background(payer, excel_file)


def validate_and_start_automation():
    """Phase 2 callback: validate inputs (again) and run the Excel automation."""

    if getattr(state, 'recheck_import_busy', False):
        messagebox.showerror('Not ready', 'Wait for the Recheck import to finish.')
        return
    if state.selected_payer_config is None:
        messagebox.showerror(
            "Not ready",
            "Click 'Open Edge' and sign in to Availity first.",
        )
        return

    validated = _validate_all_inputs()
    if validated is None:
        return
    batch, excel_file, output_dir, payer = validated

    if (payer != state.selected_payer_name or
            os.path.abspath(output_dir) != state.loaded_run_output_folder or
            os.path.abspath(excel_file) != os.path.abspath(state.loaded_excel_path or '')):
        messagebox.showerror('Not ready', 'The file, output folder, or payer changed. Click Open Edge again.')
        return

    if state.loaded_run_path is None:
        messagebox.showerror(
            "Not ready",
            "Excel data is not loaded. Click 'Open Edge' again to reload the file.",
        )
        return

    state.is_running = True
    reset_automation_progress("Starting automation…", 0)
    _set_ui_state(UI_STATE_RUNNING)
    _run_batch_in_background(batch, excel_file, output_dir)


def _run_stop_save_in_background(batch_df, batch_folder):
    """Save progress on a worker thread so the Tk main loop stays responsive."""
    def _worker():
        saved_path = None
        error = None
        try:
            set_save_progress_indeterminate(True)
            set_save_progress_status("Saving progress…")
            saved_path = save_results(batch_df, batch_folder, finalize=True)
        except Exception as e:
            error = e
            log_to_gui(f"  ⚠️ Save on stop failed: {e}\n", "error")
        finally:
            set_save_progress_indeterminate(False)

        def _on_done():
            if error is not None:
                messagebox.showerror("Save failed", f"Could not save progress:\n{error}")
            elif saved_path is None:
                messagebox.showwarning(
                    "Save failed",
                    "Could not save a styled Automated.xlsx.\n\n"
                    "Close the file if it is open in Excel, then try again.",
                )
            if not state.worker_busy:
                close_managed_edge_if_owned()
                reset_ui_state(clear_all=False)

        if state.root is not None:
            state.root.after(0, _on_done)

    threading.Thread(target=_worker, daemon=True).start()


def request_stop():
    """Signal worker threads to stop, flush progress to Excel, and reset the GUI."""
    state.is_running = False
    state.resume_decision = "cancel"
    state.resume_event.set()

    if state.loaded_run_path and state.current_batch_df is not None:
        state.stop_save_requested = True
        log_to_gui("💾 Finishing the active row, then exporting progress...\n", "info")
        _apply_button_style(state.stop_button, "stop", False)
        return

    batch_df = state.current_batch_df
    batch_folder = state.current_batch_output_folder
    if batch_df is not None and batch_folder:
        state.stop_save_requested = True
        log_to_gui("💾 Saving progress before stop...\n", "info")
        set_save_progress_status("Saving progress…")
        _apply_button_style(state.stop_button, "stop", False)
        _run_stop_save_in_background(batch_df, batch_folder)
        return

    if not state.worker_busy:
        if state.loaded_run_path:
            export_current_progress()
        close_managed_edge_if_owned()
        reset_ui_state(clear_all=False)


def export_current_progress():
    """Write a full Excel snapshot without blocking Tk or claim processing."""
    path = state.loaded_run_path
    if not path:
        messagebox.showerror('Not ready', 'Open a collection file before exporting.')
        return

    def _worker():
        from availity_app.run_export import export_run
        from availity_app.run_store import RunStore
        try:
            run = RunStore(path)
            folder = run.meta['output_folder']
            run.close()
            saved = export_run(path, folder)
            log_to_gui(f"  💾 Exported: {saved}\n", 'success')
        except Exception as exc:
            log_to_gui(f"  ❌ Export failed: {exc}\n", 'error')

    threading.Thread(target=_worker, daemon=True).start()


def import_recheck_workbook():
    """Apply only Recheck edits from a matching exported workbook."""
    path = state.loaded_run_path
    if not path or state.worker_busy:
        messagebox.showerror('Not ready', 'Open a collection file and wait for processing to finish.')
        return
    workbook = filedialog.askopenfilename(
        title='Import Recheck edits', filetypes=[('Excel workbook', '*.xlsx')],
    )
    if not workbook:
        return

    def _worker():
        from availity_app.run_store import RunStore, import_recheck_edits
        try:
            count = import_recheck_edits(path, workbook)
            run = RunStore(path)
            state.excel_row_stats = run.meta['stats']
            update_excel_row_stats(state.excel_row_stats)
            run.close()
            log_to_gui(f"  ✓ Imported {count} Recheck edit(s).\n", 'success')
        except Exception as exc:
            log_to_gui(f"  ❌ Recheck import failed: {exc}\n", 'error')
        finally:
            state.recheck_import_busy = False

    state.recheck_import_busy = True
    threading.Thread(target=_worker, daemon=True).start()


def create_gui():
    """Build and return the main application window."""

    COLOR_BG = "#FAFAFA"
    COLOR_SURFACE = "#FFFFFF"
    COLOR_BORDER = "#E5E7EB"
    COLOR_BORDER_HOVER = "#D1D5DB"
    COLOR_TEXT = "#111827"
    COLOR_TEXT_BODY = "#374151"
    COLOR_TEXT_MUTED = "#6B7280"
    COLOR_PRIMARY = "#4F46E5"
    COLOR_LOG_BG = "#F9FAFB"
    COLOR_LOG_INFO = "#2563EB"
    COLOR_LOG_OK = "#16A34A"
    COLOR_LOG_ERR = "#DC2626"
    COLOR_STAT_BG = "#F3F4F6"
    COLOR_PROGRESS_TRACK = "#E5E7EB"
    COLOR_PROGRESS_FILL = "#4F46E5"

    FONT_TITLE = ("Segoe UI", 16, "bold")
    FONT_SUBTITLE = ("Segoe UI", 9)
    FONT_LABEL = ("Segoe UI", 10)
    FONT_BUTTON = ("Segoe UI", 10, "bold")
    FONT_INPUT = ("Segoe UI", 10)
    FONT_LOG = ("Consolas", 9)
    FONT_STAT = ("Segoe UI", 10, "bold")

    state.root = tk.Tk()
    state.root.title("Availity Automation")
    state.root.geometry("880x820")
    state.root.configure(bg=COLOR_BG)

    icon_image = _load_brand_logo(_resource_path("azbilling-new-logo.png"), target_height=64)
    if icon_image is not None:
        try:
            state.root.iconphoto(True, icon_image)
            state.root._brand_icon_ref = icon_image
        except tk.TclError:
            pass

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Modern.TCombobox",
        fieldbackground=COLOR_SURFACE,
        background=COLOR_SURFACE,
        foreground=COLOR_TEXT,
        bordercolor=COLOR_BORDER,
        lightcolor=COLOR_BORDER,
        darkcolor=COLOR_BORDER,
        arrowcolor=COLOR_TEXT_BODY,
        padding=6,
        relief="flat",
    )
    style.map(
        "Modern.TCombobox",
        bordercolor=[("focus", COLOR_PRIMARY)],
        lightcolor=[("focus", COLOR_PRIMARY)],
        darkcolor=[("focus", COLOR_PRIMARY)],
    )
    style.configure(
        "Automation.Horizontal.TProgressbar",
        troughcolor=COLOR_PROGRESS_TRACK,
        background=COLOR_PROGRESS_FILL,
        bordercolor=COLOR_BORDER,
        lightcolor=COLOR_PROGRESS_FILL,
        darkcolor=COLOR_PROGRESS_FILL,
        thickness=14,
    )

    state.collection_file_var = tk.StringVar()
    state.output_folder_var = tk.StringVar()
    state.batch_size_var = tk.StringVar(value="10")
    state.payer_var = tk.StringVar(value=get_startup_payer())
    state.total_rows_var = tk.StringVar(value="—")
    state.recheck_rows_var = tk.StringVar(value="—")
    state.new_rows_var = tk.StringVar(value="—")
    state.progress_percent_var = tk.StringVar(value="0%")
    state.progress_status_var = tk.StringVar(value="Waiting to start…")
    state.org_id_var = tk.StringVar()
    state.payer_id_var = tk.StringVar()
    state.payer_npi_var = tk.StringVar()
    state.npi_blank_var = tk.BooleanVar(value=True)
    state.selector_profile_var = tk.StringVar(value="mui")
    state.match_mode_var = tk.StringVar(value="exact")
    state.default_payer_var = tk.StringVar()
    state.default_npi_var = tk.StringVar()
    state.grace_days_var = tk.StringVar()
    state.session_error_limit_var = tk.StringVar()
    state.transient_error_limit_var = tk.StringVar()
    state.medium_save_interval_var = tk.StringVar()
    state.large_save_interval_var = tk.StringVar()
    state.output_formatting_var = tk.BooleanVar(value=True)
    state.uppercase_export_var = tk.BooleanVar(value=False)
    state.settings_flag_vars = {
        "uses_hipaa_tab": tk.BooleanVar(value=False),
        "uses_line_level": tk.BooleanVar(value=False),
        "uses_remittance": tk.BooleanVar(value=False),
        "remittance_when_remarks_blank": tk.BooleanVar(value=False),
        "skip_remittance_when_unposted": tk.BooleanVar(value=False),
        "uses_results_finalized_date": tk.BooleanVar(value=False),
        "reset_via_edit_search": tk.BooleanVar(value=False),
        "read_hipaa_codes": tk.BooleanVar(value=False),
        "canned_paid_denial": tk.BooleanVar(value=False),
        "detect_adjustment_pairs": tk.BooleanVar(value=False),
        "denial_description_only": tk.BooleanVar(value=False),
        "trim_denial_to_first_sentence": tk.BooleanVar(value=False),
        "match_results_by_visit_and_amount": tk.BooleanVar(value=False),
        "supports_search_grouping": tk.BooleanVar(value=True),
        "group_search_by_patient": tk.BooleanVar(value=False),
        "filter_by_visit_date": tk.BooleanVar(value=True),
        "fallback_to_sole_line": tk.BooleanVar(value=True),
        "filter_results_by_visit_date": tk.BooleanVar(value=False),
        "uses_remittance_billed": tk.BooleanVar(value=False),
        "reconcile_finalized_date_conflicts": tk.BooleanVar(value=False),
        "filter_remit_disclaimer_text": tk.BooleanVar(value=False),
        "match_by_invoice_alignment": tk.BooleanVar(value=False),
        "enable_pool_tiers": tk.BooleanVar(value=False),
        "allow_split_tiebreak": tk.BooleanVar(value=False),
        "fallback_within_range": tk.BooleanVar(value=False),
        "range_match_by_day_position": tk.BooleanVar(value=False),
    }
    state.max_combo_var = tk.StringVar(value="3")
    state.has_result_validation = False
    state.result_validation_vars = {
        "field_read_attempts": tk.StringVar(value=""),
        "field_retry_delay_s": tk.StringVar(value=""),
        "detail_identity_timeout_ms": tk.StringVar(value=""),
        "remittance_attempts": tk.StringVar(value=""),
        "remittance_retry_delay_s": tk.StringVar(value=""),
    }
    state.result_validation_entries = {}
    state.settings_status_var = tk.StringVar(value="")
    state.settings_visible = False
    state.payer_npi_entry = None

    main_frame = tk.Frame(state.root, padx=28, pady=24, bg=COLOR_BG)
    main_frame.pack(fill=tk.BOTH, expand=True)

    def _label(parent, text, font=FONT_LABEL, fg=COLOR_TEXT_BODY):
        return tk.Label(parent, text=text, font=font, bg=COLOR_BG, fg=fg, anchor="w")

    def _entry(parent, var, width=46):
        return tk.Entry(
            parent, textvariable=var, width=width,
            font=FONT_INPUT, bg=COLOR_SURFACE, fg=COLOR_TEXT,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_PRIMARY,
            insertbackground=COLOR_PRIMARY,
        )

    def _browse(parent, command):
        return tk.Button(
            parent, text="Browse", command=command,
            font=("Segoe UI", 9), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
            activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_BORDER_HOVER,
            padx=16, pady=5, cursor="hand2",
        )

    def _stat_value(parent, var):
        return tk.Label(
            parent, textvariable=var, font=FONT_STAT,
            bg=COLOR_STAT_BG, fg=COLOR_TEXT, anchor="center",
            padx=12, pady=6,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
        )

    def _hide_settings_panel(event=None):
        if not state.settings_visible:
            return
        if state.settings_panel is not None:
            state.settings_panel.place_forget()
        state.settings_visible = False

    def _sync_npi_blank_state():
        entry = state.payer_npi_entry
        if entry is None:
            return
        if state.npi_blank_var.get():
            entry.config(state="disabled")
        else:
            entry.config(state="normal")

    def _sync_result_validation_state():
        """Grey out the validation fields for payers that don't validate."""
        enabled = "normal" if state.has_result_validation else "disabled"
        for entry in (state.result_validation_entries or {}).values():
            entry.config(state=enabled)

    def _apply_global_form_values(values):
        state.default_payer_var.set(values.get("default_payer") or "")
        state.default_npi_var.set(values.get("default_npi") or "")
        state.grace_days_var.set(str(values.get("remit_check_date_grace_days", "")))
        state.output_formatting_var.set(bool(values.get("output_formatting_enabled")))
        state.uppercase_export_var.set(bool(values.get("uppercase_export_enabled")))
        state.session_error_limit_var.set(str(values.get("session_error_limit", "")))
        state.transient_error_limit_var.set(str(values.get("transient_error_limit", "")))
        state.medium_save_interval_var.set(str(values.get("medium_file_save_interval", "")))
        state.large_save_interval_var.set(str(values.get("large_file_save_interval", "")))
        if state.search_miss_list_frame is not None:
            _set_search_miss_rows(values.get("search_miss_phrases") or [])

    def _apply_payer_form_values(values):
        state.org_id_var.set(values.get("org_id") or "")
        state.payer_id_var.set(values.get("payer_id") or "")
        state.payer_npi_var.set(values.get("npi") or "")
        state.npi_blank_var.set(bool(values.get("npi_blank")))
        state.selector_profile_var.set(values.get("selector_profile") or "mui")
        state.match_mode_var.set(values.get("match_mode") or "exact")
        state.max_combo_var.set(str(values.get("max_combo", "3")))
        for key, var in state.settings_flag_vars.items():
            var.set(bool(values.get(key)))
        state.has_result_validation = bool(values.get("has_result_validation"))
        for key, var in state.result_validation_vars.items():
            var.set(str(values.get(key, "")))
        _sync_npi_blank_state()
        _sync_result_validation_state()

    def _collect_global_values():
        return {
            "default_payer": state.default_payer_var.get(),
            "default_npi": state.default_npi_var.get(),
            "remit_check_date_grace_days": state.grace_days_var.get(),
            "output_formatting_enabled": state.output_formatting_var.get(),
            "uppercase_export_enabled": state.uppercase_export_var.get(),
            "session_error_limit": state.session_error_limit_var.get(),
            "transient_error_limit": state.transient_error_limit_var.get(),
            "medium_file_save_interval": state.medium_save_interval_var.get(),
            "large_file_save_interval": state.large_save_interval_var.get(),
            "search_miss_phrases": _collect_search_miss_phrases(),
        }

    def _collect_payer_values():
        values = {
            "org_id": state.org_id_var.get(),
            "payer_id": state.payer_id_var.get(),
            "npi": state.payer_npi_var.get(),
            "npi_blank": state.npi_blank_var.get(),
            "selector_profile": state.selector_profile_var.get(),
            "match_mode": state.match_mode_var.get(),
            "max_combo": state.max_combo_var.get(),
            "has_result_validation": state.has_result_validation,
        }
        for key, var in state.settings_flag_vars.items():
            values[key] = var.get()
        for key, var in state.result_validation_vars.items():
            values[key] = var.get()
        return values

    def _load_settings_form(*, load_globals=True, clear_status=True):
        payer = state.payer_var.get() if state.payer_var is not None else ""
        if load_globals:
            _apply_global_form_values(global_form_values())
        _apply_payer_form_values(payer_form_values(payer))
        if clear_status and state.settings_status_var is not None:
            state.settings_status_var.set("")

    def _save_settings_ids():
        payer = state.payer_var.get() if state.payer_var is not None else ""
        try:
            save_all_settings(
                _collect_global_values(),
                payer,
                _collect_payer_values(),
            )
        except ValueError as e:
            messagebox.showerror("Settings", str(e))
            return
        except OSError as e:
            messagebox.showerror(
                "Settings",
                f"Could not write settings.json:\n{e}",
            )
            return
        startup = get_startup_payer()
        if state.payer_var.get() not in get_gui_payer_names():
            state.payer_var.set(startup)
        _load_settings_form(clear_status=False)
        if state.settings_status_var is not None:
            state.settings_status_var.set(f"Saved for {payer} to settings.json.")

    def _open_settings_folder():
        folder = os.path.dirname(os.path.abspath(settings_file_path()))
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Settings", "Settings folder is not available.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.run(["open", folder], check=False)
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except OSError as e:
            messagebox.showerror("Settings", f"Could not open the settings folder:\n{e}")

    def _clear_settings_ids():
        payer = state.payer_var.get() if state.payer_var is not None else ""
        clear_payer_ids(payer)
        _apply_payer_form_values(payer_form_values(payer))
        if state.settings_status_var is not None:
            if payer:
                state.settings_status_var.set(f"Restored {payer} defaults.")
            else:
                state.settings_status_var.set("Cleared.")

    def _show_settings_panel():
        if state.settings_panel is None:
            return
        _load_settings_form()
        load_error = settings_load_error()
        if load_error:
            messagebox.showerror("Settings", load_error)
            clear_settings_load_error()
        main_frame.update_idletasks()
        _, y, _, h = main_frame.grid_bbox(0, 0)
        top = y + h
        state.settings_panel.place(x=0, y=top, relwidth=1, relheight=1, height=-top)
        state.settings_panel.lift()
        header_frame.lift()
        state.settings_visible = True

    def _toggle_settings():
        if state.settings_visible:
            _hide_settings_panel()
        else:
            _show_settings_panel()

    header_frame = tk.Frame(main_frame, bg=COLOR_BG)
    header_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 2))

    _label(header_frame, "Availity Automation", font=FONT_TITLE, fg=COLOR_TEXT).pack(
        side=tk.LEFT,
    )

    gear_image = _make_settings_icon(size=16, color=COLOR_TEXT_MUTED)
    settings_button = tk.Button(
        header_frame,
        command=_toggle_settings,
        bg=COLOR_BG,
        fg=COLOR_TEXT_MUTED,
        activebackground=COLOR_LOG_BG,
        activeforeground=COLOR_TEXT,
        disabledforeground=COLOR_TEXT_MUTED,
        relief="flat",
        bd=0,
        highlightthickness=0,
        cursor="hand2",
        padx=4,
        pady=0,
    )
    if gear_image is not None:
        settings_button.config(image=gear_image, text="")
        settings_button._gear_ref = gear_image
    else:
        # Windows Settings cog; avoids the flower-like U+2699 glyph.
        settings_button.config(text="\uE713", font=("Segoe MDL2 Assets", 11))
    settings_button.pack(side=tk.RIGHT)

    _label(
        main_frame, "Automate claim status lookups",
        font=FONT_SUBTITLE, fg=COLOR_TEXT_MUTED,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 16))

    tk.Frame(main_frame, height=1, bg=COLOR_BORDER).grid(
        row=2, column=0, columnspan=3, sticky="ew", pady=(0, 20),
    )

    _label(main_frame, "Collection File").grid(row=3, column=0, sticky="w", pady=8, padx=(0, 16))
    _entry(main_frame, state.collection_file_var).grid(row=3, column=1, sticky="ew", pady=8, padx=(0, 10), ipady=6)
    _browse(main_frame, browse_excel).grid(row=3, column=2, sticky="w", pady=8)

    _label(main_frame, "Output Folder").grid(row=4, column=0, sticky="w", pady=8, padx=(0, 16))
    _entry(main_frame, state.output_folder_var).grid(row=4, column=1, sticky="ew", pady=8, padx=(0, 10), ipady=6)
    _browse(main_frame, browse_folder).grid(row=4, column=2, sticky="w", pady=8)

    stats_frame = tk.Frame(main_frame, bg=COLOR_BG)
    stats_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 12))
    for col, (title, var) in enumerate([
        ("Total Rows Read", state.total_rows_var),
        ("Rows with Recheck", state.recheck_rows_var),
        ("New Rows (Recheck empty)", state.new_rows_var),
    ]):
        cell = tk.Frame(stats_frame, bg=COLOR_BG)
        cell.grid(row=0, column=col, sticky="ew", padx=(0, 12 if col < 2 else 0))
        _label(cell, title, fg=COLOR_TEXT_MUTED).pack(anchor="w")
        _stat_value(cell, var).pack(fill=tk.X, pady=(4, 0))
        stats_frame.grid_columnconfigure(col, weight=1)

    _label(main_frame, "Claim Search Limit").grid(row=6, column=0, sticky="w", pady=8, padx=(0, 16))
    _entry(main_frame, state.batch_size_var, width=18).grid(row=6, column=1, sticky="w", pady=8, padx=(0, 10), ipady=6)

    _label(main_frame, "Select Payer").grid(row=7, column=0, sticky="w", pady=8, padx=(0, 16))
    payer_combo = ttk.Combobox(
        main_frame, textvariable=state.payer_var,
        values=get_gui_payer_names(),
        state="readonly", width=43,
        font=FONT_INPUT, style="Modern.TCombobox",
    )
    payer_combo.grid(row=7, column=1, sticky="w", pady=8, padx=(0, 10), ipady=2)
    payer_combo.bind("<<ComboboxSelected>>", lambda _event: _load_settings_form(load_globals=False))

    btn_frame = tk.Frame(main_frame, bg=COLOR_BG)
    btn_frame.grid(row=8, column=0, columnspan=3, pady=22)

    button_kwargs = dict(
        font=FONT_BUTTON, relief="flat", bd=0,
        padx=22, pady=9, cursor="hand2",
        highlightthickness=2,
    )

    state.open_edge_button = tk.Button(
        btn_frame, text="Open Edge",
        command=validate_and_open_browser, **button_kwargs,
    )
    state.open_edge_button.pack(side=tk.LEFT, padx=8)

    state.start_button = tk.Button(
        btn_frame, text="Start Automation",
        command=validate_and_start_automation, **button_kwargs,
    )
    state.start_button.pack(side=tk.LEFT, padx=8)

    state.stop_button = tk.Button(
        btn_frame, text="Stop",
        command=request_stop, **button_kwargs,
    )
    state.stop_button.pack(side=tk.LEFT, padx=8)

    tools_frame = tk.Frame(main_frame, bg=COLOR_BG)
    tools_frame.grid(row=9, column=0, columnspan=3, pady=(0, 14))
    tk.Button(tools_frame, text="Export current progress",
              command=export_current_progress, **button_kwargs).pack(side=tk.LEFT, padx=8)
    tk.Button(tools_frame, text="Import Recheck edits",
              command=import_recheck_workbook, **button_kwargs).pack(side=tk.LEFT, padx=8)

    _apply_button_style(state.open_edge_button, "open_edge", True)
    _apply_button_style(state.start_button, "start", False)
    _apply_button_style(state.stop_button, "stop", False)

    state.progress_frame = tk.Frame(main_frame, bg=COLOR_SURFACE, highlightthickness=1,
                                    highlightbackground=COLOR_BORDER)
    state.progress_frame.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(0, 16))

    progress_inner = tk.Frame(state.progress_frame, bg=COLOR_SURFACE, padx=16, pady=14)
    progress_inner.pack(fill=tk.X, expand=True)

    progress_header = tk.Frame(progress_inner, bg=COLOR_SURFACE)
    progress_header.pack(fill=tk.X)

    tk.Label(
        progress_header, text="Automation Progress",
        font=("Segoe UI", 10, "bold"), bg=COLOR_SURFACE, fg=COLOR_TEXT,
    ).pack(side=tk.LEFT)

    tk.Label(
        progress_header, textvariable=state.progress_percent_var,
        font=("Segoe UI", 11, "bold"), bg=COLOR_SURFACE, fg=COLOR_PRIMARY,
    ).pack(side=tk.RIGHT)

    state.progress_bar = ttk.Progressbar(
        progress_inner,
        style="Automation.Horizontal.TProgressbar",
        orient="horizontal",
        mode="determinate",
        maximum=100,
        value=0,
    )
    state.progress_bar.pack(fill=tk.X, pady=(10, 8))

    tk.Label(
        progress_inner, textvariable=state.progress_status_var,
        font=("Segoe UI", 9), bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED, anchor="w",
    ).pack(fill=tk.X)

    reset_automation_progress()

    _label(main_frame, "Activity Log", font=("Segoe UI", 10, "bold"), fg=COLOR_TEXT).grid(
        row=11, column=0, sticky="w", pady=(6, 8),
    )

    log_wrap = tk.Frame(main_frame, bg=COLOR_BORDER)
    log_wrap.grid(row=12, column=0, columnspan=3, sticky="nsew")

    log_inner = tk.Frame(log_wrap, bg=COLOR_LOG_BG)
    log_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    scrollbar = tk.Scrollbar(log_inner, bd=0, highlightthickness=0)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    state.log_text = tk.Text(
        log_inner, height=14, width=95,
        yscrollcommand=scrollbar.set, state="disabled", wrap=tk.WORD,
        bg=COLOR_LOG_BG, fg=COLOR_TEXT_BODY,
        font=FONT_LOG, relief="flat", bd=0,
        padx=14, pady=12,
        insertbackground=COLOR_PRIMARY,
        selectbackground="#DBEAFE", selectforeground=COLOR_TEXT,
    )
    state.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=state.log_text.yview)

    state.log_text.tag_config("info", foreground=COLOR_LOG_INFO)
    state.log_text.tag_config("success", foreground=COLOR_LOG_OK)
    state.log_text.tag_config("error", foreground=COLOR_LOG_ERR)

    main_frame.grid_rowconfigure(12, weight=1)
    main_frame.grid_columnconfigure(1, weight=1)

    state.settings_panel = tk.Frame(main_frame, bg=COLOR_BG)
    settings_inner = tk.Frame(
        state.settings_panel,
        bg=COLOR_SURFACE,
        highlightthickness=1,
        highlightbackground=COLOR_BORDER,
    )
    settings_inner.pack(fill=tk.BOTH, expand=True)

    settings_header = tk.Frame(settings_inner, bg=COLOR_SURFACE, padx=16, pady=14)
    settings_header.pack(fill=tk.X)

    tk.Label(
        settings_header, text="Settings",
        font=("Segoe UI", 12, "bold"), bg=COLOR_SURFACE, fg=COLOR_TEXT,
    ).pack(side=tk.LEFT)

    tk.Button(
        settings_header, text="Close",
        command=_hide_settings_panel,
        font=("Segoe UI", 9), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=COLOR_BORDER,
        highlightcolor=COLOR_BORDER_HOVER,
        padx=16, pady=5, cursor="hand2",
    ).pack(side=tk.RIGHT)

    tk.Frame(settings_inner, height=1, bg=COLOR_BORDER).pack(fill=tk.X)

    settings_canvas_wrap = tk.Frame(settings_inner, bg=COLOR_SURFACE)
    settings_canvas_wrap.pack(fill=tk.BOTH, expand=True)

    settings_canvas = tk.Canvas(
        settings_canvas_wrap, bg=COLOR_SURFACE, highlightthickness=0,
    )
    settings_scrollbar = tk.Scrollbar(
        settings_canvas_wrap, orient="vertical", command=settings_canvas.yview,
    )
    settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
    settings_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    state.settings_body = tk.Frame(settings_canvas, bg=COLOR_SURFACE, padx=16, pady=16)
    settings_body_window = settings_canvas.create_window(
        (0, 0), window=state.settings_body, anchor="nw",
    )

    def _on_settings_body_configure(_event):
        settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

    def _on_settings_canvas_configure(event):
        settings_canvas.itemconfigure(settings_body_window, width=event.width)

    def _on_settings_mousewheel(event):
        if not state.settings_visible:
            return
        settings_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    state.settings_body.bind("<Configure>", _on_settings_body_configure)
    settings_canvas.bind("<Configure>", _on_settings_canvas_configure)
    settings_canvas.bind("<Enter>", lambda _e: settings_canvas.bind_all("<MouseWheel>", _on_settings_mousewheel))
    settings_canvas.bind("<Leave>", lambda _e: settings_canvas.unbind_all("<MouseWheel>"))

    def _settings_label(parent, text):
        return tk.Label(
            parent, text=text, font=FONT_LABEL,
            bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY, anchor="w",
        )

    def _settings_heading(parent, text):
        return tk.Label(
            parent, text=text, font=("Segoe UI", 10, "bold"),
            bg=COLOR_SURFACE, fg=COLOR_TEXT, anchor="w",
        )

    def _settings_entry(parent, var, width=36):
        return tk.Entry(
            parent, textvariable=var, width=width,
            font=FONT_INPUT, bg=COLOR_SURFACE, fg=COLOR_TEXT,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_PRIMARY,
            insertbackground=COLOR_PRIMARY,
            disabledbackground=COLOR_LOG_BG,
            disabledforeground=COLOR_TEXT_MUTED,
        )

    def _settings_check(parent, text, var, command=None):
        return tk.Checkbutton(
            parent, text=text, variable=var, command=command,
            font=FONT_LABEL, bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
            activebackground=COLOR_SURFACE, activeforeground=COLOR_TEXT,
            selectcolor=COLOR_SURFACE, anchor="w",
        )

    def _search_miss_outcome_label(outcome):
        return SEARCH_MISS_OUTCOME_LABELS.get(
            search_miss_outcome_key(outcome) or 'not_found',
            SEARCH_MISS_OUTCOME_LABELS['not_found'],
        )

    def _collect_search_miss_phrases():
        entries = []
        for row in state.search_miss_rows or []:
            phrase = " ".join((row["phrase_var"].get() or "").split())
            if not phrase:
                continue
            entries.append({
                "phrase": phrase,
                "outcome": search_miss_outcome_key(row["outcome_var"].get()) or "not_found",
            })
        return entries

    def _clear_search_miss_rows():
        for row in list(state.search_miss_rows or []):
            try:
                row["frame"].destroy()
            except tk.TclError:
                pass
        state.search_miss_rows = []

    def _add_search_miss_row(phrase="", outcome="not_found"):
        parent = state.search_miss_list_frame
        if parent is None:
            return
        if state.search_miss_rows is None:
            state.search_miss_rows = []
        row_frame = tk.Frame(parent, bg=COLOR_SURFACE)
        row_frame.pack(fill=tk.X, pady=3)
        phrase_var = tk.StringVar(value=phrase)
        outcome_var = tk.StringVar(value=_search_miss_outcome_label(outcome))
        _settings_entry(row_frame, phrase_var, width=28).pack(
            side=tk.LEFT, fill=tk.X, expand=True, ipady=4, padx=(0, 8),
        )
        ttk.Combobox(
            row_frame, textvariable=outcome_var,
            values=list(SEARCH_MISS_OUTCOME_LABELS.values()),
            state="readonly", width=18,
            font=FONT_INPUT, style="Modern.TCombobox",
        ).pack(side=tk.LEFT, padx=(0, 8))

        def _remove():
            try:
                row_frame.destroy()
            except tk.TclError:
                pass
            state.search_miss_rows = [
                item for item in (state.search_miss_rows or [])
                if item["frame"] is not row_frame
            ]
            if not state.search_miss_rows:
                _add_search_miss_row()

        tk.Button(
            row_frame, text="Remove", command=_remove,
            font=("Segoe UI", 8), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
            activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_BORDER_HOVER,
            padx=10, pady=3, cursor="hand2",
        ).pack(side=tk.LEFT)
        state.search_miss_rows.append({
            "frame": row_frame,
            "phrase_var": phrase_var,
            "outcome_var": outcome_var,
        })

    def _set_search_miss_rows(entries):
        _clear_search_miss_rows()
        rows = list(entries or [])
        if not rows:
            _add_search_miss_row()
            return
        for entry in rows:
            _add_search_miss_row(
                entry.get("phrase") or "",
                entry.get("outcome") or "not_found",
            )

    body = state.settings_body
    row = 0

    _settings_heading(body, "All payers").grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 8),
    )
    row += 1

    tk.Label(
        body,
        text="These values apply to every payer until you override them below.",
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED, anchor="w",
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
    row += 1

    _settings_label(body, "Default payer").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    ttk.Combobox(
        body, textvariable=state.default_payer_var,
        values=get_gui_payer_names(),
        state="readonly", width=34,
        font=FONT_INPUT, style="Modern.TCombobox",
    ).grid(row=row, column=1, sticky="ew", pady=6)
    row += 1

    _settings_label(body, "Default Provider NPI").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.default_npi_var).grid(
        row=row, column=1, sticky="ew", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Remit check-date grace days").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.grace_days_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    _settings_check(body, "Format Excel output", state.output_formatting_var).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=4,
    )
    row += 1

    _settings_check(
        body, "Uppercase Excel cell text", state.uppercase_export_var,
    ).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=4,
    )
    row += 1

    _settings_label(body, "Session error limit").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.session_error_limit_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Transient error limit").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.transient_error_limit_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Medium save interval (legacy)").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.medium_save_interval_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Large save interval (legacy)").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.large_save_interval_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    miss_header = tk.Frame(body, bg=COLOR_SURFACE)
    miss_header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 4))
    _settings_label(miss_header, "Search miss phrases").pack(side=tk.LEFT)
    tk.Button(
        miss_header, text="Add phrase", command=_add_search_miss_row,
        font=("Segoe UI", 8), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=COLOR_BORDER,
        highlightcolor=COLOR_BORDER_HOVER,
        padx=10, pady=3, cursor="hand2",
    ).pack(side=tk.RIGHT)
    row += 1

    tk.Label(
        body,
        text=(
            "Applies to every Availity payer. If a visible search alert contains "
            "the phrase, the row is Claim Not Found or Invalid Member ID. "
            "Do not add wait/progress banners here."
        ),
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED,
        anchor="w", justify="left", wraplength=520,
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
    row += 1

    state.search_miss_list_frame = tk.Frame(body, bg=COLOR_SURFACE)
    state.search_miss_list_frame.grid(
        row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6),
    )
    state.search_miss_rows = []
    row += 1

    tk.Frame(body, height=1, bg=COLOR_BORDER).grid(
        row=row, column=0, columnspan=2, sticky="ew", pady=14,
    )
    row += 1

    _settings_heading(body, "Selected payer").grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 8),
    )
    row += 1

    tk.Label(
        body,
        text="Applies to the payer selected on the main screen.",
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED, anchor="w",
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
    row += 1

    _settings_label(body, "OrgID").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.org_id_var).grid(
        row=row, column=1, sticky="ew", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Payer ID").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.payer_id_var).grid(
        row=row, column=1, sticky="ew", pady=6, ipady=6,
    )
    row += 1

    _settings_label(body, "Provider NPI").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    state.payer_npi_entry = _settings_entry(body, state.payer_npi_var)
    state.payer_npi_entry.grid(row=row, column=1, sticky="ew", pady=6, ipady=6)
    row += 1

    _settings_check(
        body, "Leave NPI blank", state.npi_blank_var, command=_sync_npi_blank_state,
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
    row += 1

    _settings_label(body, "Selector profile").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    ttk.Combobox(
        body, textvariable=state.selector_profile_var,
        values=list(SELECTOR_PROFILES_ALLOWED),
        state="readonly", width=34,
        font=FONT_INPUT, style="Modern.TCombobox",
    ).grid(row=row, column=1, sticky="ew", pady=6)
    row += 1

    _settings_label(body, "Line match mode").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    ttk.Combobox(
        body, textvariable=state.match_mode_var,
        values=list(MATCH_MODES_ALLOWED),
        state="readonly", width=34,
        font=FONT_INPUT, style="Modern.TCombobox",
    ).grid(row=row, column=1, sticky="ew", pady=6)
    row += 1

    flag_labels = (
        ("uses_hipaa_tab", "Use HIPAA Standard tab"),
        ("uses_line_level", "Use line-level amounts"),
        ("uses_remittance", "Use remittance viewer for denials"),
        ("remittance_when_remarks_blank", "Remittance only when Reason/Remark is --"),
        ("skip_remittance_when_unposted", "Skip remittance when check and paid are --"),
        ("uses_results_finalized_date", "Read Finalized Date from results"),
        ("reset_via_edit_search", "Reset search via Edit Search"),
        ("read_hipaa_codes", "Read HIPAA status codes"),
        ("canned_paid_denial", "Canned paid denial text"),
        ("detect_adjustment_pairs", "Detect adjustment pairs"),
        ("denial_description_only", "Denial description only"),
        ("trim_denial_to_first_sentence", "Trim denial to first sentence"),
        ("match_results_by_visit_and_amount", "Match results by visit date and amount"),
        ("supports_search_grouping", "Reuse search across sibling rows"),
        ("group_search_by_patient", "Group search by patient (90-day visit window)"),
        ("filter_by_visit_date", "Filter lines by visit date"),
        ("fallback_to_sole_line", "Fall back to sole service line"),
        ("filter_results_by_visit_date", "Filter results by visit date"),
        ("uses_remittance_billed", "Read billed and paid from remittance"),
        ("reconcile_finalized_date_conflicts", "Reconcile Finalized Date conflicts"),
        ("filter_remit_disclaimer_text", "Ignore remittance disclaimer text"),
    )
    for key, label in flag_labels:
        _settings_check(body, label, state.settings_flag_vars[key]).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2,
        )
        row += 1

    _settings_heading(body, "Line matching").grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(14, 8),
    )
    row += 1

    tk.Label(
        body,
        text=(
            "For payers whose service lines cannot be told apart by VisitDate."
        ),
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED, anchor="w",
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
    row += 1

    line_match_labels = (
        ("match_by_invoice_alignment", "Match whole invoice to portal lines"),
        ("enable_pool_tiers", "Enable pool-tier matching fallbacks"),
        ("allow_split_tiebreak", "Allow split-bill tiebreak"),
        ("fallback_within_range", "Fall back to within-range line match"),
        ("range_match_by_day_position", "Pick within-range line by day position"),
    )
    for key, label in line_match_labels:
        _settings_check(body, label, state.settings_flag_vars[key]).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2,
        )
        row += 1

    _settings_label(body, "Max line combination size").grid(
        row=row, column=0, sticky="w", pady=6, padx=(0, 16),
    )
    _settings_entry(body, state.max_combo_var, width=12).grid(
        row=row, column=1, sticky="w", pady=6, ipady=6,
    )
    row += 1

    _settings_heading(body, "Extraction validation").grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(14, 8),
    )
    row += 1

    tk.Label(
        body,
        text=(
            "Retry and timeout limits for payers that re-read and verify every "
            "claim field. Disabled for payers without validation."
        ),
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED,
        anchor="w", justify="left", wraplength=520,
    ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
    row += 1

    validation_labels = (
        ("field_read_attempts", "Field read attempts"),
        ("field_retry_delay_s", "Field retry delay (seconds)"),
        ("detail_identity_timeout_ms", "Claim identity timeout (ms)"),
        ("remittance_attempts", "Remittance attempts"),
        ("remittance_retry_delay_s", "Remittance retry delay (seconds)"),
    )
    for key, label in validation_labels:
        _settings_label(body, label).grid(
            row=row, column=0, sticky="w", pady=6, padx=(0, 16),
        )
        entry = _settings_entry(body, state.result_validation_vars[key], width=12)
        entry.grid(row=row, column=1, sticky="w", pady=6, ipady=6)
        state.result_validation_entries[key] = entry
        row += 1
    _sync_result_validation_state()

    body.grid_columnconfigure(1, weight=1)

    settings_footer = tk.Frame(settings_inner, bg=COLOR_SURFACE, padx=16, pady=12)
    settings_footer.pack(fill=tk.X)
    tk.Frame(settings_inner, height=1, bg=COLOR_BORDER).pack(
        fill=tk.X, before=settings_footer,
    )

    save_button = tk.Button(
        settings_footer, text="Save", command=_save_settings_ids,
        font=FONT_BUTTON, relief="flat", bd=0,
        padx=22, pady=9, cursor="hand2",
        highlightthickness=2,
        bg=COLOR_PRIMARY, fg="white",
        activebackground=COLOR_PRIMARY, activeforeground="white",
        highlightbackground=COLOR_PRIMARY, highlightcolor=COLOR_PRIMARY,
    )
    save_button.pack(side=tk.LEFT, padx=(0, 8))

    clear_button = tk.Button(
        settings_footer, text="Clear", command=_clear_settings_ids,
        font=("Segoe UI", 9), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=COLOR_BORDER,
        highlightcolor=COLOR_BORDER_HOVER,
        padx=16, pady=5, cursor="hand2",
    )
    clear_button.pack(side=tk.LEFT)

    open_folder_button = tk.Button(
        settings_footer, text="Open folder", command=_open_settings_folder,
        font=("Segoe UI", 9), bg=COLOR_SURFACE, fg=COLOR_TEXT_BODY,
        activebackground=COLOR_LOG_BG, activeforeground=COLOR_TEXT,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=COLOR_BORDER,
        highlightcolor=COLOR_BORDER_HOVER,
        padx=16, pady=5, cursor="hand2",
    )
    open_folder_button.pack(side=tk.LEFT, padx=(8, 0))

    tk.Label(
        settings_footer, textvariable=state.settings_status_var,
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_LOG_OK, anchor="w",
    ).pack(side=tk.LEFT, padx=(16, 0))

    settings_path_bar = tk.Frame(settings_inner, bg=COLOR_SURFACE, padx=16, pady=12)
    settings_path_bar.pack(fill=tk.X)
    tk.Label(
        settings_path_bar,
        text=settings_file_path(),
        font=FONT_SUBTITLE, bg=COLOR_SURFACE, fg=COLOR_TEXT_MUTED,
        anchor="w", justify="left", wraplength=640,
    ).pack(fill=tk.X)

    _load_settings_form()

    state.root.bind("<Escape>", _hide_settings_panel)

    return state.root
