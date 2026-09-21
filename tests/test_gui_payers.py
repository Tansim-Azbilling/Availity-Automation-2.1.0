"""GUI payer dropdown for Availity Automation."""
import tkinter as tk

from availity_app import state
from availity_app.constants import PAYER_CONFIG, get_default_gui_payer, get_gui_payer_names
from availity_app.tk_ui import create_gui
from availity_app.user_settings import (
    default_org_payer_ids,
    get_uppercase_export_enabled,
    load_payer_ids,
    set_settings_path_for_tests,
)


def test_gui_shows_all_payers():
    names = get_gui_payer_names()
    assert names == list(PAYER_CONFIG.keys())
    assert get_default_gui_payer() == names[0]
    assert set(names) == {
        'Healthfirst',
        'Integra',
        'Villagecaremax',
        'Aetna Better Health',
        'Anthem BCBS',
        'Fidelis',
        'Empire',
        'SWHNY',
        'Molina Home Health',
    }


def _walk_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk_widgets(child)


def _find_button(root, text):
    for widget in _walk_widgets(root):
        if isinstance(widget, tk.Button) and widget.cget('text') == text:
            return widget
    return None


def _find_label(root, text):
    for widget in _walk_widgets(root):
        if isinstance(widget, tk.Label) and widget.cget('text') == text:
            return widget
    return None


def _find_check(root, text):
    for widget in _walk_widgets(root):
        if isinstance(widget, tk.Checkbutton) and widget.cget('text') == text:
            return widget
    return None


def test_settings_panel_has_org_and_payer_fields(tmp_path):
    set_settings_path_for_tests(str(tmp_path / 'settings.json'))
    root = create_gui()
    try:
        root.update_idletasks()
        assert _find_label(root, 'OrgID') is not None
        assert _find_label(root, 'Payer ID') is not None
        assert _find_label(root, 'Default Provider NPI') is not None
        assert _find_label(root, 'Remit check-date grace days') is not None
        assert _find_check(root, 'Skip remittance when check and paid are --') is not None
        assert _find_check(root, 'Reuse search across sibling rows') is not None
        assert _find_check(root, 'Group search by patient (90-day visit window)') is not None
        assert _find_check(root, 'Format Excel output') is not None
        assert _find_check(root, 'Uppercase Excel cell text') is not None
        assert not state.uppercase_export_var.get()
        save_btn = _find_button(root, 'Save')
        clear_btn = _find_button(root, 'Clear')
        assert save_btn is not None
        assert clear_btn is not None
        assert _find_button(root, 'Open folder') is not None

        default_org, default_payer = default_org_payer_ids('Healthfirst')
        assert state.payer_var.get() == 'Healthfirst'
        assert state.org_id_var.get() == default_org
        assert state.payer_id_var.get() == default_payer
        assert state.settings_flag_vars['uses_hipaa_tab'].get()
        assert state.settings_flag_vars['uses_remittance'].get()
        assert state.settings_flag_vars['remittance_when_remarks_blank'].get()
        assert not state.settings_flag_vars['skip_remittance_when_unposted'].get()
        assert not state.settings_flag_vars['group_search_by_patient'].get()
        assert state.selector_profile_var.get() == 'mui'
        assert state.npi_blank_var.get()
        assert state.payer_npi_entry.cget('state') == 'disabled'

        state.uppercase_export_var.set(True)
        state.org_id_var.set('99999')
        state.payer_id_var.set('NEWPAY')
        save_btn.invoke()
        root.update_idletasks()
        assert load_payer_ids('Healthfirst') == {'org_id': '99999', 'payer_id': 'NEWPAY'}
        assert 'Saved for Healthfirst' in state.settings_status_var.get()
        assert get_uppercase_export_enabled() is True

        clear_btn.invoke()
        root.update_idletasks()
        assert load_payer_ids('Healthfirst') == {}
        assert state.org_id_var.get() == default_org
        assert state.payer_id_var.get() == default_payer
        assert 'Restored Healthfirst defaults' in state.settings_status_var.get()

        state.payer_var.set('Aetna Better Health')
        settings_btn = _find_settings_button(root)
        assert settings_btn is not None
        settings_btn.invoke()
        root.update_idletasks()
        assert state.selector_profile_var.get() == 'aetna'
        assert state.settings_flag_vars['uses_remittance'].get()
        assert not state.settings_flag_vars['remittance_when_remarks_blank'].get()
        assert not state.settings_flag_vars['skip_remittance_when_unposted'].get()
        assert state.settings_flag_vars['match_results_by_visit_and_amount'].get()

        state.payer_var.set('Villagecaremax')
        settings_btn.invoke()
        settings_btn.invoke()
        root.update_idletasks()
        assert state.settings_flag_vars['uses_remittance'].get()
        assert not state.settings_flag_vars['remittance_when_remarks_blank'].get()
        assert state.settings_flag_vars['skip_remittance_when_unposted'].get()
        assert state.settings_flag_vars['group_search_by_patient'].get()
    finally:
        root.destroy()
        set_settings_path_for_tests(None)


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
