"""HIPAA Codes denial lookup for Integra, Anthem BCBS, and Empire."""
from unittest.mock import MagicMock, patch

from availity_app.automation import (
    _code_tokens_from_text,
    _codes_table_row_selector,
    _empty_claim_result_for_payer,
    _expanded_remark_is_placeholder,
    _extract_code_pairs_from_row,
    _format_denial_from_code_pairs,
    _include_denial_code,
    _is_denied_charge_or_noncovered_status,
    _is_explicit_remark_placeholder,
    _is_hipaa_codes_header,
    _payer_denial_description_only,
    _payer_reads_hipaa_codes,
    _payer_trims_denial_first_sentence,
    _trim_denial_to_first_sentence,
    _usable_code_pairs,
    _usable_remark_text,
    _wait_for_remark_codes_panel,
    enrich_claim_with_denial,
    extract_denial_codes_inline,
    extract_line_level_denial_reason,
    parse_hipaa_status_code,
    select_hipaa_raw_for_denial,
)
from availity_app.constants import (
    CLAIM_NOT_FOUND_DENIAL_REASON,
    CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS,
    CLAIM_NOT_FOUND_STATUS,
    NO_CLAIMS_FOUND_ADVISED_STATUS,
    FULLY_PAID_DENIAL_REASON,
    HIPAA_REMARK_FALLBACK_STATUS_DESC,
    HIPAA_REMARK_FALLBACK_STATUSES,
    INVALID_MEMBER_ID_STATUS,
    OUTPUT_COLUMNS,
    PAYER_CONFIG,
)
from availity_app.utils import empty_claim_result

HIPAA_101_STATUS_CODE, HIPAA_101_STATUS_DESC = HIPAA_REMARK_FALLBACK_STATUSES[1]


def test_output_columns_do_not_include_hipaa_codes():
    assert 'HIPAA Codes' not in OUTPUT_COLUMNS
    assert 'Denial Reason' in OUTPUT_COLUMNS


def test_empty_claim_result_has_no_hipaa_codes_column():
    result = empty_claim_result('Denied')
    assert 'HIPAA Codes' not in result
    assert result['Denial Reason'] == '--'


def test_payer_flags_only_integra_anthem_empire():
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Integra']) is True
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Anthem BCBS']) is True
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Empire']) is True
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Healthfirst']) is False
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Fidelis']) is False
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Villagecaremax']) is False
    assert _payer_reads_hipaa_codes(PAYER_CONFIG['Aetna Better Health']) is False
    assert _payer_trims_denial_first_sentence(PAYER_CONFIG['Anthem BCBS']) is True
    assert _payer_trims_denial_first_sentence(PAYER_CONFIG['Integra']) is False
    assert _payer_trims_denial_first_sentence(PAYER_CONFIG['Empire']) is False
    assert _payer_denial_description_only(PAYER_CONFIG['Healthfirst']) is True
    assert _include_denial_code(PAYER_CONFIG['Healthfirst']) is False
    assert _include_denial_code(PAYER_CONFIG['Integra']) is False
    assert _include_denial_code(PAYER_CONFIG['Fidelis']) is True
    assert _include_denial_code(PAYER_CONFIG['Aetna Better Health']) is True
    for name in ('Integra', 'Anthem BCBS', 'Empire'):
        cfg = PAYER_CONFIG[name]
        assert cfg['uses_remittance'] is True
        assert cfg['remittance_when_remarks_blank'] is True
        assert cfg['read_hipaa_codes'] is True
    assert PAYER_CONFIG['Fidelis']['uses_remittance'] is False


def test_hipaa_codes_header_matching():
    assert _is_hipaa_codes_header('HIPAA Codes')
    assert _is_hipaa_codes_header('HIPAA Code')
    assert _is_hipaa_codes_header('HIPPAA Codes')
    assert _is_hipaa_codes_header('HIPAA\nCodes')
    assert not _is_hipaa_codes_header('Reason/Remark Codes')
    assert not _is_hipaa_codes_header('Service Dates')
    assert not _is_hipaa_codes_header('Billed Amount')


def test_usable_remark_text_skips_blank_heading_and_amount():
    assert _usable_remark_text('Y3Z') == 'Y3Z'
    assert _usable_remark_text('G18, N59') == 'G18, N59'
    assert _usable_remark_text('--') == ''
    assert _usable_remark_text('Reason/Remark Codes') == ''
    assert _usable_remark_text('  ') == ''
    assert _usable_remark_text('$0.00') == ''
    assert _usable_remark_text('$42.24') == ''
    assert _usable_remark_text('$118.24') == ''
    assert _usable_remark_text('Ineligible') == ''
    assert _usable_remark_text('Deductible') == ''
    assert _usable_remark_text('Copay') == ''
    assert _usable_remark_text('256, G18') == '256, G18'
    assert _usable_remark_text('Reason/Remark Codes 256, G18') == '256, G18'
    assert _usable_remark_text('Ineligible 256, G18') == '256, G18'
    assert _usable_remark_text('Reason/Remark Codes\n\n--') == ''
    assert _usable_remark_text('M150\nReason/Remark Codes\n--') == ''
    assert _usable_remark_text('M6281\nReason/Remark Codes\n256, G18') == '256, G18'
    assert _usable_remark_text('R6889\nReason/Remark Codes\n--') == ''


def test_explicit_remark_placeholder_requires_visible_dashes():
    assert _is_explicit_remark_placeholder('--') is True
    assert _is_explicit_remark_placeholder(' -- ') is True
    assert _is_explicit_remark_placeholder('N/A') is True
    assert _is_explicit_remark_placeholder('') is False
    assert _is_explicit_remark_placeholder(None) is False
    assert _is_explicit_remark_placeholder('N59') is False


def test_expanded_remark_detects_placeholder_after_heading():
    matching_row = MagicMock()
    sibling = MagicMock()
    sibling.count.return_value = 1
    matching_row.locator.return_value = sibling

    sibling.first.inner_text.return_value = 'Reason/Remark Codes\n\n--'
    assert _expanded_remark_is_placeholder(matching_row) is True

    sibling.first.inner_text.return_value = 'Reason/Remark Codes\n\nN59'
    assert _expanded_remark_is_placeholder(matching_row) is False

    sibling.first.inner_text.return_value = ''
    assert _expanded_remark_is_placeholder(matching_row) is False


@patch('availity_app.automation._remark_is_placeholder_now', return_value=True)
def test_wait_for_remark_panel_stops_when_placeholder_visible(mock_placeholder):
    matching_row = MagicMock()
    iframe = MagicMock()

    result = _wait_for_remark_codes_panel(
        iframe, matching_row, 'remark-sel', PAYER_CONFIG['Healthfirst'],
    )

    assert result == 'placeholder'
    matching_row.locator.assert_not_called()
    iframe.locator.assert_not_called()
    mock_placeholder.assert_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._line_expand_button_locator')
@patch('availity_app.automation._read_remark_codes')
@patch('availity_app.automation._wait_for_remark_codes_panel')
@patch('availity_app.automation._collapsed_remark_raw')
def test_extract_skips_wait_when_collapsed_remark_is_placeholder(
    mock_collapsed, mock_wait, mock_read, mock_expand_btn, mock_log,
):
    mock_collapsed.return_value = '--'
    expand_btn = MagicMock()
    mock_expand_btn.return_value = expand_btn
    matching_row = MagicMock()
    iframe = MagicMock()

    line_level_payers = [
        name for name, cfg in PAYER_CONFIG.items() if cfg.get('uses_line_level')
    ]
    assert line_level_payers
    for name in line_level_payers:
        expand_btn.reset_mock()
        mock_wait.reset_mock()
        mock_read.reset_mock()
        result = _extract_code_pairs_from_row(
            iframe, matching_row, PAYER_CONFIG[name],
        )
        assert result == []
        expand_btn.click.assert_not_called()
        mock_wait.assert_not_called()
        mock_read.assert_not_called()
    mock_log.assert_any_call(
        "    ℹ️ Reason/Remark is -- — skipping wait\n",
        "info",
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._line_expand_button_locator')
@patch('availity_app.automation._read_remark_codes')
@patch('availity_app.automation._wait_for_remark_codes_panel')
@patch('availity_app.automation._collapsed_remark_raw')
def test_extract_skips_remaining_wait_when_expanded_remark_is_placeholder(
    mock_collapsed, mock_wait, mock_read, mock_expand_btn, mock_log,
):
    mock_collapsed.return_value = None
    mock_wait.return_value = 'placeholder'
    expand_btn = MagicMock()
    mock_expand_btn.return_value = expand_btn

    result = _extract_code_pairs_from_row(
        MagicMock(), MagicMock(), PAYER_CONFIG['Fidelis'],
    )

    assert result == []
    assert expand_btn.click.call_count >= 1
    mock_wait.assert_called_once()
    mock_read.assert_not_called()
    mock_log.assert_any_call(
        "    ℹ️ Reason/Remark is -- — skipping wait\n",
        "info",
    )


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._line_expand_button_locator')
@patch('availity_app.automation._read_remark_codes')
def test_extract_code_pairs_reads_once_when_remark_empty(
    mock_read, mock_expand_btn, mock_log,
):
    mock_read.return_value = '--'
    mock_expand_btn.return_value = MagicMock()
    matching_row = MagicMock()
    iframe = MagicMock()

    result = _extract_code_pairs_from_row(
        iframe, matching_row, PAYER_CONFIG['Healthfirst'],
    )

    assert result == []
    assert mock_read.call_count == 1
    mock_log.assert_any_call("    ⚠️ Remark codes panel not found\n", "error")


def test_select_hipaa_raw_prefers_585_or_101():
    assert select_hipaa_raw_for_denial(['F2:107', 'F2:585']) == 'F2:585'
    assert select_hipaa_raw_for_denial(['F2:21', 'F3:101']) == 'F3:101'
    assert select_hipaa_raw_for_denial(['F2:585', 'F2:107']) == 'F2:585'
    assert select_hipaa_raw_for_denial(['F3:101']) == 'F3:101'
    assert select_hipaa_raw_for_denial(['F2:21']) == 'F2:21'
    assert select_hipaa_raw_for_denial([]) == ''
    assert select_hipaa_raw_for_denial(['--', 'F2:585']) == 'F2:585'


def test_parse_hipaa_status_code_splits_prefix():
    assert parse_hipaa_status_code('F2:585') == '585'
    assert parse_hipaa_status_code('F2:107') == '107'
    assert parse_hipaa_status_code('585') == '585'
    assert parse_hipaa_status_code('  F2:585  ') == '585'
    assert parse_hipaa_status_code('') == ''
    assert parse_hipaa_status_code('--') == ''


def test_status_codes_table_selector_is_exact():
    selector = _codes_table_row_selector('585', 'Status')
    assert 'td:text-is("Status")' in selector
    assert 'td:text-is("585")' in selector
    assert 'Remark' not in selector


def test_remark_codes_table_selector_unchanged():
    selector = _codes_table_row_selector('CO-45', 'Remark')
    assert 'td:text("Remark")' in selector
    assert 'td:text-is("CO-45")' in selector


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_anthem_585_entity_not_eligible_expands_remarks(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    matching_row = MagicMock()
    mock_collect.return_value = [
        {
            'row': matching_row,
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [('585', 'Entity not eligible')]
    mock_expand.return_value = [
        ('N59', 'Service not payable per managed care contract'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == 'Service not payable per managed care contract'
    mock_expand.assert_called_once()
    assert mock_expand.call_args.args[1] is matching_row
    mock_lookup.assert_called_once()
    args, kwargs = mock_lookup.call_args
    assert args[1] == '585'
    assert kwargs.get('code_type') == 'Status'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_585_uses_status_when_remark_panel_blank(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [('585', 'Entity not eligible')]
    mock_expand.return_value = []

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == 'Entity not eligible'
    mock_expand.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_empire_585_entity_not_eligible_expands_remarks(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$133.76',
            'paid': '$0.00',
            'date_range': '04/14/2026-04/14/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [('585', 'Entity not eligible')]
    mock_expand.return_value = [
        ('N59', 'Service not payable per managed care contract'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Empire'],
        visit_date='04/14/2026',
    )

    assert result == 'Service not payable per managed care contract'
    mock_expand.assert_called_once()
    assert mock_lookup.call_args.args[1] == '585'
    assert mock_lookup.call_args.kwargs.get('code_type') == 'Status'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_healthfirst_still_expands_remark_codes(
    mock_collect, mock_expand, mock_log,
):
    mock_collect.return_value = [
        {
            'row': 'row0',
            'idx': 0,
            'billed': '$100.00',
            'paid': '$0.00',
            'date_range': '01/15/2026-01/15/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_expand.return_value = [('CO-45', 'Charge exceeds fee schedule')]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Healthfirst'],
        visit_date='01/15/2026',
    )

    assert result == 'Charge exceeds fee schedule'
    mock_expand.assert_called_once()


def test_format_denial_description_only_strips_code():
    lines = {'_single': [('585', 'Denied Charge or Non-covered Charge')]}
    assert (
        _format_denial_from_code_pairs(lines, include_code=True)
        == '[585] - Denied Charge or Non-covered Charge'
    )
    assert (
        _format_denial_from_code_pairs(lines, include_code=False)
        == 'Denied Charge or Non-covered Charge'
    )


def test_format_denial_description_only_multiple_codes():
    lines = {'_single': [('N59', 'Reason A'), ('CO-45', 'Reason B')]}
    assert (
        _format_denial_from_code_pairs(lines, include_code=False)
        == 'Reason A, Reason B'
    )


def test_code_tokens_skip_blank_and_placeholder():
    assert _code_tokens_from_text('N59, --') == ['N59']
    assert _code_tokens_from_text('--, CO-45') == ['CO-45']
    assert _code_tokens_from_text('--') == []
    assert _code_tokens_from_text('N59\n--') == ['N59']
    assert _code_tokens_from_text('') == []
    assert _code_tokens_from_text('$0.00') == []
    assert _code_tokens_from_text('$42.24') == []
    assert _code_tokens_from_text('Ineligible') == []
    assert _code_tokens_from_text('Deductible') == []
    assert _code_tokens_from_text('256, G18') == ['256', 'G18']


def test_usable_code_pairs_drop_placeholder():
    assert _usable_code_pairs([
        ('--', '--'),
        ('N59', 'Service not payable per managed care contract'),
        ('', ''),
    ]) == [('N59', 'Service not payable per managed care contract')]
    assert _usable_code_pairs([('--', '--')]) == []


def test_format_denial_ignores_missing_remark_codes():
    lines = {
        '_single': [
            ('--', '--'),
            ('N59', 'Service not payable per managed care contract'),
        ],
    }
    assert (
        _format_denial_from_code_pairs(lines, include_code=False)
        == 'Service not payable per managed care contract'
    )
    assert (
        _format_denial_from_code_pairs(lines, include_code=True)
        == '[N59] - Service not payable per managed care contract'
    )
    assert _format_denial_from_code_pairs(
        {'_single': [('--', '--')]}, include_code=False,
    ) == '--'


def test_denied_charge_status_match_is_normalized():
    assert _is_denied_charge_or_noncovered_status([
        ('585', HIPAA_REMARK_FALLBACK_STATUS_DESC),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('585', '  denied charge or non-covered charge  '),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('585', 'Entity not eligible'),
    ])
    assert not _is_denied_charge_or_noncovered_status([
        ('107', HIPAA_REMARK_FALLBACK_STATUS_DESC),
    ])


def test_101_adjustment_status_match_is_normalized():
    assert _is_denied_charge_or_noncovered_status([
        (HIPAA_101_STATUS_CODE, HIPAA_101_STATUS_DESC),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('101', '  CLAIM WAS PROCESSED AS ADJUSTMENT TO PREVIOUS CLAIM  '),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('101', 'Claim was processed as adjustment to previous claim.'),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('101', 'Claim previously processed'),
    ])
    assert _is_denied_charge_or_noncovered_status([
        ('585', HIPAA_101_STATUS_DESC),
    ])


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_anthem_585_denied_charge_expands_remark_codes(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    matching_row = MagicMock()
    mock_collect.return_value = [
        {
            'row': matching_row,
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [
        ('N59', 'Service not payable per managed care contract'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == 'Service not payable per managed care contract'
    mock_expand.assert_called_once()
    assert mock_expand.call_args.args[1] is matching_row
    mock_lookup.assert_called_once()
    assert mock_lookup.call_args.kwargs.get('code_type') == 'Status'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_585_keeps_status_description_when_remarks_missing(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = []

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Integra'],
        visit_date='04/13/2026',
    )

    assert result == 'Denied Charge or Non-covered Charge'
    mock_expand.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_anthem_101_adjustment_expands_remark_codes(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    matching_row = MagicMock()
    mock_collect.return_value = [
        {
            'row': matching_row,
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:101',
        },
    ]
    mock_lookup.return_value = [
        (HIPAA_101_STATUS_CODE, HIPAA_101_STATUS_DESC + '.'),
    ]
    mock_expand.return_value = [
        ('N59', 'Service not payable per managed care contract'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == 'Service not payable per managed care contract'
    mock_expand.assert_called_once()
    assert mock_expand.call_args.args[1] is matching_row
    mock_lookup.assert_called_once()
    assert mock_lookup.call_args.args[1] == '101'
    assert mock_lookup.call_args.kwargs.get('code_type') == 'Status'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_101_keeps_status_description_when_remarks_missing(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:101',
        },
    ]
    mock_lookup.return_value = [
        (HIPAA_101_STATUS_CODE, HIPAA_101_STATUS_DESC),
    ]
    mock_expand.return_value = []

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Integra'],
        visit_date='04/13/2026',
    )

    assert result == HIPAA_101_STATUS_DESC
    mock_expand.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_101_expands_regardless_of_status_description(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:101',
        },
    ]
    mock_lookup.return_value = [
        ('101', 'Claim previously processed'),
    ]
    mock_expand.return_value = [
        ('Y3Z', 'This was not paid because a prior authorization was required.'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Empire'],
        visit_date='04/13/2026',
    )

    assert result == 'This was not paid because a prior authorization was required.'
    mock_expand.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation._first_hipaa_code_from_cell')
@patch('availity_app.automation._resolve_line_table_columns')
def test_inline_bundle_101_uses_remark_description(
    mock_cols, mock_first_code, mock_expand, mock_lookup, mock_log,
):
    mock_cols.return_value = {'hipaa_codes': 4}
    mock_first_code.return_value = 'F2:101'
    mock_lookup.return_value = [
        (HIPAA_101_STATUS_CODE, HIPAA_101_STATUS_DESC),
    ]
    mock_expand.return_value = [('N59', 'Not covered under plan')]

    result = extract_denial_codes_inline(
        MagicMock(), MagicMock(), 0, PAYER_CONFIG['Integra'],
    )

    assert result == 'Not covered under plan'
    mock_expand.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_anthem_585_ignores_placeholder_remark_and_keeps_available(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [
        ('--', '--'),
        ('N59', 'Service not payable per managed care contract'),
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == 'Service not payable per managed care contract'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_585_placeholder_remarks_keep_status_description(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [('--', '--')]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Integra'],
        visit_date='04/13/2026',
    )

    assert result == 'Denied Charge or Non-covered Charge'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_hipaa_fully_paid_line_sets_fully_paid(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$100.00',
            'paid': '$100.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'PAID', PAYER_CONFIG['Empire'],
        visit_date='04/13/2026',
    )

    assert result == FULLY_PAID_DENIAL_REASON
    mock_expand.assert_not_called()
    mock_lookup.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
def test_enrich_hipaa_fully_paid_skips_extraction(mock_extract, mock_log):
    claim_data = {'Claim Status': 'PAID', 'Denial Reason': '--'}
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$100.00', '$100.00',
        MagicMock(), 0, PAYER_CONFIG['Integra'],
        row_data={'VisitDate': '01/01/2026'},
    )
    assert claim_data['Denial Reason'] == FULLY_PAID_DENIAL_REASON
    mock_extract.assert_not_called()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_enrich_integra_skips_remittance_when_hipaa_remarks_present(
    mock_remit, mock_line, mock_log,
):
    mock_line.return_value = 'Service not payable per managed care contract'
    claim_data = {
        'Claim ID': 'INT1',
        'Claim Status': 'DENIED',
        'Denial Reason': '--',
        'Check Date': '--',
    }
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$100.00', '$0.00',
        MagicMock(), 0, PAYER_CONFIG['Integra'],
        row_data={'VisitDate': '01/01/2026'},
    )
    mock_line.assert_called_once()
    mock_remit.assert_not_called()
    assert claim_data['Denial Reason'] == 'Service not payable per managed care contract'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
@patch('availity_app.automation.extract_denial_reason_villagecaremax')
def test_enrich_integra_opens_remittance_when_hipaa_blank(
    mock_remit, mock_line, mock_log,
):
    mock_line.return_value = '--'
    mock_remit.return_value = 'From remittance'
    claim_data = {
        'Claim ID': 'INT1',
        'Claim Status': 'DENIED',
        'Denial Reason': '--',
        'Check Date': '--',
    }
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$100.00', '$0.00',
        MagicMock(), 0, PAYER_CONFIG['Integra'],
        row_data={'VisitDate': '01/01/2026'},
    )
    mock_line.assert_called_once()
    mock_remit.assert_called_once()
    assert claim_data['Denial Reason'] == 'From remittance'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_hipaa_pending_does_not_set_fully_paid(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$100.00',
            'paid': '$100.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'PENDING', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == '--'
    mock_expand.assert_not_called()
    mock_lookup.assert_not_called()


def test_hipaa_claim_not_found_sets_denial_reason():
    result = _empty_claim_result_for_payer(
        CLAIM_NOT_FOUND_STATUS, PAYER_CONFIG['Anthem BCBS'],
    )
    assert result['Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert result['Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON


def test_claim_not_found_search_manually_sets_unified_status_and_denial():
    result = _empty_claim_result_for_payer(
        CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS, PAYER_CONFIG['Empire'],
    )
    assert result['Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert result['Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON


def test_no_claims_found_advised_sets_unified_status_and_denial():
    result = _empty_claim_result_for_payer(
        NO_CLAIMS_FOUND_ADVISED_STATUS, PAYER_CONFIG['Healthfirst'],
    )
    assert result['Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert result['Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON


def test_healthfirst_claim_not_found_sets_denial_reason():
    result = _empty_claim_result_for_payer(
        CLAIM_NOT_FOUND_STATUS, PAYER_CONFIG['Healthfirst'],
    )
    assert result['Claim Status'] == CLAIM_NOT_FOUND_STATUS
    assert result['Denial Reason'] == CLAIM_NOT_FOUND_DENIAL_REASON


def test_hipaa_invalid_member_id_keeps_blank_denial():
    result = _empty_claim_result_for_payer(
        INVALID_MEMBER_ID_STATUS, PAYER_CONFIG['Integra'],
    )
    assert result['Denial Reason'] == '--'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation._first_hipaa_code_from_cell')
@patch('availity_app.automation._resolve_line_table_columns')
def test_inline_bundle_585_uses_remark_description(
    mock_cols, mock_first_code, mock_expand, mock_lookup, mock_log,
):
    mock_cols.return_value = {'hipaa_codes': 4}
    mock_first_code.return_value = 'F2:585'
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [('N59', 'Not covered under plan')]

    result = extract_denial_codes_inline(
        MagicMock(), MagicMock(), 0, PAYER_CONFIG['Integra'],
    )

    assert result == 'Not covered under plan'
    mock_expand.assert_called_once()


_ANTHEM_LONG_DENIAL = (
    'This was not paid because it was submitted for services that are not '
    'allowed based on our contract. If you disagree with our decision, and '
    'have documeShow more...'
)
_ANTHEM_FIRST_SENTENCE = (
    'This was not paid because it was submitted for services that are not '
    'allowed based on our contract.'
)


def test_trim_denial_to_first_sentence_drops_show_more():
    assert _trim_denial_to_first_sentence(_ANTHEM_LONG_DENIAL) == (
        _ANTHEM_FIRST_SENTENCE
    )


def test_trim_denial_to_first_sentence_leaves_short_text():
    assert _trim_denial_to_first_sentence('Entity not eligible') == (
        'Entity not eligible'
    )
    assert _trim_denial_to_first_sentence('--') == '--'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_anthem_keeps_first_sentence_of_long_remark(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [('N59', _ANTHEM_LONG_DENIAL)]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Anthem BCBS'],
        visit_date='04/13/2026',
    )

    assert result == _ANTHEM_FIRST_SENTENCE


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_integra_keeps_full_long_remark(
    mock_collect, mock_expand, mock_lookup, mock_log,
):
    mock_collect.return_value = [
        {
            'row': MagicMock(),
            'idx': 0,
            'billed': '$140.80',
            'paid': '$0.00',
            'date_range': '04/13/2026-04/13/2026',
            'hipaa_codes': 'F2:585',
        },
    ]
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [('N59', _ANTHEM_LONG_DENIAL)]

    result = extract_line_level_denial_reason(
        MagicMock(), 'DENIED', PAYER_CONFIG['Integra'],
        visit_date='04/13/2026',
    )

    assert result == _ANTHEM_LONG_DENIAL


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._lookup_code_pairs')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation._first_hipaa_code_from_cell')
@patch('availity_app.automation._resolve_line_table_columns')
def test_anthem_inline_bundle_trims_first_sentence(
    mock_cols, mock_first_code, mock_expand, mock_lookup, mock_log,
):
    mock_cols.return_value = {'hipaa_codes': 4}
    mock_first_code.return_value = 'F2:585'
    mock_lookup.return_value = [
        ('585', 'Denied Charge or Non-covered Charge'),
    ]
    mock_expand.return_value = [('N59', _ANTHEM_LONG_DENIAL)]

    result = extract_denial_codes_inline(
        MagicMock(), MagicMock(), 0, PAYER_CONFIG['Anthem BCBS'],
    )

    assert result == _ANTHEM_FIRST_SENTENCE
