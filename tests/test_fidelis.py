"""Unit checks for Fidelis payer wiring."""
from unittest.mock import patch

import pandas as pd

from availity_app import state
from availity_app.automation import _is_informational_payer_alert, derive_claim_status
from availity_app.constants import (
    NPI,
    PAID_OVERPAYMENT_STATUS,
    PAYER_CONFIG,
    get_gui_payer_names,
    get_selector_profile,
    resolve_provider_npi,
)
from availity_app.search_grouping import supports_search_grouping
from availity_app.utils import resolve_invoice_search_dates, resolve_line_level_config


def _fidelis():
    return PAYER_CONFIG['Fidelis']


def test_fidelis_is_shown_in_gui():
    assert 'Fidelis' in get_gui_payer_names()
    assert _fidelis().get('gui_enabled', True) is True


def test_fidelis_config_flags():
    cfg = _fidelis()
    assert cfg['uses_hipaa_tab'] is True
    assert cfg['uses_line_level'] is True
    assert cfg['uses_remittance'] is False
    assert cfg['uses_results_finalized_date'] is False
    assert 'bundle_search_date_fallback' not in cfg
    assert cfg['org_id'] == '36045893'
    assert cfg['payer_id'] == '11315'
    assert '{orgId}' in cfg['url']
    assert '{payerId}' in cfg['url']


def test_fidelis_uses_default_mui_profile_and_npi():
    cfg = _fidelis()
    assert resolve_provider_npi(cfg) == NPI
    assert get_selector_profile(cfg) is get_selector_profile({})
    assert cfg.get('selector_profile', 'mui') == 'mui'


def test_fidelis_line_level_defaults():
    cfg = resolve_line_level_config(_fidelis())
    assert cfg['filter_by_visit_date'] is True
    assert cfg['match_mode'] == 'exact'
    assert cfg['fallback_to_sole_line'] is True


def test_fidelis_supports_search_grouping():
    assert supports_search_grouping(_fidelis()) is True


def test_fidelis_provider_list_alert_is_informational():
    previous = state.selected_payer_config
    state.selected_payer_config = _fidelis()
    try:
        assert _is_informational_payer_alert(
            'Please complete provider record in the provider list before searching.'
        )
        assert not _is_informational_payer_alert('Subscriber not found')
    finally:
        state.selected_payer_config = previous


def test_fidelis_invoice_aggregation_search_dates():
    df = pd.DataFrame([
        {
            'InvoiceNumber': 'FID-1',
            'VisitDate': '01/10/2025',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
        },
        {
            'InvoiceNumber': 'FID-1',
            'VisitDate': '01/20/2025',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
        },
    ])
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('01/10/2025', '01/20/2025', True)


@patch('availity_app.automation.log_to_gui')
def test_fidelis_line_level_status_derivation(mock_log):
    cfg = _fidelis()

    denied = {'Claim Status': 'PAID'}
    derive_claim_status(denied, '$100.00', '$0.00', cfg)
    assert denied['Claim Status'] == 'Denied'

    partial = {'Claim Status': 'PAID'}
    derive_claim_status(partial, '$100.00', '$40.00', cfg)
    assert partial['Claim Status'] == 'Partially Paid'

    over = {'Claim Status': 'PAID'}
    derive_claim_status(over, '$100.00', '$120.00', cfg)
    assert over['Claim Status'] == PAID_OVERPAYMENT_STATUS
