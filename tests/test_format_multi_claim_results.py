"""Tests for multi-claim merge formatting, especially Denial Reason."""
from availity_app.automation import format_multi_claim_results


def _claim(**overrides):
    row = {
        'Claim ID': 'C1',
        'Billed Amount': '$100.00',
        'Paid Amount': '$0.00',
        'Claim Status': 'Denied',
        'Denial Reason': '--',
        'Finalized Date': '01/01/2025',
        'Check Number': 'CHK1',
        'Check Date': '01/02/2025',
    }
    row.update(overrides)
    return row


def test_single_claim_is_not_numbered():
    result = format_multi_claim_results([
        _claim(**{'Denial Reason': 'Reason A'}),
    ])
    assert result['Denial Reason'] == 'Reason A'
    assert result['Claim ID'] == 'C1'


def test_one_real_denial_among_blanks_is_numbered():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Denial Reason': 'Reason A'}),
        _claim(**{'Claim ID': 'C2', 'Denial Reason': '--'}),
        _claim(**{'Claim ID': 'C3', 'Denial Reason': ''}),
    ])
    assert result['Denial Reason'] == '1. Reason A\n2. --\n3. --'
    assert result['Claim ID'] == '1. C1\n2. C2\n3. C3'


def test_same_real_reason_with_blanks_is_numbered():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Denial Reason': 'Reason A'}),
        _claim(**{'Claim ID': 'C2', 'Denial Reason': '--'}),
        _claim(**{'Claim ID': 'C3', 'Denial Reason': 'Reason A'}),
    ])
    assert result['Denial Reason'] == '1. Reason A\n2. --\n3. Reason A'


def test_all_same_denial_reasons_are_numbered():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Denial Reason': 'Reason A'}),
        _claim(**{'Claim ID': 'C2', 'Denial Reason': 'Reason A'}),
    ])
    assert result['Denial Reason'] == '1. Reason A\n2. Reason A'
    assert result['Claim ID'] == '1. C1\n2. C2'


def test_all_blank_denial_reasons_are_numbered():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Denial Reason': '--'}),
        _claim(**{'Claim ID': 'C2', 'Denial Reason': '--'}),
    ])
    assert result['Denial Reason'] == '1. --\n2. --'


def test_different_denial_reasons_stay_numbered():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Denial Reason': 'Reason A'}),
        _claim(**{'Claim ID': 'C2', 'Denial Reason': '--'}),
        _claim(**{'Claim ID': 'C3', 'Denial Reason': 'Reason B'}),
    ])
    assert result['Denial Reason'] == '1. Reason A\n2. --\n3. Reason B'
    assert result['Claim ID'] == '1. C1\n2. C2\n3. C3'


def test_any_need_manual_review_claim_collapses_the_row():
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Claim Status': 'Denied', 'Denial Reason': 'CARC A'}),
        _claim(**{
            'Claim ID': '--',
            'Claim Status': 'Need Manual Review',
            'Denial Reason': '--',
            'Billed Amount': '--',
        }),
    ])
    assert result['Claim Status'] == 'Need Manual Review'
    assert result['Claim ID'] == '--'
    assert result['Billed Amount'] == '--'
    assert result['Denial Reason'] == '--'
    result = format_multi_claim_results([
        _claim(**{'Claim ID': 'C1', 'Claim Status': 'Denied', 'Denial Reason': 'CARC A'}),
        _claim(**{
            'Claim ID': '--',
            'Claim Status': 'Need Manual Review',
            'Denial Reason': '--',
            'Billed Amount': '--',
        }),
    ])
    assert result['Claim Status'] == 'Need Manual Review'
    assert result['Claim ID'] == '--'
    assert result['Billed Amount'] == '--'
    assert result['Denial Reason'] == '--'
