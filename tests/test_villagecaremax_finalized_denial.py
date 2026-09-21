"""Tests for VillageCareMAX Effective Date priority and remittance denial format."""
from unittest.mock import MagicMock, patch

from availity_app import state
from availity_app.automation import (
    _apply_finalized_date_priority,
    _clear_remit_applied_filters,
    _ensure_remit_page,
    _first_usable_remit_reason,
    _format_remit_claim_adjustment,
    _open_remit_claim_details,
    _prefer_remit_description_cell,
    _reset_remit_search,
    _select_remit_row_index,
    _should_open_remittance,
    enrich_claim_with_denial,
    extract_denial_reason_villagecaremax,
    harvest_open_claim_snapshot,
    resolve_finalized_date,
)
from availity_app.constants import (
    IN_PROCESS_DENIAL_REASON,
    PAYER_CONFIG,
    REMIT_CHECK_DATE_GRACE_DAYS,
    REMIT_NOT_FOUND_DENIAL,
    REMIT_TABLE_COLUMNS,
    REMITTANCE_URLS,
    SELECTORS,
)


def test_resolve_finalized_date_prefers_effective():
    assert resolve_finalized_date(
        effective_date='7/9/2026',
        results_finalized_date='01/01/2025',
        check_date='02/02/2025',
    ) == '07/09/2026'


def test_resolve_finalized_date_falls_back_to_results():
    assert resolve_finalized_date(
        effective_date='--',
        results_finalized_date='1/15/2025',
        check_date='02/02/2025',
    ) == '01/15/2025'


def test_resolve_finalized_date_falls_back_to_check_date():
    assert resolve_finalized_date(
        effective_date='',
        results_finalized_date=None,
        check_date='2/2/2025',
    ) == '02/02/2025'


def test_resolve_finalized_date_all_empty():
    assert resolve_finalized_date(
        effective_date='--',
        results_finalized_date='N/A',
        check_date='',
    ) == '--'


@patch('availity_app.automation.log_to_gui')
def test_apply_finalized_date_priority_uses_effective(mock_log):
    claim_data = {
        '_effective_date': '07/09/2026',
        'Check Date': '01/01/2025',
        'Finalized Date': '--',
    }
    _apply_finalized_date_priority(
        claim_data, '03/03/2025', PAYER_CONFIG['Villagecaremax'],
    )
    assert claim_data['Finalized Date'] == '07/09/2026'
    assert '_effective_date' not in claim_data


@patch('availity_app.automation.log_to_gui')
def test_apply_finalized_date_priority_noop_without_flag(mock_log):
    claim_data = {
        '_effective_date': '07/09/2026',
        'Finalized Date': '01/01/2025',
    }
    _apply_finalized_date_priority(
        claim_data, '03/03/2025', PAYER_CONFIG['Healthfirst'],
    )
    assert claim_data['Finalized Date'] == '01/01/2025'
    assert claim_data['_effective_date'] == '07/09/2026'


def test_format_remit_claim_adjustment_strips_code_and_trims():
    assert _format_remit_claim_adjustment(
        '197 - Precertification/authorization/notification/pre-treatment absent.'
    ) == 'Precertification/authorization/notification/pre-treatment absent.'


def test_format_remit_claim_adjustment_stops_at_first_period():
    assert _format_remit_claim_adjustment(
        '197 - Precertification/authorization/notification/pre-treatment absent. '
        'Additional note here.'
    ) == 'Precertification/authorization/notification/pre-treatment absent.'


def test_format_remit_claim_adjustment_no_separator_keeps_first_sentence():
    assert _format_remit_claim_adjustment(
        'Charge exceeds fee schedule. Extra text.'
    ) == 'Charge exceeds fee schedule.'


def test_format_remit_claim_adjustment_empty():
    assert _format_remit_claim_adjustment('') == '--'
    assert _format_remit_claim_adjustment('--') == '--'
    assert _format_remit_claim_adjustment('N/A') == '--'


def test_prefer_remit_description_uses_second_nested_cell():
    assert _prefer_remit_description_cell(
        ['45', '45 - Charge exceeds fee schedule.'],
    ) == '45 - Charge exceeds fee schedule.'


def test_prefer_remit_description_uses_single_nested_cell():
    assert _prefer_remit_description_cell(
        ['Charge exceeds fee schedule/maximum allowable.'],
    ) == 'Charge exceeds fee schedule/maximum allowable.'


def test_prefer_remit_description_falls_back_to_td_text():
    assert _prefer_remit_description_cell(
        [], '45 - Charge exceeds fee schedule. View More',
    ) == '45 - Charge exceeds fee schedule.'


def test_first_usable_remit_reason_skips_blank_leading_rows():
    assert _first_usable_remit_reason(['', '--', 'N/A']) == '--'
    assert _first_usable_remit_reason([
        '',
        '--',
        '45 - Charge exceeds fee schedule/maximum allowable or contracted/legislated fee arrangement.',
    ]) == (
        'Charge exceeds fee schedule/maximum allowable or contracted/legislated '
        'fee arrangement.'
    )


_AETNA_REMIT_HEADER = (
    'Claim Adjustment Group Codes\n'
    'Claim Adjustment Code/Desc\n'
    'Adjusted Units\n'
    'Adj Amount'
)


def test_format_remit_skips_nested_adjustment_headers():
    assert _format_remit_claim_adjustment(_AETNA_REMIT_HEADER) == '--'
    assert _format_remit_claim_adjustment('Adj Amount') == '--'
    assert _format_remit_claim_adjustment('Claim Adjustment Code/Desc') == '--'


def test_format_remit_extracts_description_after_header_dump():
    assert _format_remit_claim_adjustment(
        _AETNA_REMIT_HEADER + '\nCO\n45 - Charge exceeds fee schedule.'
    ) == 'Charge exceeds fee schedule.'


def test_prefer_skips_header_cells_and_uses_code_desc():
    assert _prefer_remit_description_cell([
        'Claim Adjustment Group Codes',
        'Claim Adjustment Code/Desc',
        'Adjusted Units',
        'Adj Amount',
        'CO',
        '45 - Charge exceeds fee schedule.',
    ]) == '45 - Charge exceeds fee schedule.'


def test_prefer_uses_code_desc_not_group_code_description():
    assert _prefer_remit_description_cell([
        'CO - Contractual Obligation',
        '45 - Charge exceeds fee schedule.',
        '0',
        '40.00',
    ]) == '45 - Charge exceeds fee schedule.'
    assert _prefer_remit_description_cell([
        'Claim Adjustment Group Codes',
        'Claim Adjustment Code/Desc',
        'Adjusted Units',
        'Adj Amount',
        'PR - Patient Responsibility',
        '1 - Deductible Amount.',
        '0',
        '25.00',
    ]) == '1 - Deductible Amount.'


def test_format_remit_skips_group_code_and_uses_code_desc():
    assert _format_remit_claim_adjustment(
        _AETNA_REMIT_HEADER
        + '\nCO - Contractual Obligation\n'
        + '45 - Charge exceeds fee schedule.'
    ) == 'Charge exceeds fee schedule.'
    assert _format_remit_claim_adjustment(
        'CO - Contractual Obligation'
    ) == '--'
    assert _format_remit_claim_adjustment('CO') == '--'
    assert _format_remit_claim_adjustment('Contractual Obligation') == '--'


def test_first_usable_skips_header_then_uses_real_adjustment():
    assert _first_usable_remit_reason([
        _AETNA_REMIT_HEADER,
        'Adj Amount',
        '45 - Charge exceeds fee schedule.',
    ]) == 'Charge exceeds fee schedule.'


def test_first_usable_skips_group_code_then_uses_code_desc():
    assert _first_usable_remit_reason([
        'CO - Contractual Obligation',
        '45 - Charge exceeds fee schedule.',
    ]) == 'Charge exceeds fee schedule.'


def _base_claim(status='FINALIZED', check_number='CHK123', check_date='07/16/2026'):
    return {
        'Claim ID': 'C123',
        'Claim Status': status,
        'Check Number': check_number,
        'Check Date': check_date,
        'Denial Reason': '--',
    }


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_runs_for_finalized_with_check(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED', check_number='CHK123')
    mock_extract.return_value = 'N59'

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='$0.00',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_called_once_with(
        None, 'C123', '07/16/2026',
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )
    assert claim_data['Denial Reason'] == 'N59'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_skips_for_pending_even_with_check(mock_extract, mock_log):
    claim_data = _base_claim(status='PENDING', check_number='CHK123')

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='$0.00',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_not_called()
    assert claim_data['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_runs_without_check_number(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED', check_number='--')
    mock_extract.return_value = 'CO-16'

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='$0.00',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_called_once_with(
        None, 'C123', '07/16/2026',
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )
    assert claim_data['Denial Reason'] == 'CO-16'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_skips_when_check_and_paid_unposted(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED', check_number='--', check_date='--')
    claim_data['Paid Amount'] = '--'

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='--',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_not_called()
    assert claim_data['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_runs_when_unposted_skip_flag_off(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED', check_number='--', check_date='--')
    mock_extract.return_value = 'CO-16'
    payer = dict(PAYER_CONFIG['Villagecaremax'])
    payer['skip_remittance_when_unposted'] = False

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='--',
        matching_row=None,
        row_idx=-1,
        payer_config=payer,
    )

    mock_extract.assert_called_once_with(
        None, 'C123', '--', payer_config=payer,
    )
    assert claim_data['Denial Reason'] == 'CO-16'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_runs_when_check_blank_but_paid_posted(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED', check_number='--', check_date='--')
    mock_extract.return_value = 'CO-16'

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='$0.00',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_called_once_with(
        None, 'C123', '--',
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )
    assert claim_data['Denial Reason'] == 'CO-16'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._harvest_remit_viewer')
@patch('availity_app.automation.extract_claim_header')
def test_harvest_skips_remittance_when_check_and_paid_unposted(
    mock_header, mock_harvest, mock_log,
):
    mock_header.return_value = {
        'Claim ID': 'C123',
        'Claim Status': 'FINALIZED',
        'Billed Amount': '$100.00',
        'Paid Amount': '--',
        'Check Number': '--',
        'Check Date': '--',
    }

    snapshot = harvest_open_claim_snapshot(
        MagicMock(), MagicMock(), PAYER_CONFIG['Villagecaremax'],
    )

    mock_harvest.assert_not_called()
    assert snapshot['remittance_denial'] is None
    assert _should_open_remittance(
        PAYER_CONFIG['Villagecaremax'], 'C123',
        claim_data=mock_header.return_value,
    ) is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_remit_lookup_skips_without_claim_id(mock_extract, mock_log):
    claim_data = _base_claim(status='FINALIZED')
    claim_data['Claim ID'] = '--'

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$100.00',
        paid='$0.00',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Villagecaremax'],
    )

    mock_extract.assert_not_called()
    assert claim_data['Denial Reason'] == IN_PROCESS_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_aetna_partial_payment_runs_remit_lookup(mock_extract, mock_log):
    claim_data = _base_claim(status='PAID', check_number='EFT205006')
    claim_data['Claim ID'] = '25297Z0048849'
    mock_extract.return_value = (
        'Charge exceeds fee schedule/maximum allowable or '
        'contracted/legislated fee arrangement.'
    )

    enrich_claim_with_denial(
        page=None,
        iframe=None,
        claim_data=claim_data,
        billed='$106.40',
        paid='$66.50',
        matching_row=None,
        row_idx=-1,
        payer_config=PAYER_CONFIG['Aetna Better Health'],
        allow_line_fallback=False,
    )

    mock_extract.assert_called_once_with(
        None, '25297Z0048849', '07/16/2026',
        payer_config=PAYER_CONFIG['Aetna Better Health'],
    )
    assert claim_data['Denial Reason'] == (
        'Charge exceeds fee schedule/maximum allowable or '
        'contracted/legislated fee arrangement.'
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_healthfirst_skips_remit_when_remarks_present(
    mock_extract, mock_line, mock_log,
):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    mock_line.return_value = 'Charge exceeds fee schedule'
    claim_data = _base_claim(status='Partially Paid', check_number='--')

    enrich_claim_with_denial(
        page=None,
        iframe=MagicMock(),
        claim_data=claim_data,
        billed='$100.00',
        paid='$40.00',
        matching_row=MagicMock(),
        row_idx=0,
        payer_config=payer,
        row_data={'VisitDate': '01/01/2026'},
    )

    mock_extract.assert_not_called()
    mock_line.assert_called_once()
    assert claim_data['Denial Reason'] == 'Charge exceeds fee schedule'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_healthfirst_remit_runs_when_remarks_blank(
    mock_extract, mock_line, mock_log,
):
    payer = dict(PAYER_CONFIG['Healthfirst'])
    mock_line.return_value = '--'
    mock_extract.return_value = 'CO-16'
    claim_data = _base_claim(status='Partially Paid', check_number='--')

    enrich_claim_with_denial(
        page=None,
        iframe=MagicMock(),
        claim_data=claim_data,
        billed='$100.00',
        paid='$40.00',
        matching_row=MagicMock(),
        row_idx=0,
        payer_config=payer,
        row_data={'VisitDate': '01/01/2026'},
    )

    mock_line.assert_called_once()
    mock_extract.assert_called_once_with(
        None, 'C123', '07/16/2026', payer_config=payer,
    )
    assert claim_data['Denial Reason'] == 'CO-16'


# Snapshot of div[role="table"][aria-label="Remits"] with two remits for one claim.
_SAMPLE_REMIT_ROWS = [
    {'claim_id': '261700425200', 'check_date': '(07/16/2026)'},
    {'claim_id': '261700425200', 'check_date': '(08/13/2026)'},
]


def test_remit_table_column_map_includes_claim_and_check_hooks():
    assert REMIT_TABLE_COLUMNS['claim_number']['selector'] == 'a[id^="claimNumber"]'
    assert REMIT_TABLE_COLUMNS['check_number']['selector'] == 'a[id^="checkNumber"]'
    assert REMIT_TABLE_COLUMNS['check_date']['selector'] == 'a[id^="checkNumber"] + div'
    assert REMIT_TABLE_COLUMNS['patient_name']['selector'] == 'div[id^="patientNameId"]'


def test_select_remit_row_matches_claim_and_check_date():
    assert _select_remit_row_index(
        _SAMPLE_REMIT_ROWS, '261700425200', '08/13/2026',
    ) == 1
    assert _select_remit_row_index(
        _SAMPLE_REMIT_ROWS, '261700425200', '7/16/2026',
    ) == 0


def test_select_remit_row_does_not_fall_back_to_other_check_date():
    assert _select_remit_row_index(
        _SAMPLE_REMIT_ROWS, '261700425200', '01/01/2025',
    ) is None


def test_select_remit_row_unique_claim_without_check_date():
    rows = [{'claim_id': '261700425200', 'check_date': '(07/16/2026)'}]
    assert _select_remit_row_index(rows, '261700425200') == 0
    assert _select_remit_row_index(rows, '261700425200', '--') == 0
    mixed = [
        {'claim_id': '111', 'check_date': '(07/16/2026)'},
        {'claim_id': '261700425200', 'check_date': '(08/13/2026)'},
    ]
    assert _select_remit_row_index(mixed, '261700425200') == 1


def test_select_remit_row_ambiguous_claim_without_check_date():
    assert _select_remit_row_index(_SAMPLE_REMIT_ROWS, '261700425200') is None
    assert _select_remit_row_index(_SAMPLE_REMIT_ROWS, '261700425200', '--') is None


def test_select_remit_row_unknown_claim():
    assert _select_remit_row_index(
        _SAMPLE_REMIT_ROWS, '000000', '07/16/2026',
    ) is None


def test_select_remit_row_accepts_check_date_within_grace_window():
    rows = [
        {'claim_id': '261700425200', 'check_date': '(12/24/2025)'},
        {'claim_id': '261700425200', 'check_date': '(12/30/2025)'},
    ]
    assert _select_remit_row_index(rows, '261700425200', '12/23/2025') == 0
    assert _select_remit_row_index(
        [{'claim_id': '261700425200', 'check_date': '(12/30/2025)'}],
        '261700425200',
        '12/23/2025',
    ) == 0


def test_select_remit_row_rejects_check_date_beyond_grace_window():
    rows = [{'claim_id': '261700425200', 'check_date': '(12/31/2025)'}]
    assert _select_remit_row_index(rows, '261700425200', '12/23/2025') is None


def test_select_remit_row_rejects_check_date_before_claim_detail():
    rows = [{'claim_id': '261700425200', 'check_date': '(12/22/2025)'}]
    assert _select_remit_row_index(rows, '261700425200', '12/23/2025') is None


def test_select_remit_row_prefers_exact_then_closest_in_window():
    rows = [
        {'claim_id': '261700425200', 'check_date': '(12/28/2025)'},
        {'claim_id': '261700425200', 'check_date': '(12/23/2025)'},
        {'claim_id': '261700425200', 'check_date': '(12/24/2025)'},
    ]
    assert _select_remit_row_index(rows, '261700425200', '12/23/2025') == 1

    later_only = [
        {'claim_id': '261700425200', 'check_date': '(12/28/2025)'},
        {'claim_id': '261700425200', 'check_date': '(12/24/2025)'},
    ]
    assert _select_remit_row_index(
        later_only, '261700425200', '12/23/2025',
    ) == 1


def test_select_remit_row_grace_days_is_configurable():
    rows = [{'claim_id': '261700425200', 'check_date': '(12/26/2025)'}]
    assert _select_remit_row_index(
        rows, '261700425200', '12/23/2025', grace_days=3,
    ) == 0
    assert _select_remit_row_index(
        rows, '261700425200', '12/23/2025', grace_days=2,
    ) is None
    assert REMIT_CHECK_DATE_GRACE_DAYS == 7


def test_select_remit_row_grace_window_crosses_year():
    rows = [{'claim_id': '261700425200', 'check_date': '(01/03/2026)'}]
    assert _select_remit_row_index(rows, '261700425200', '12/28/2025') == 0


def test_remit_selector_keys_include_back_and_applied_filters():
    assert SELECTORS['remit_back_link'] == (
        'a.text-primary[href="#/?tab=claim"]:has-text("Back")'
    )
    assert SELECTORS['remit_applied_filters'] == (
        'div[role="group"][aria-label="Applied Search Terms"]'
    )
    assert SELECTORS['remit_remove_filter'] == 'button[aria-label="Remove Filter"]'


def _live_remit_page():
    remit_page = MagicMock()
    remit_page.is_closed.return_value = False
    return remit_page


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._prepare_remit_search_form')
def test_ensure_remit_page_reuses_live_tab(mock_prepare, _log):
    existing = _live_remit_page()
    claim_page = MagicMock()
    state.remit_page = existing
    try:
        result = _ensure_remit_page(claim_page)
        assert result is existing
        claim_page.context.new_page.assert_not_called()
        mock_prepare.assert_called_once_with(existing)
    finally:
        state.remit_page = None


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_remit_search_outcome', return_value=False)
@patch('availity_app.automation._click_matching_remit_claim', return_value=True)
@patch('availity_app.automation._prepare_remit_search_form')
@patch('availity_app.automation._ensure_remit_page')
def test_open_remit_does_not_open_new_page_when_tab_exists(
    mock_ensure, mock_prepare, _click, _wait, _log,
):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    search_input = MagicMock()
    search_btn = MagicMock()

    def _locator(selector):
        if selector == SELECTORS['remit_search_input']:
            return search_input
        if selector == SELECTORS['remit_search_btn']:
            return search_btn
        return MagicMock()

    iframe.locator.side_effect = _locator
    mock_ensure.return_value = remit_page
    mock_prepare.return_value = iframe

    claim_page = MagicMock()
    opened, opened_iframe = _open_remit_claim_details(
        claim_page, 'C123', '07/16/2026',
    )

    assert opened is remit_page
    assert opened_iframe is iframe
    mock_ensure.assert_called_once_with(claim_page)
    claim_page.context.new_page.assert_not_called()
    remit_page.close.assert_not_called()
    search_input.first.fill.assert_called_once_with('C123')
    remit_page.wait_for_url.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._wait_remit_search_outcome', return_value=True)
@patch('availity_app.automation._prepare_remit_search_form')
@patch('availity_app.automation._ensure_remit_page')
def test_open_remit_not_found_does_not_close_tab(
    mock_ensure, mock_prepare, _wait, _log,
):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    mock_ensure.return_value = remit_page
    mock_prepare.return_value = iframe

    opened, opened_iframe = _open_remit_claim_details(
        MagicMock(), 'C123', '07/16/2026',
    )

    assert opened is None
    assert opened_iframe is None
    remit_page.close.assert_not_called()
    remit_page.goto.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._reset_remit_search')
@patch(
    'availity_app.automation._read_remit_adjustment_rows',
    return_value=[{
        'billed': '--',
        'paid': '--',
        'quantity': '',
        'date_range': '',
        'denial': 'Denied.',
        'raw_denial': 'Denied.',
    }],
)
@patch('availity_app.automation._open_remit_claim_details')
def test_extract_success_resets_from_details_and_does_not_close(
    mock_open, _read_rows, mock_reset, _log,
):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    mock_open.return_value = (remit_page, iframe)
    state.remit_page = remit_page
    try:
        result = extract_denial_reason_villagecaremax(
            MagicMock(), 'C123', '07/16/2026',
        )
        assert result == 'Denied.'
        mock_reset.assert_called_once_with(remit_page, from_claim_details=True)
        remit_page.close.assert_not_called()
    finally:
        state.remit_page = None


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._reset_remit_search')
@patch('availity_app.automation._open_remit_claim_details', return_value=(None, None))
def test_extract_not_found_clears_without_back_and_does_not_close(
    _open, mock_reset, _log,
):
    remit_page = _live_remit_page()
    state.remit_page = remit_page
    try:
        result = extract_denial_reason_villagecaremax(
            MagicMock(), 'C123', '07/16/2026',
        )
        assert result == REMIT_NOT_FOUND_DENIAL
        mock_reset.assert_called_once_with(remit_page, from_claim_details=False)
        remit_page.close.assert_not_called()
    finally:
        state.remit_page = None


def test_clear_remit_applied_filters_clicks_until_gone():
    iframe = MagicMock()
    group = MagicMock()
    iframe.locator.return_value = group
    remove_btn = MagicMock()
    group.locator.return_value = remove_btn
    remove_btn.count.side_effect = [1, 0]
    btn = MagicMock()
    btn.is_visible.return_value = True
    remove_btn.first = btn

    _clear_remit_applied_filters(iframe)

    iframe.locator.assert_called_once_with(SELECTORS['remit_applied_filters'])
    group.locator.assert_called_once_with(SELECTORS['remit_remove_filter'])
    btn.click.assert_called_once()


@patch('availity_app.automation.log_to_gui')
def test_reset_from_details_clicks_back_then_clears(_log):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    remit_page.frame_locator.return_value = iframe
    back = MagicMock()
    search_input = MagicMock()
    group = MagicMock()
    remove_btn = MagicMock()
    remove_btn.count.return_value = 0
    group.locator.return_value = remove_btn

    def _locator(selector):
        if selector == SELECTORS['remit_back_link']:
            return back
        if selector == SELECTORS['remit_search_input']:
            return search_input
        if selector == SELECTORS['remit_applied_filters']:
            return group
        return MagicMock()

    iframe.locator.side_effect = _locator

    _reset_remit_search(remit_page, from_claim_details=True)

    back.first.click.assert_called_once()
    search_input.first.wait_for.assert_called_once()
    remit_page.close.assert_not_called()
    remit_page.goto.assert_not_called()


@patch('availity_app.automation.log_to_gui')
def test_reset_from_search_skips_back(_log):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    remit_page.frame_locator.return_value = iframe
    group = MagicMock()
    remove_btn = MagicMock()
    remove_btn.count.return_value = 0
    group.locator.return_value = remove_btn
    iframe.locator.return_value = group

    _reset_remit_search(remit_page, from_claim_details=False)

    iframe.locator.assert_called_with(SELECTORS['remit_applied_filters'])
    remit_page.close.assert_not_called()
    remit_page.goto.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._prepare_remit_search_form')
def test_reset_falls_back_to_home_and_keeps_tab_open(mock_prepare, _log):
    remit_page = _live_remit_page()
    iframe = MagicMock()
    remit_page.frame_locator.return_value = iframe
    back = MagicMock()
    back.first.wait_for.side_effect = TimeoutError('Back missing')
    iframe.locator.return_value = back

    _reset_remit_search(remit_page, from_claim_details=True)

    remit_page.goto.assert_called_once()
    assert remit_page.goto.call_args.args[0] == REMITTANCE_URLS['home']
    mock_prepare.assert_called_once_with(remit_page)
    remit_page.close.assert_not_called()
