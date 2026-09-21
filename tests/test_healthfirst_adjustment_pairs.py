"""Healthfirst original/adjustment line-pair extraction."""
from unittest.mock import MagicMock, patch

from availity_app.automation import (
    _canned_paid_denial_reason,
    _payer_uses_canned_paid_denial,
    _should_extract_line_denial,
    derive_claim_status,
    enrich_claim_with_denial,
    extract_line_level_denial_reason,
    harvest_open_claim_snapshot,
    should_extract_denial,
)
from availity_app.claim_snapshots import make_claim_snapshot, materialize_claim_for_visit
from availity_app.constants import (
    ADJUSTED_STATUS,
    FULLY_PAID_DENIAL_REASON,
    FULLY_PAID_OVERPAYMENT_DENIAL_REASON,
    PAID_OVERPAYMENT_STATUS,
    PAYER_CONFIG,
)
from availity_app.utils import (
    aggregate_line_amounts_for_payer,
    classify_adjustment_pair_roles,
    first_adjustment_original_line,
    payer_detects_adjustment_pairs,
)

_SCREENSHOT_DATES = ('06/08/2026', '06/10/2026', '06/11/2026', '06/12/2026')
_HEALTHFIRST = PAYER_CONFIG['Healthfirst']
_AETNA = PAYER_CONFIG['Aetna Better Health']
_FIDELIS = PAYER_CONFIG['Fidelis']


def _line(date, billed, paid, idx=0, row='row'):
    return {
        'row': row,
        'idx': idx,
        'date_range': f'{date}-{date}',
        'billed': billed,
        'paid': paid,
        'hipaa_codes': '',
        'denial_pairs': [],
    }


def _screenshot_lines():
    """Eight-line Healthfirst table from the adjustment screenshot."""
    lines = []
    for idx, date in enumerate(_SCREENSHOT_DATES):
        lines.append(_line(date, '$129.24', '$129.24', idx=idx, row=f'orig{idx}'))
    for idx, date in enumerate(_SCREENSHOT_DATES):
        lines.append(
            _line(date, '$129.24', '-$129.24', idx=idx + 4, row=f'adj{idx}')
        )
    return lines


def test_healthfirst_detects_adjustment_pairs_flag():
    assert payer_detects_adjustment_pairs(_HEALTHFIRST) is True
    assert payer_detects_adjustment_pairs(_AETNA) is False
    assert payer_detects_adjustment_pairs(_FIDELIS) is False


def test_healthfirst_uses_canned_paid_denial():
    assert _payer_uses_canned_paid_denial(_HEALTHFIRST) is True
    assert _payer_uses_canned_paid_denial(_AETNA) is False
    assert _payer_uses_canned_paid_denial(PAYER_CONFIG['Integra']) is False
    assert _canned_paid_denial_reason('PAID', _HEALTHFIRST) == (
        FULLY_PAID_DENIAL_REASON
    )
    assert _canned_paid_denial_reason(PAID_OVERPAYMENT_STATUS, _HEALTHFIRST) == (
        FULLY_PAID_OVERPAYMENT_DENIAL_REASON
    )
    assert _canned_paid_denial_reason(ADJUSTED_STATUS, _HEALTHFIRST) is None
    assert _canned_paid_denial_reason('PAID', _AETNA) == FULLY_PAID_DENIAL_REASON


def test_screenshot_roles_first_positive_is_original():
    roles = classify_adjustment_pair_roles(_screenshot_lines())
    assert roles == [
        'original', 'original', 'original', 'original',
        'adjustment', 'adjustment', 'adjustment', 'adjustment',
    ]


def test_screenshot_amounts_original_billed_net_paid():
    june_8 = [
        line for line in _screenshot_lines()
        if line['date_range'] == '06/08/2026-06/08/2026'
    ]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        june_8, _HEALTHFIRST,
    )
    assert billed == '$129.24'
    assert paid == '$0.00'
    assert is_adjusted is True


def test_screenshot_does_not_double_billed_for_other_payers():
    june_8 = [
        line for line in _screenshot_lines()
        if line['date_range'] == '06/08/2026-06/08/2026'
    ]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(june_8, _AETNA)
    assert billed == '$258.48'
    assert paid == '$0.00'
    assert is_adjusted is False

    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(june_8, _FIDELIS)
    assert billed == '$258.48'
    assert paid == '$0.00'
    assert is_adjusted is False


def test_unequal_paid_magnitudes_still_pair():
    matches = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig'),
        _line('06/08/2026', '$129.24', '-$100.00', idx=1, row='adj'),
    ]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is True
    assert billed == '$129.24'
    assert paid == '$29.24'


def test_unpaired_fully_paid_is_not_adjusted():
    matches = [_line('06/08/2026', '$129.24', '$129.24')]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is False
    assert billed == '$129.24'
    assert paid == '$129.24'


def test_unpaired_zero_paid_is_not_adjusted():
    matches = [_line('06/08/2026', '$129.24', '$0.00')]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is False
    assert billed == '$129.24'
    assert paid == '$0.00'


def test_unpaired_negative_only_is_not_adjusted():
    matches = [_line('06/08/2026', '$129.24', '-$129.24')]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is False
    assert billed == '$129.24'
    assert paid == '-$129.24'


def test_same_date_two_billed_amounts_pairs_only_matching_group():
    matches = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig'),
        _line('06/08/2026', '$129.24', '-$129.24', idx=1, row='adj'),
        _line('06/08/2026', '$50.00', '$50.00', idx=2, row='other'),
    ]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is True
    assert billed == '$179.24'
    assert paid == '$50.00'


def test_multiple_positives_and_negatives_same_group():
    matches = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig1'),
        _line('06/08/2026', '$129.24', '$129.24', idx=1, row='orig2'),
        _line('06/08/2026', '$129.24', '-$129.24', idx=2, row='adj1'),
        _line('06/08/2026', '$129.24', '-$129.24', idx=3, row='adj2'),
    ]
    roles = classify_adjustment_pair_roles(matches)
    assert roles == ['original', None, 'adjustment', 'adjustment']
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
        matches, _HEALTHFIRST,
    )
    assert is_adjusted is True
    assert billed == '$129.24'
    assert paid == '$0.00'
    assert first_adjustment_original_line(matches, _HEALTHFIRST)['row'] == 'orig1'


@patch('availity_app.automation.log_to_gui')
def test_should_extract_original_even_when_fully_paid(mock_log):
    original = _line('06/08/2026', '$129.24', '$129.24', row='orig')
    adjustment = _line('06/08/2026', '$129.24', '-$129.24', row='adj')
    assert _should_extract_line_denial('PAID', original, 'original') is True
    assert _should_extract_line_denial('PAID', adjustment, 'adjustment') is False
    assert should_extract_denial(
        'PAID', original['billed'], original['paid'], original['row'],
    ) is False


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_extract_expands_original_not_adjustment(
    mock_collect, mock_expand, mock_log,
):
    mock_collect.return_value = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig'),
        _line('06/08/2026', '$129.24', '-$129.24', idx=1, row='adj'),
    ]
    mock_expand.return_value = [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]

    result = extract_line_level_denial_reason(
        MagicMock(), 'PAID', _HEALTHFIRST, visit_date='06/08/2026',
    )

    assert result == 'ADJUSTMENT TO ORIGINAL CLAIM'
    mock_expand.assert_called_once()
    assert mock_expand.call_args.args[1] == 'orig'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_extract_falls_back_to_adjustment_when_original_has_no_codes(
    mock_collect, mock_expand, mock_log,
):
    mock_collect.return_value = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig'),
        _line('06/08/2026', '$129.24', '-$129.24', idx=1, row='adj'),
    ]
    mock_expand.side_effect = [
        [],
        [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')],
    ]

    result = extract_line_level_denial_reason(
        MagicMock(), 'PAID', _HEALTHFIRST, visit_date='06/08/2026',
    )

    assert result == 'ADJUSTMENT TO ORIGINAL CLAIM'
    assert mock_expand.call_count == 2
    assert mock_expand.call_args_list[0].args[1] == 'orig'
    assert mock_expand.call_args_list[1].args[1] == 'adj'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation.collect_line_data_rows')
def test_unpaired_fully_paid_still_skips_expand(
    mock_collect, mock_expand, mock_log,
):
    mock_collect.return_value = [
        _line('06/08/2026', '$129.24', '$129.24', idx=0, row='orig'),
    ]
    result = extract_line_level_denial_reason(
        MagicMock(), 'PAID', _HEALTHFIRST, visit_date='06/08/2026',
    )
    assert result == '--'
    mock_expand.assert_not_called()


@patch('availity_app.automation.log_to_gui')
def test_materialize_paid_sets_fully_paid_denial(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[_line('06/08/2026', '$129.24', '$129.24')],
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim['Claim Status'] == 'PAID'
    assert claim['Denial Reason'] == FULLY_PAID_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
def test_materialize_overpayment_sets_fully_paid_overpayment_denial(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[_line('06/08/2026', '$100.00', '$120.00')],
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim['Claim Status'] == PAID_OVERPAYMENT_STATUS
    assert claim['Denial Reason'] == FULLY_PAID_OVERPAYMENT_DENIAL_REASON


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
def test_enrich_paid_fills_fully_paid_when_blank(mock_extract, mock_log):
    mock_extract.return_value = '--'
    claim_data = {'Claim Status': 'PAID', 'Denial Reason': '--'}
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$129.24', '$129.24',
        MagicMock(), 0, _HEALTHFIRST,
        row_data={'VisitDate': '06/08/2026'},
    )
    assert claim_data['Denial Reason'] == FULLY_PAID_DENIAL_REASON
    mock_extract.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
def test_enrich_overpayment_fills_when_blank(mock_extract, mock_log):
    mock_extract.return_value = '--'
    claim_data = {
        'Claim Status': PAID_OVERPAYMENT_STATUS,
        'Denial Reason': '--',
    }
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$100.00', '$120.00',
        MagicMock(), 0, _HEALTHFIRST,
        row_data={'VisitDate': '06/08/2026'},
    )
    assert claim_data['Denial Reason'] == FULLY_PAID_OVERPAYMENT_DENIAL_REASON
    mock_extract.assert_called_once()


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation.extract_line_level_denial_reason')
def test_enrich_paid_keeps_extracted_reason(mock_extract, mock_log):
    mock_extract.return_value = 'ADJUSTMENT TO ORIGINAL CLAIM'
    claim_data = {'Claim Status': 'PAID', 'Denial Reason': '--'}
    enrich_claim_with_denial(
        MagicMock(), MagicMock(), claim_data, '$129.24', '$129.24',
        MagicMock(), 0, _HEALTHFIRST,
        row_data={'VisitDate': '06/08/2026'},
    )
    assert claim_data['Denial Reason'] == 'ADJUSTMENT TO ORIGINAL CLAIM'


@patch('availity_app.automation.log_to_gui')
def test_materialize_screenshot_visit_is_adjusted(_log):
    lines = []
    for line in _screenshot_lines():
        serial = dict(line)
        serial['denial_pairs'] = (
            [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]
            if serial['paid'] == '$129.24'
            else []
        )
        lines.append(serial)
    snapshot = make_claim_snapshot(
        header={
            'Claim ID': '0206192661064',
            'Claim Status': 'PAID',
            'Billed Amount': '$1033.92',
            'Paid Amount': '$0.00',
        },
        lines=lines,
        fingerprint='c:0206192661064',
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim is not None
    assert claim['Billed Amount'] == '$129.24'
    assert claim['Paid Amount'] == '$0.00'
    assert claim['Claim Status'] == ADJUSTED_STATUS
    assert claim['Denial Reason'] == 'ADJUSTMENT TO ORIGINAL CLAIM'


@patch('availity_app.automation.log_to_gui')
def test_materialize_ignores_adjustment_denial_pairs(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[
            {
                **_line('06/08/2026', '$129.24', '$129.24', row='orig'),
                'denial_pairs': [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')],
            },
            {
                **_line('06/08/2026', '$129.24', '-$129.24', row='adj'),
                'denial_pairs': [('99', 'SHOULD NOT APPEAR')],
            },
        ],
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim['Denial Reason'] == 'ADJUSTMENT TO ORIGINAL CLAIM'
    assert '99' not in claim['Denial Reason']


@patch('availity_app.automation.log_to_gui')
def test_materialize_falls_back_to_adjustment_denial_pairs(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PAID'},
        lines=[
            {
                **_line('06/08/2026', '$129.24', '$129.24', row='orig'),
                'denial_pairs': [],
            },
            {
                **_line('06/08/2026', '$129.24', '-$129.24', row='adj'),
                'denial_pairs': [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')],
            },
        ],
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim['Denial Reason'] == 'ADJUSTMENT TO ORIGINAL CLAIM'
    assert claim['Claim Status'] == ADJUSTED_STATUS


@patch('availity_app.automation.log_to_gui')
def test_materialize_pending_does_not_become_adjusted(_log):
    snapshot = make_claim_snapshot(
        header={'Claim ID': 'C1', 'Claim Status': 'PENDING'},
        lines=[
            _line('06/08/2026', '$129.24', '$129.24'),
            _line('06/08/2026', '$129.24', '-$129.24'),
        ],
    )
    claim = materialize_claim_for_visit(snapshot, '06/08/2026', _HEALTHFIRST)
    assert claim['Claim Status'] == 'PENDING'
    assert claim['Billed Amount'] == '$129.24'
    assert claim['Paid Amount'] == '$0.00'


@patch('availity_app.automation.log_to_gui')
def test_aetna_zero_paid_still_denied(_log):
    matches = [
        _line('06/08/2026', '$129.24', '$129.24'),
        _line('06/08/2026', '$129.24', '-$129.24'),
    ]
    billed, paid, is_adjusted = aggregate_line_amounts_for_payer(matches, _AETNA)
    claim = {'Claim Status': 'PAID'}
    derive_claim_status(claim, billed, paid, _AETNA, is_adjusted=is_adjusted)
    assert is_adjusted is False
    assert billed == '$258.48'
    assert claim['Claim Status'] == 'Denied'


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_harvest_expands_four_originals_not_adjustments(
    mock_header, mock_scrape, mock_expand, mock_log,
):
    mock_header.return_value = {
        'Claim ID': '0206192661064',
        'Claim Status': 'PAID',
        'Billed Amount': '$1033.92',
        'Paid Amount': '$0.00',
    }
    mock_scrape.return_value = _screenshot_lines()
    mock_expand.return_value = [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]

    snapshot = harvest_open_claim_snapshot(
        MagicMock(), MagicMock(), _HEALTHFIRST,
    )

    assert mock_expand.call_count == 4
    expanded_rows = [call.args[1] for call in mock_expand.call_args_list]
    assert expanded_rows == ['orig0', 'orig1', 'orig2', 'orig3']
    assert len(snapshot['lines']) == 8
    originals = [line for line in snapshot['lines'] if line['paid'] == '$129.24']
    adjustments = [line for line in snapshot['lines'] if line['paid'] == '-$129.24']
    assert all(line['denial_pairs'] == [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]
               for line in originals)
    assert all(line['denial_pairs'] == [] for line in adjustments)


@patch('availity_app.automation.log_to_gui')
@patch('availity_app.automation._extract_code_pairs_from_row')
@patch('availity_app.automation._scrape_line_data_rows')
@patch('availity_app.automation.extract_claim_header')
def test_harvest_falls_back_to_adjustments_when_originals_have_no_codes(
    mock_header, mock_scrape, mock_expand, mock_log,
):
    mock_header.return_value = {
        'Claim ID': '0206192661064',
        'Claim Status': 'PAID',
        'Billed Amount': '$1033.92',
        'Paid Amount': '$0.00',
    }
    mock_scrape.return_value = _screenshot_lines()
    mock_expand.side_effect = (
        [[]] * 4 + [[('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]] * 4
    )

    snapshot = harvest_open_claim_snapshot(
        MagicMock(), MagicMock(), _HEALTHFIRST,
    )

    assert mock_expand.call_count == 8
    expanded_rows = [call.args[1] for call in mock_expand.call_args_list]
    assert expanded_rows == [
        'orig0', 'orig1', 'orig2', 'orig3',
        'adj0', 'adj1', 'adj2', 'adj3',
    ]
    adjustments = [line for line in snapshot['lines'] if line['paid'] == '-$129.24']
    originals = [line for line in snapshot['lines'] if line['paid'] == '$129.24']
    assert all(line['denial_pairs'] == [] for line in originals)
    assert all(
        line['denial_pairs'] == [('32', 'ADJUSTMENT TO ORIGINAL CLAIM')]
        for line in adjustments
    )
