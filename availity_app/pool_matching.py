"""Pool-based tiered invoice â†” portal-line matching.

Consume-once pool pipeline: P1 validation, P2 reversal netting, Tier 1/1.5
signature+paid, Tier 2 SPLIT, Tier 3 MERGE, Tier 4 legacy fallbacks, Tier 5
leftovers, then claim-level W1/W2 warnings.

Pure dict/row logic â€” no Playwright imports. Imports from utils are deferred
inside functions to avoid circular imports.
"""
from __future__ import annotations

import itertools
from collections import defaultdict


def _u():
    """Lazy import of availity_app.utils to avoid circular import at module load."""
    import availity_app.utils as utils
    return utils


def _empty_entry(*, confidence=None, note=None, unresolved=True):
    u = _u()
    if confidence is None:
        confidence = u.MATCH_CONFIDENCE_REVIEW
    entry = {
        'line': None,
        'unresolved': unresolved,
        'units_mismatch': False,
        'split_bill': False,
        'confidence': u.humanize_match_confidence(confidence),
    }
    if note:
        entry['note'] = note
    return entry


def _append_note(entry, text):
    if not text:
        return
    existing = entry.get('note') or ''
    entry['note'] = f"{existing}; {text}" if existing else text


def _format_cents_note(cents):
    u = _u()
    if cents is None:
        return '--'
    return u._format_match_currency(cents / 100.0)


def _parse_input_record(df_idx, row, sort_pos):
    u = _u()
    amount_c = u.to_cents(u.safe_field(row, 'Amount'))
    units = u.parse_quantity(u.safe_field(row, 'Units'))
    paid_c = u.to_cents(u.safe_field(row, 'PaidAmount'))
    adj_raw = u.safe_field(row, 'Adj', '')
    if not adj_raw or str(adj_raw).strip() in ('', '--', '-', 'n/a', 'N/A'):
        adj_c = 0
    else:
        adj_c = u.to_cents(adj_raw)
        if adj_c is None:
            adj_c = 0
    balance_c = u.to_cents(u.safe_field(row, 'Balance'))
    return {
        'df_idx': df_idx,
        'row': row,
        'sort_pos': sort_pos,
        'amount_c': amount_c,
        'units': units,
        'paid_c': paid_c,
        'adj_c': adj_c,
        'balance_c': balance_c,
    }


def _parse_line_record(line, pool_pos):
    u = _u()
    billed_c = u.to_cents(line.get('billed'))
    paid_c = u.to_cents(line.get('paid'))
    if paid_c is None:
        paid_c = 0
    qty = u.parse_quantity(line.get('quantity'))
    if qty is None:
        qty = 0.0
    idx = line.get('idx')
    if idx is None:
        idx = pool_pos
    return {
        'line': line,
        'pool_pos': pool_pos,
        'idx': idx,
        'billed_c': billed_c if billed_c is not None else 0,
        'paid_c': paid_c,
        'qty': qty,
        'status': u._normalized_line_status(line),
    }


def _pool_dump_note(pool):
    if not pool:
        return 'remaining pool empty'
    parts = []
    for rec in pool:
        billed = _format_cents_note(rec['billed_c'])
        parts.append(f"L{rec['idx']}:{billed}/q{rec['qty']:g}")
    return 'remaining pool: ' + ', '.join(parts)


def _remove_from_pool(pool, recs):
    remove_ids = {id(r) for r in recs}
    pool[:] = [r for r in pool if id(r) not in remove_ids]


def _remove_inputs(unresolved, recs):
    remove_ids = {id(r) for r in recs}
    unresolved[:] = [r for r in unresolved if id(r) not in remove_ids]


def _p1_validate_rows(input_recs, result):
    u = _u()
    valid = []
    for rec in input_recs:
        amount_c, paid_c, balance_c = rec['amount_c'], rec['paid_c'], rec['balance_c']
        adj_c = rec['adj_c'] if rec['adj_c'] is not None else 0
        if amount_c is None or paid_c is None or balance_c is None:
            valid.append(rec)
            continue
        if abs(amount_c - paid_c - adj_c - balance_c) > u.TOL_CENTS_SUM:
            note = (
                f"{_format_cents_note(amount_c)} - {_format_cents_note(paid_c)} - "
                f"{_format_cents_note(adj_c)} != {_format_cents_note(balance_c)}"
            )
            result[rec['df_idx']] = _empty_entry(
                confidence=u.MATCH_CONFIDENCE_DATA_MISMATCH,
                note=note,
            )
            u.log_to_gui(
                f"    âš ï¸ Row {rec['df_idx']} DATA_MISMATCH: {note}\n",
                'error',
            )
            continue
        valid.append(rec)
    return valid


def _p2_net_reversals(pool, claim_notes):
    u = _u()
    negatives = [r for r in list(pool) if r['billed_c'] < 0]
    for neg in negatives:
        if neg not in pool and id(neg) not in {id(x) for x in pool}:
            # already removed
            still = next((x for x in pool if x is neg), None)
            if still is None:
                continue
        if not any(x is neg for x in pool):
            continue
        target_abs = abs(neg['billed_c'])
        partner = None
        for pos in pool:
            if pos is neg:
                continue
            if (
                pos['billed_c'] > 0
                and pos['billed_c'] == target_abs
                and u.units_eq(pos['qty'], neg['qty'])
            ):
                partner = pos
                break
        if partner is None:
            amt = _format_cents_note(neg['billed_c'])
            return f"unpaired reversal line {neg['idx']} ({amt})"
        claim_notes.append(
            f"netted reversal pair lines {neg['idx']}/{partner['idx']}"
        )
        u.log_to_gui(
            f"    â„¹ï¸ Netted reversal pair lines {neg['idx']}/{partner['idx']}\n",
            'info',
        )
        _remove_from_pool(pool, [neg, partner])
    return None


def _cents_as_dollars(cents):
    if cents is None:
        return None
    return cents / 100.0


def _paid_sub_bijection(inputs, line_recs):
    u = _u()
    if any(r['paid_c'] is None for r in inputs):
        return None
    available = list(line_recs)
    assignment = []
    for inp in inputs:
        matches = [
            lr for lr in available
            if u.money_eq(lr['paid_c'], inp['paid_c'], u.TOL_CENTS_SUM)
        ]
        if len(matches) != 1:
            return None
        pick = matches[0]
        available.remove(pick)
        assignment.append((inp, pick))
    if available:
        return None
    return assignment


def _tier1_pool_signature(unresolved, pool, result):
    u = _u()
    if not unresolved or not pool:
        return

    input_groups = defaultdict(list)
    for rec in unresolved:
        sig = u.line_signature(rec['units'], _cents_as_dollars(rec['amount_c']))
        if sig is None:
            return
        input_groups[sig].append(rec)

    line_groups = defaultdict(list)
    for lr in pool:
        sig = u.line_signature(lr['qty'], _cents_as_dollars(lr['billed_c']))
        if sig is None:
            return
        line_groups[sig].append(lr)

    matched_inputs = []
    matched_lines = []
    line_dicts = [lr['line'] for lr in pool]
    used_line_indices = set()

    for sig, inputs in input_groups.items():
        lines = line_groups.get(sig)
        if not lines or len(lines) != len(inputs):
            continue

        if len(inputs) == 1:
            inp, lr = inputs[0], lines[0]
            if inp['paid_c'] is not None:
                if not u.money_eq(lr['paid_c'], inp['paid_c'], u.TOL_CENTS_SUM):
                    continue
            entry = u._build_matched_line_entry(
                lr['line'], inp['row'], confidence=u.MATCH_CONFIDENCE_HIGH,
            )
            result[inp['df_idx']] = entry
            matched_inputs.append(inp)
            matched_lines.append(lr)
            continue

        paid_map = _paid_sub_bijection(inputs, lines)
        if paid_map is not None:
            for inp, lr in paid_map:
                entry = u._build_matched_line_entry(
                    lr['line'], inp['row'], confidence=u.MATCH_CONFIDENCE_HIGH,
                )
                entry['note'] = 'paid-disambiguated'
                result[inp['df_idx']] = entry
                matched_inputs.append(inp)
                matched_lines.append(lr)
            u.log_to_gui(
                f"    âœ“ Paid-disambiguated signature group "
                f"({u._describe_signature(sig)}): {len(inputs)} row(s)\n",
                'success',
            )
            continue

        statuses = {lr['status'] for lr in lines}
        ambiguous = len(statuses) > 1
        confidence = (
            u.MATCH_CONFIDENCE_LOW_ORDER if ambiguous else u.MATCH_CONFIDENCE_HIGH
        )
        if ambiguous:
            u.log_to_gui(
                f"    âš ï¸ {len(inputs)} row(s) share signature "
                f"({u._describe_signature(sig)}) but their lines have differing "
                f"statuses â€” assigned by VisitDate order (LOW confidence)\n",
                'error',
            )
        remaining_lines = list(lines)
        for inp in inputs:
            pick = u._assign_line_by_bundle_day(
                inp['row'], line_dicts, used_line_indices, sig=sig,
            )
            lr = None
            if pick is not None:
                line_idx, line = pick
                used_line_indices.add(line_idx)
                lr = next(
                    (
                        x for x in remaining_lines
                        if x['line'] is line or x['idx'] == line.get('idx')
                    ),
                    None,
                )
            if lr is None and remaining_lines:
                lr = remaining_lines[0]
            if lr is None:
                continue
            remaining_lines.remove(lr)
            if lr['idx'] is not None:
                used_line_indices.add(lr['idx'])
            entry = u._build_matched_line_entry(
                lr['line'], inp['row'], confidence=confidence,
            )
            result[inp['df_idx']] = entry
            matched_inputs.append(inp)
            matched_lines.append(lr)

    if matched_inputs:
        _remove_inputs(unresolved, matched_inputs)
        _remove_from_pool(pool, matched_lines)
        u.log_to_gui(
            f"    âœ“ Pool Tier 1: matched {len(matched_inputs)} row(s)\n",
            'success',
        )


def _split_tiebreak_ok(subset, inp):
    u = _u()
    if inp['paid_c'] is None:
        return False
    sum_paid = sum(lr['paid_c'] for lr in subset)
    if not u.money_eq(sum_paid, inp['paid_c'], u.TOL_CENTS_SUM):
        return False
    balance_c = inp['balance_c']
    adj_c = inp['adj_c'] if inp['adj_c'] is not None else 0
    if balance_c is None:
        return False
    sum_unpaid = sum(lr['billed_c'] - lr['paid_c'] for lr in subset)
    return u.money_eq(sum_unpaid, balance_c + adj_c, u.TOL_CENTS_SUM)


def _split_forced_status(subset):
    statuses = [lr['status'] for lr in subset]
    all_paid = all('paid' in s and 'denied' not in s for s in statuses)
    all_non_paid = all(not ('paid' in s and 'denied' not in s) for s in statuses)
    if all_paid:
        return 'PAID', False
    if all_non_paid:
        return 'DENIED', True
    return 'PARTIALLY PAID', True


def _build_split_entry(inp, subset, note):
    u = _u()
    sum_billed = sum(lr['billed_c'] for lr in subset)
    sum_paid = sum(lr['paid_c'] for lr in subset)
    sum_qty = sum(lr['qty'] for lr in subset)
    forced_status, needs_comment = _split_forced_status(subset)
    billed_str = u._format_match_currency(sum_billed / 100.0)
    paid_str = u._format_match_currency(sum_paid / 100.0)
    first = subset[0]['line']
    synthetic = {
        'row': first.get('row'),
        'idx': first.get('idx', 0),
        'billed': billed_str,
        'paid': paid_str,
        'date_range': first.get('date_range'),
        'quantity': str(sum_qty),
        'status': forced_status,
    }
    denial_line_numbers = [
        lr['idx'] + 1 for lr in subset
        if not ('paid' in lr['status'] and 'denied' not in lr['status'])
    ]
    return {
        'line': u._present_line_for_match(synthetic, inp['row']),
        'unresolved': False,
        'units_mismatch': False,
        'split_bill': True,
        'billed': billed_str,
        'paid': paid_str,
        'forced_status': forced_status,
        'needs_claim_comment': needs_comment,
        'confidence': u.build_match_confidence(u.MATCH_CONFIDENCE_SPLIT_VISIT),
        'note': note,
        'denial_line_numbers': denial_line_numbers,
        'matched_line_idxs': [lr['idx'] for lr in subset],
    }


def _tier2_split(unresolved, pool, result, *, allow_split_tiebreak, max_combo):
    u = _u()
    if not unresolved or not pool:
        return

    for inp in list(unresolved):
        if inp['amount_c'] is None or inp['units'] is None:
            continue
        if len(pool) < 2:
            break

        candidates = []
        n = len(pool)
        for size in range(2, min(max_combo, n) + 1):
            for combo_idxs in itertools.combinations(range(n), size):
                subset = [pool[i] for i in combo_idxs]
                sum_billed = sum(lr['billed_c'] for lr in subset)
                sum_qty = sum(lr['qty'] for lr in subset)
                if (
                    u.money_eq(sum_billed, inp['amount_c'], u.TOL_CENTS_SUM)
                    and u.units_eq(sum_qty, inp['units'])
                ):
                    candidates.append(subset)

        if not candidates:
            continue

        chosen = None
        note = None
        if len(candidates) == 1:
            chosen = candidates[0]
            if _split_tiebreak_ok(chosen, inp):
                note = 'paid/balance verified'
            else:
                note = 'WARN: paid/balance tiebreak failed'
        elif allow_split_tiebreak:
            survivors = [c for c in candidates if _split_tiebreak_ok(c, inp)]
            if len(survivors) == 1:
                chosen = survivors[0]
                note = 'paid/balance verified'
            else:
                result[inp['df_idx']] = _empty_entry(
                    note=f"{len(candidates)} valid split subsets â€” ambiguous",
                )
                _remove_inputs(unresolved, [inp])
                continue
        else:
            result[inp['df_idx']] = _empty_entry(
                note=f"{len(candidates)} valid split subsets â€” ambiguous",
            )
            _remove_inputs(unresolved, [inp])
            continue

        if chosen is None:
            continue

        entry = _build_split_entry(inp, chosen, note)
        result[inp['df_idx']] = entry
        _remove_inputs(unresolved, [inp])
        _remove_from_pool(pool, chosen)
        idxs = [lr['idx'] for lr in chosen]
        u.log_to_gui(
            f"    âœ“ SPLIT_VISIT row {inp['df_idx']} â†’ lines {idxs} ({note})\n",
            'success',
        )


def _tier3_merge(unresolved, pool, result, *, max_combo):
    u = _u()
    if not unresolved or not pool:
        return

    while True:
        n_rows = len(unresolved)
        if n_rows < 2 or not pool:
            break

        pairs = []
        for size in range(2, min(max_combo, n_rows) + 1):
            for combo in itertools.combinations(range(n_rows), size):
                inputs = [unresolved[i] for i in combo]
                if any(r['amount_c'] is None or r['units'] is None for r in inputs):
                    continue
                sum_amt = sum(r['amount_c'] for r in inputs)
                sum_units = sum(r['units'] for r in inputs)
                for lr in pool:
                    if (
                        u.money_eq(sum_amt, lr['billed_c'], u.TOL_CENTS_SUM)
                        and u.units_eq(sum_units, lr['qty'])
                    ):
                        pairs.append((combo, inputs, lr))

        if not pairs:
            break

        line_to_combos = defaultdict(list)
        for combo, inputs, lr in pairs:
            line_to_combos[id(lr)].append((combo, inputs, lr))

        accepted = []
        used_row_positions = set()
        for _line_id, combos in line_to_combos.items():
            if len(combos) != 1:
                continue
            combo, inputs, lr = combos[0]
            if any(i in used_row_positions for i in combo):
                continue
            accepted.append((combo, inputs, lr))
            used_row_positions.update(combo)

        row_count = defaultdict(int)
        for combo, _inputs, _lr in accepted:
            for i in combo:
                row_count[i] += 1
        accepted = [
            (combo, inputs, lr) for combo, inputs, lr in accepted
            if all(row_count[i] == 1 for i in combo)
        ]

        if not accepted:
            break

        any_merged = False
        for combo, inputs, lr in accepted:
            note = (
                f"part of merged line {lr['idx']} covering {len(inputs)} visits"
            )
            for inp in inputs:
                entry = u._build_matched_line_entry(
                    lr['line'], inp['row'],
                    confidence=u.MATCH_CONFIDENCE_MERGED_LINE,
                )
                entry['note'] = note
                result[inp['df_idx']] = entry
            _remove_inputs(unresolved, inputs)
            _remove_from_pool(pool, [lr])
            any_merged = True
            u.log_to_gui(
                f"    âœ“ MERGED_LINE {lr['idx']}: {len(inputs)} visits\n",
                'success',
            )

        if not any_merged:
            break


def _apply_claim_warnings(result, all_line_recs, pool, header_billed, claim_notes):
    u = _u()
    warnings = list(claim_notes)

    header_c = None
    if header_billed is not None:
        if isinstance(header_billed, (int, float)) and not isinstance(header_billed, bool):
            header_c = int(round(float(header_billed) * 100))
        else:
            header_c = u.to_cents(header_billed)

    if header_c is not None:
        line_sum = sum(r['billed_c'] for r in all_line_recs)
        if abs(line_sum - header_c) > u.TOL_CENTS_HEADER:
            warnings.append(
                f"CLAIM WARN: line sum {_format_cents_note(line_sum)} != "
                f"header billed {_format_cents_note(header_c)} â€” "
                f"possible pagination miss"
            )

    denied_left = [
        r['idx'] for r in pool
        if r.get('status') and not (
            'paid' in r['status'] and 'denied' not in r['status']
        )
    ]
    if denied_left:
        warnings.append(
            f"CLAIM WARN: unmatched DENIED lines left: {denied_left}"
        )

    if not warnings:
        return
    for entry in result.values():
        for w in warnings:
            _append_note(entry, w)


def match_with_pool_tiers(
    sorted_inputs,
    line_rows,
    *,
    dates_aligned,
    header_billed=None,
    header_paid=None,
    panel_range_start=None,
    panel_range_end=None,
    allow_split_tiebreak=False,
    max_combo=3,
    fallback_fn=None,
):
    """Run the full pool-tier pipeline. ``fallback_fn`` is Tier 4 legacy matcher."""
    u = _u()
    result = {}
    if not sorted_inputs:
        return result

    input_recs = [
        _parse_input_record(df_idx, row, pos)
        for pos, (df_idx, row) in enumerate(sorted_inputs)
    ]
    all_line_recs = [
        _parse_line_record(line, pos)
        for pos, line in enumerate(line_rows or [])
    ]
    pool = list(all_line_recs)
    claim_notes = []

    if not pool:
        for rec in input_recs:
            result[rec['df_idx']] = _empty_entry()
        return result

    unresolved = _p1_validate_rows(input_recs, result)

    abort_note = _p2_net_reversals(pool, claim_notes)
    if abort_note is not None:
        for rec in input_recs:
            result[rec['df_idx']] = _empty_entry(note=abort_note)
        u.log_to_gui(f"    âš ï¸ Reversal abort: {abort_note}\n", 'error')
        return result

    _tier1_pool_signature(unresolved, pool, result)
    _tier2_split(
        unresolved, pool, result,
        allow_split_tiebreak=allow_split_tiebreak,
        max_combo=max_combo,
    )
    _tier3_merge(unresolved, pool, result, max_combo=max_combo)

    if unresolved and pool and fallback_fn is not None:
        survivor_inputs = [(r['df_idx'], r['row']) for r in unresolved]
        survivor_lines = [r['line'] for r in pool]
        if panel_range_start and panel_range_end:
            survivor_aligned = u.invoice_service_dates_aligned(
                survivor_inputs, panel_range_start, panel_range_end,
            )
        else:
            survivor_aligned = dates_aligned
        try:
            fb = fallback_fn(
                survivor_inputs, survivor_lines,
                dates_aligned=survivor_aligned,
                header_billed=header_billed,
                header_paid=header_paid,
                panel_range_start=panel_range_start,
                panel_range_end=panel_range_end,
            )
        except Exception as exc:
            u.log_to_gui(f"    â„¹ï¸ Tier 4 fallback skipped ({exc})\n", 'info')
            fb = {}

        consumed_line_ids = set()
        resolved_recs = []
        for rec in list(unresolved):
            entry = fb.get(rec['df_idx'])
            if entry is None:
                continue
            result[rec['df_idx']] = entry
            if not entry.get('unresolved') and entry.get('line'):
                line = entry['line']
                idx = line.get('idx')
                for lr in pool:
                    if lr['line'] is line or (idx is not None and lr['idx'] == idx):
                        consumed_line_ids.add(id(lr))
                        break
                if entry.get('split_bill'):
                    for lr in pool:
                        consumed_line_ids.add(id(lr))
                resolved_recs.append(rec)
        if resolved_recs:
            _remove_inputs(unresolved, resolved_recs)
        if consumed_line_ids:
            pool[:] = [r for r in pool if id(r) not in consumed_line_ids]

    pool_note = _pool_dump_note(pool)
    for rec in unresolved:
        existing = result.get(rec['df_idx'])
        if existing and not existing.get('unresolved'):
            continue
        if existing and existing.get('unresolved'):
            _append_note(existing, pool_note)
            existing['confidence'] = u.humanize_match_confidence(
                u.MATCH_CONFIDENCE_REVIEW
            )
        else:
            result[rec['df_idx']] = _empty_entry(note=pool_note)

    for rec in input_recs:
        if rec['df_idx'] not in result:
            result[rec['df_idx']] = _empty_entry(note=pool_note)

    _apply_claim_warnings(result, all_line_recs, pool, header_billed, claim_notes)
    return result
