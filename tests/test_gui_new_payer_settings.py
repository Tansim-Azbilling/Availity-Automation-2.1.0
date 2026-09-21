"""Settings panel exposes the SWHNY and Molina knobs added by the re-merge."""
import tkinter as tk

from availity_app import state
from availity_app.tk_ui import create_gui
from availity_app.user_settings import (
    resolve_payer_config,
    set_settings_path_for_tests,
)

LINE_MATCH_FLAGS = (
    'match_by_invoice_alignment',
    'enable_pool_tiers',
    'allow_split_tiebreak',
    'fallback_within_range',
    'range_match_by_day_position',
)
PAYER_FLAGS = (
    'uses_remittance_billed',
    'reconcile_finalized_date_conflicts',
    'filter_remit_disclaimer_text',
)
VALIDATION_KEYS = (
    'field_read_attempts',
    'field_retry_delay_s',
    'detail_identity_timeout_ms',
    'remittance_attempts',
    'remittance_retry_delay_s',
)


def _walk_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk_widgets(child)


def _find_settings_button(root):
    for widget in _walk_widgets(root):
        if not isinstance(widget, tk.Button):
            continue
        if widget.cget('text') in ('Save', 'Clear', 'Close', 'Browse', 'Open folder',
                                   'Open Edge', 'Start Automation', 'Stop'):
            continue
        if widget.cget('image') or widget.cget('text') == '\uE713':
            return widget
    return None


def _show_payer(root, settings_btn, payer):
    """Select ``payer`` and repaint the settings panel from its config."""
    state.payer_var.set(payer)
    settings_btn.invoke()
    settings_btn.invoke()
    root.update_idletasks()


def test_new_settings_render_and_track_selected_payer(tmp_path):
    set_settings_path_for_tests(str(tmp_path / 'settings.json'))
    root = create_gui()
    try:
        root.update_idletasks()
        for flag in LINE_MATCH_FLAGS + PAYER_FLAGS:
            assert flag in state.settings_flag_vars, flag
        for key in VALIDATION_KEYS:
            assert key in state.result_validation_vars, key
        assert state.search_miss_list_frame is not None

        settings_btn = _find_settings_button(root)
        assert settings_btn is not None

        # Healthfirst opts into none of the new behavior.
        _show_payer(root, settings_btn, 'Healthfirst')
        assert state.npi_blank_var.get()
        for flag in LINE_MATCH_FLAGS + PAYER_FLAGS:
            assert not state.settings_flag_vars[flag].get(), flag
        assert not state.has_result_validation
        for entry in state.result_validation_entries.values():
            assert entry.cget('state') == 'disabled'

        # SWHNY turns on invoice alignment and pool tiers.
        _show_payer(root, settings_btn, 'SWHNY')
        assert not state.npi_blank_var.get()
        assert state.payer_npi_var.get() == '1376097303'
        assert state.settings_flag_vars['match_by_invoice_alignment'].get()
        assert state.settings_flag_vars['enable_pool_tiers'].get()
        assert state.max_combo_var.get() == '3'
        assert state.settings_flag_vars['uses_remittance'].get()
        assert not state.settings_flag_vars['remittance_when_remarks_blank'].get()
        assert not state.has_result_validation

        # Molina turns on day-position matching and extraction validation.
        _show_payer(root, settings_btn, 'Molina Home Health')
        assert state.settings_flag_vars['range_match_by_day_position'].get()
        assert state.settings_flag_vars['fallback_within_range'].get()
        assert state.has_result_validation
        for entry in state.result_validation_entries.values():
            assert entry.cget('state') == 'normal'
        assert state.result_validation_vars['field_read_attempts'].get() == '3'
        assert state.result_validation_vars['remittance_attempts'].get() == '3'
    finally:
        root.destroy()
        set_settings_path_for_tests(None)


def test_edited_validation_numbers_persist_for_molina(tmp_path):
    set_settings_path_for_tests(str(tmp_path / 'settings.json'))
    root = create_gui()
    try:
        settings_btn = _find_settings_button(root)
        _show_payer(root, settings_btn, 'Molina Home Health')

        state.result_validation_vars['field_read_attempts'].set('5')
        state.result_validation_vars['remittance_retry_delay_s'].set('2.5')
        for widget in _walk_widgets(root):
            if isinstance(widget, tk.Button) and widget.cget('text') == 'Save':
                widget.invoke()
                break
        root.update_idletasks()

        saved = resolve_payer_config('Molina Home Health')['result_validation']
        assert saved['field_read_attempts'] == 5
        assert saved['remittance_retry_delay_s'] == 2.5
        # Untouched keys keep their code defaults.
        assert saved['remittance_attempts'] == 3

        # A payer without a validation block is unaffected by the save.
        assert not resolve_payer_config('Healthfirst').get('result_validation')
    finally:
        root.destroy()
        set_settings_path_for_tests(None)
