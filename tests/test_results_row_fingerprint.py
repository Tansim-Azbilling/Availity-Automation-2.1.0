"""Tests for #claimsTable multi-claim row identity fingerprints."""
from availity_app.utils import (
    build_results_row_fingerprint,
    normalize_results_cell_text,
)


def test_normalize_results_cell_text_collapses_whitespace():
    assert normalize_results_cell_text('  02/24/2026\n02/24/2026  ') == (
        '02/24/2026 02/24/2026'
    )
    assert normalize_results_cell_text(None) == ''
    assert normalize_results_cell_text('nan') == ''


def test_fingerprint_prefers_composite_claim_and_amounts():
    key_a = build_results_row_fingerprint(
        claim_number='0303052615418',
        billed='$0.00',
        paid='$0.00',
        service_dates='02/24/2026-02/24/2026',
    )
    key_b = build_results_row_fingerprint(
        claim_number='0207302612836',
        billed='$142.00',
        paid='$141.85',
        service_dates='02/24/2026-02/24/2026',
    )
    assert key_a.startswith('c:')
    assert key_a != key_b


def test_fingerprint_distinguishes_shared_claim_number_by_amounts():
    """Shared account/claim column must not collapse distinct amount rows."""
    key_a = build_results_row_fingerprint(
        claim_number='645436',
        billed='$0.00',
        paid='$0.00',
        service_dates='02/24/2026-02/24/2026',
    )
    key_b = build_results_row_fingerprint(
        claim_number='645436',
        billed='$142.00',
        paid='$141.85',
        service_dates='02/24/2026-02/24/2026',
    )
    assert key_a != key_b


def test_fingerprint_amount_only_when_claim_number_missing():
    key_a = build_results_row_fingerprint(
        billed='$0.00',
        paid='$0.00',
        service_dates='02/24/2026-02/24/2026',
    )
    key_b = build_results_row_fingerprint(
        billed='$142.00',
        paid='$141.85',
        service_dates='02/24/2026-02/24/2026',
    )
    assert key_a != key_b
    assert 'b:$0.00' in key_a
    assert 'b:$142.00' in key_b


def test_fingerprint_falls_back_to_row_text_then_idx():
    text_key = build_results_row_fingerprint(
        row_text='PAID  SP50914D  0303052615418  $0.00',
        idx=0,
    )
    other_text = build_results_row_fingerprint(
        row_text='PAID  SP50914D  0207302612836  $142.00',
        idx=1,
    )
    assert text_key.startswith('text:')
    assert text_key != other_text

    idx_key = build_results_row_fingerprint(idx=3)
    assert idx_key == 'idx:3'


def test_fingerprint_shared_claim_number_without_amounts_uses_row_text():
    key_a = build_results_row_fingerprint(
        claim_number='645436',
        service_dates='02/24/2026-02/24/2026',
        row_text='PAID MALDONADORAMOS 0303052615418 SP50914D 645436',
    )
    key_b = build_results_row_fingerprint(
        claim_number='645436',
        service_dates='02/24/2026-02/24/2026',
        row_text='PAID MALDONADORAMOS 0207302612836 SP50914D 645436',
    )
    assert '|t:' in key_a
    assert key_a != key_b


def test_fingerprint_ignores_finalized_date_collision_scenario():
    """Same invoice/service date still yields two keys when amounts differ.

    This is the regression for multi-claim selection that previously keyed
    only on Finalized Date and skipped the second same-day claim.
    """
    shared_service = '02/24/2026-02/24/2026'
    first = build_results_row_fingerprint(
        claim_number='0303052615418',
        billed='$0.00',
        paid='$0.00',
        service_dates=shared_service,
    )
    second = build_results_row_fingerprint(
        claim_number='0207302612836',
        billed='$142.00',
        paid='$141.85',
        service_dates=shared_service,
    )
    processed = {first}
    assert second not in processed
