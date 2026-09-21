"""Serializable claim-detail snapshots and VisitDate fan-out (no Playwright)."""
from availity_app.constants import (
    FULLY_PAID_DENIAL_REASON,
    FULLY_PAID_OVERPAYMENT_DENIAL_REASON,
    IN_PROCESS_DENIAL_REASON,
    NEED_MANUAL_REVIEW_STATUS,
    resolved_output_columns,
)
from availity_app.search_grouping import search_group_key
from availity_app.utils import (
    aggregate_line_amounts_for_payer,
    denial_entries_preferring_original,
    empty_claim_result,
    filter_line_rows_by_visit_date,
    get_sorted_invoice_rows,
    humanize_match_confidence,
    invoice_row_indices,
    invoice_service_dates_aligned,
    match_invoice_lines_to_inputs,
    MATCH_FLAG_MANUAL_REVIEW,
    parse_amount,
    resolve_line_level_config,
    safe_field,
    safe_invoice_number,
)

_HEADER_COPY_FIELDS = (
    'Claim ID', 'Billed Amount', 'Paid Amount', 'Claim Status',
    'Finalized Date', 'Check Number', 'Check Date',
)


def make_claim_snapshot(
    *,
    header,
    lines=None,
    remittance_denial=None,
    fingerprint='',
    results_finalized_date=None,
    harvest_complete=True,
    remit_join_unresolved=False,
):
    """Build a locator-free snapshot of one opened claim submission."""
    return {
        'header': dict(header or {}),
        'fingerprint': fingerprint or '',
        'results_finalized_date': results_finalized_date,
        'lines': list(lines or []),
        'remittance_denial': remittance_denial,
        'harvest_complete': harvest_complete,
        'remit_join_unresolved': remit_join_unresolved,
    }


def pending_invoice_sibling_indices(
    df, seed_index, should_process_fn=None, payer_config=None,
):
    """Pending spreadsheet rows that share invoice + search identity with seed.

    Always includes ``seed_index``. Other rows must pass ``should_process_fn``
    (typically ``row_should_process``) so Done / Skipped / Recheck=no are
    not overwritten. Matching requires the same InvoiceNumber and the same
    search-group key (member, name, DOB, aggregated service dates).
    """
    if df is None or seed_index is None:
        return []
    if seed_index < 0 or seed_index >= len(df):
        return []

    seed_invoice = safe_invoice_number(df.iloc[seed_index], 'InvoiceNumber')
    if not seed_invoice:
        return [seed_index]

    seed_key = search_group_key(df, seed_index, payer_config=payer_config)
    indices = [seed_index]
    for idx in invoice_row_indices(df, seed_invoice):
        if idx == seed_index:
            continue
        if should_process_fn is not None and not should_process_fn(df, idx):
            continue
        if search_group_key(df, idx, payer_config=payer_config) != seed_key:
            continue
        indices.append(idx)
    return indices


def uses_invoice_alignment(payer_config):
    """True when the payer matches whole invoices to portal lines at once."""
    return bool(
        payer_config
        and payer_config.get('uses_line_level')
        and resolve_line_level_config(payer_config).get('match_by_invoice_alignment')
    )


def attach_invoice_alignment(snapshots, df, invoice_number, payer_config):
    """Precompute a df-index to portal-line match map on each snapshot.

    Invoice-alignment payers (SWHNY) cannot pick a service line from VisitDate
    alone, so every spreadsheet row on the invoice is matched against every
    harvested line of a claim submission at once using (Units, Amount)
    signatures.  A no-op for every other payer.
    """
    if not uses_invoice_alignment(payer_config) or df is None:
        return
    sorted_inputs = get_sorted_invoice_rows(df, invoice_number)
    if not sorted_inputs:
        return
    cfg = resolve_line_level_config(payer_config)
    for snapshot in snapshots or []:
        lines = list(snapshot.get('lines') or [])
        if not lines:
            continue
        header = snapshot.get('header') or {}
        range_start = header.get('_service_date_start')
        range_end = header.get('_service_date_end')
        snapshot['_align_map'] = match_invoice_lines_to_inputs(
            sorted_inputs, lines,
            dates_aligned=invoice_service_dates_aligned(
                sorted_inputs, range_start, range_end,
            ),
            header_billed=parse_amount(header.get('Billed Amount')),
            header_paid=parse_amount(header.get('Paid Amount')),
            panel_range_start=range_start,
            panel_range_end=range_end,
            enable_pool_tiers=cfg.get('enable_pool_tiers', True),
            allow_split_tiebreak=cfg.get('allow_split_tiebreak', False),
            max_combo=int(cfg.get('max_combo', 3) or 3),
        )


def alignment_entry_for_row(snapshot, row_index):
    """Return this row's match entry from a snapshot's map, or None."""
    align_map = (snapshot or {}).get('_align_map')
    if not align_map or row_index is None:
        return None
    return align_map.get(row_index)


def _alignment_confidence(entry):
    """Confidence label for a match entry, with the note only when unsafe."""
    confidence = entry.get('confidence') or ''
    note = entry.get('note')
    if note and MATCH_FLAG_MANUAL_REVIEW in str(confidence):
        return f"{confidence}; {note}"
    return confidence


def need_manual_review_result(payer_config, note=''):
    """Output dict with Claim Status Need Manual Review and other fields ``--``."""
    claim = empty_claim_result(NEED_MANUAL_REVIEW_STATUS, payer_config=payer_config)
    confidence = MATCH_FLAG_MANUAL_REVIEW
    if note:
        confidence = f"{MATCH_FLAG_MANUAL_REVIEW}; {note}"
    if 'Match Confidence' in resolved_output_columns(payer_config):
        claim['Match Confidence'] = confidence
    return _finalize_output(claim, payer_config)


def unresolved_alignment_result(snapshots, row_index, payer_config):
    """Result dict flagging a row the matcher could not resolve, else None.

    A row is only flagged when at least one snapshot produced an entry for it,
    which distinguishes "matcher gave up" from "this claim isn't for this row".
    """
    entry = None
    for snapshot in snapshots or []:
        candidate = alignment_entry_for_row(snapshot, row_index)
        if candidate is not None:
            entry = candidate
            break
    if entry is None:
        return None
    note = entry.get('note') or ''
    return need_manual_review_result(payer_config, note)


_RELIABILITY_FIELDS = (
    '_field_states', '_field_errors', '_extraction_errors',
    '_denial_required', '_denial_state', '_denial_error',
)


def _copy_header_claim(header, status=None):
    """Return an output-column dict copied from a harvested header."""
    claim = empty_claim_result(status or header.get('Claim Status', '--'))
    for field in _HEADER_COPY_FIELDS:
        if field in header and header[field] not in (None, ''):
            claim[field] = header[field]
    if header.get('_effective_date'):
        claim['_effective_date'] = header['_effective_date']
    if header.get('_effective_date_read') is not None:
        claim['_effective_date_read'] = header['_effective_date_read']
    for field in _RELIABILITY_FIELDS:
        if field in header:
            value = header[field]
            claim[field] = dict(value) if isinstance(value, dict) else value
    return claim


def _finalize_output(claim, payer_config):
    """Project a claim onto its output columns, keeping reliability metadata.

    Validation runs on this dict before it is written, and
    ``format_multi_claim_results`` drops the underscore keys on the way out.
    """
    result = {field: claim.get(field, '--')
              for field in resolved_output_columns(payer_config)
              if field not in ('Last Action Taken', 'Recheck')}
    if (payer_config or {}).get('result_validation'):
        for field in _RELIABILITY_FIELDS:
            if field in claim:
                result[field] = claim[field]
    return result


def _dedupe_line_matches(matches):
    """Drop duplicate line dicts that share date_range + billed + paid."""
    seen = set()
    unique = []
    for entry in matches or []:
        key = (entry.get('date_range'), entry.get('billed'), entry.get('paid'))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _aggregate_line_amounts(matches, payer_config=None):
    """Sum billed/paid across matching line dicts as ``$X.XX``."""
    billed, paid, _is_adjusted = aggregate_line_amounts_for_payer(
        matches, payer_config,
    )
    return billed, paid


def _is_header_level_snapshot(snapshot, payer_config):
    """True when amounts/denial come from the claim header, not service lines."""
    if not payer_config or not payer_config.get('uses_line_level'):
        return True
    return not list(snapshot.get('lines') or [])


def _promote_check_date(claim, payer_config):
    """Header-only payers copy Check Date onto Finalized Date when configured."""
    if payer_config and payer_config.get('uses_results_finalized_date'):
        return
    check_date = claim.get('Check Date')
    if check_date and check_date != '--':
        claim['Finalized Date'] = check_date


def _denial_from_line_matches(matches, payer_config):
    """Format cached denial pairs for the VisitDate-matched lines."""
    from availity_app.automation import (
        _finalize_denial_reason,
        _format_denial_from_code_pairs,
        _include_denial_code,
        _is_fully_paid_amounts,
        _payer_reads_hipaa_codes,
    )

    use_hipaa = _payer_reads_hipaa_codes(payer_config)
    matches = denial_entries_preferring_original(matches, payer_config)
    lines_by_date = {}
    skipped_fully_paid = False
    for entry in matches:
        date_range = entry.get('date_range') or '_single'
        pairs = list(entry.get('denial_pairs') or [])
        if pairs:
            lines_by_date.setdefault(date_range, [])
            for pair in pairs:
                if pair not in lines_by_date[date_range]:
                    lines_by_date[date_range].append(tuple(pair))
        elif (
            use_hipaa
            and _is_fully_paid_amounts(entry.get('billed'), entry.get('paid'))
        ):
            skipped_fully_paid = True

    result = _finalize_denial_reason(
        _format_denial_from_code_pairs(
            lines_by_date, include_code=_include_denial_code(payer_config),
        ),
        payer_config,
    )
    if result != '--':
        return result
    if use_hipaa and skipped_fully_paid:
        return FULLY_PAID_DENIAL_REASON
    return result


def _apply_canned_paid_denial(claim, payer_config=None):
    """Apply Paid/Overpayment canned text after remittance assignment.

    Fully paid / overpayment line amounts replace remittance text. Remarks-first
    payers keep harvested denial pairs. Other statuses only fill a blank
    Denial Reason.
    """
    from availity_app.automation import (
        _fill_blank_status_denial,
        _is_blank_denial_reason,
        _payment_skips_remittance,
    )

    if (
        (payer_config or {}).get('remittance_when_remarks_blank')
        and not _is_blank_denial_reason(claim.get('Denial Reason'))
    ):
        return
    skip_why = _payment_skips_remittance(
        claim.get('Billed Amount'), claim.get('Paid Amount'),
    )
    if skip_why == 'fully_paid':
        claim['Denial Reason'] = FULLY_PAID_DENIAL_REASON
        return
    if skip_why == 'overpayment':
        claim['Denial Reason'] = FULLY_PAID_OVERPAYMENT_DENIAL_REASON
        return
    _fill_blank_status_denial(claim)


def _line_remittance_denials(matches):
    """Unique usable remittance denials attached to matched claim-detail lines."""
    from availity_app.automation import _usable_remittance_denial

    values = []
    for entry in matches or []:
        denial = entry.get('remittance_denial')
        if _usable_remittance_denial(denial) and denial not in values:
            values.append(denial)
    return values


def _matches_mix_paid_and_denied(matches):
    """True when a split/merge combo mixes fully-paid and unpaid remittance lines."""
    from availity_app.automation import _payment_skips_remittance

    if len(matches or []) < 2:
        return False
    classes = set()
    for entry in matches:
        skip = _payment_skips_remittance(entry.get('billed'), entry.get('paid'))
        classes.add('paid' if skip else 'unpaid')
    return 'paid' in classes and 'unpaid' in classes


def materialize_claim_for_visit(snapshot, visit_date, payer_config,
                                row_index=None):
    """Build one claim result dict for ``visit_date``, or None to omit it.

    Line-level snapshots with no matching service line return None (the
    submission is skipped for that VisitDate). Header-only snapshots and
    line-level snapshots with an empty line table apply to every visit.

    Invoice-alignment payers select the line from the precomputed match map
    keyed by ``row_index`` instead of by VisitDate.
    """
    from availity_app.automation import (
        _apply_finalized_date_priority,
        _is_blank_denial_reason,
        _is_fully_paid_amounts,
        _payer_reads_hipaa_codes,
        _payment_skips_remittance,
        _usable_remittance_denial,
        derive_claim_status,
    )

    if not snapshot:
        return None

    header = snapshot.get('header') or {}
    claim = _copy_header_claim(header)
    portal_status = str(claim.get('Claim Status') or '')
    pending = portal_status.upper() == 'PENDING'

    if _is_header_level_snapshot(snapshot, payer_config):
        _promote_check_date(claim, payer_config)
        remittance = snapshot.get('remittance_denial')
        if remittance not in (None, ''):
            claim['Denial Reason'] = remittance
        billed = claim.get('Billed Amount', '--')
        paid = claim.get('Paid Amount', '--')
        if not pending:
            derive_claim_status(claim, billed, paid, payer_config)
        _apply_canned_paid_denial(claim, payer_config)
        _apply_finalized_date_priority(
            claim, snapshot.get('results_finalized_date'), payer_config,
        )
        return _finalize_output(claim, payer_config)

    cfg = dict(resolve_line_level_config(payer_config))
    cfg['fallback_to_sole_line'] = False

    aligned = None
    if uses_invoice_alignment(payer_config):
        aligned = alignment_entry_for_row(snapshot, row_index)
        if aligned is None or aligned.get('unresolved') or not aligned.get('line'):
            return None
        matches = [aligned['line']]
        if aligned.get('split_bill') and aligned.get('line'):
            # Combo/split entries still expose the aggregated line; mixed
            # paid+denied remittance reasons on consumed members are flagged
            # via remittance_denial lists stored on the line when present.
            extra = aligned.get('consumed_lines') or []
            if extra:
                matches = list(extra)
    else:
        snapshot_matches, _unresolved = filter_line_rows_by_visit_date(
            snapshot.get('lines') or [], visit_date, cfg=cfg,
        )
        matches = _dedupe_line_matches(snapshot_matches)
    if not matches:
        return None

    if aligned is not None and aligned.get('split_bill'):
        billed = aligned.get('billed') or matches[0].get('billed', '--')
        paid = aligned.get('paid') or matches[0].get('paid', '--')
        is_adjusted = False
    else:
        billed, paid, is_adjusted = aggregate_line_amounts_for_payer(
            matches, payer_config,
        )
    claim['Billed Amount'] = billed
    claim['Paid Amount'] = paid
    if (
        aligned is not None
        and 'Match Confidence' in resolved_output_columns(payer_config)
    ):
        claim['Match Confidence'] = (
            humanize_match_confidence(_alignment_confidence(aligned)) or '--'
        )

    if (
        aligned is not None
        and aligned.get('split_bill')
        and _matches_mix_paid_and_denied(matches)
    ):
        return need_manual_review_result(
            payer_config, 'split/merge mixed paid and denied remittance',
        )

    line_denial = _denial_from_line_matches(matches, payer_config)
    if (
        _payer_reads_hipaa_codes(payer_config)
        and not pending
        and _is_fully_paid_amounts(billed, paid)
        and _is_blank_denial_reason(line_denial)
    ):
        line_denial = FULLY_PAID_DENIAL_REASON

    line_remits = _line_remittance_denials(matches)
    if len(line_remits) > 1:
        return need_manual_review_result(
            payer_config, 'multiple remittance denials on matched lines',
        )
    line_remit = line_remits[0] if line_remits else None
    remittance = snapshot.get('remittance_denial')
    blank_mode = bool(payer_config.get('remittance_when_remarks_blank'))
    skip_why = None if pending else _payment_skips_remittance(billed, paid)

    if pending:
        claim['Denial Reason'] = IN_PROCESS_DENIAL_REASON
    elif blank_mode and not _is_blank_denial_reason(line_denial):
        claim['Denial Reason'] = line_denial
    elif skip_why == 'fully_paid':
        claim['Denial Reason'] = FULLY_PAID_DENIAL_REASON
    elif skip_why == 'overpayment':
        claim['Denial Reason'] = FULLY_PAID_OVERPAYMENT_DENIAL_REASON
    elif _usable_remittance_denial(line_remit):
        claim['Denial Reason'] = line_remit
    elif payer_config.get('uses_remittance') and _usable_remittance_denial(
        remittance,
    ):
        claim['Denial Reason'] = remittance
    elif payer_config.get('uses_remittance') and remittance not in (None, ''):
        if blank_mode:
            claim['Denial Reason'] = line_denial
        else:
            claim['Denial Reason'] = remittance
    else:
        claim['Denial Reason'] = line_denial

    if not pending:
        derive_claim_status(
            claim, billed, paid, payer_config, is_adjusted=is_adjusted,
        )
    _apply_canned_paid_denial(claim, payer_config)
    _apply_finalized_date_priority(
        claim, snapshot.get('results_finalized_date'), payer_config,
    )
    return _finalize_output(claim, payer_config)


def materialize_row_results(snapshots, visit_date, payer_config, row_index=None):
    """Materialize every snapshot that applies to ``visit_date``."""
    if any(
        snapshot.get('remit_join_unresolved') for snapshot in (snapshots or [])
    ):
        return [need_manual_review_result(
            payer_config, 'remittance line join ambiguous',
        )]
    results = []
    for snapshot in snapshots or []:
        claim = materialize_claim_for_visit(
            snapshot, visit_date, payer_config, row_index=row_index,
        )
        if claim:
            results.append(claim)
    if any(
        str(claim.get('Claim Status') or '') == NEED_MANUAL_REVIEW_STATUS
        for claim in results
    ):
        return [need_manual_review_result(payer_config)]
    return results


def visit_date_for_row(df, row_index):
    """Spreadsheet VisitDate for a dataframe row."""
    return safe_field(df.iloc[row_index], 'VisitDate')
