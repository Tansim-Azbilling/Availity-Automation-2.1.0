"""One-pass invoice lookup index so claim helpers do not scan the whole sheet."""
from collections import defaultdict
import json
import os
import time

import pandas as pd

ATTR_KEY = '_availity_invoice_index'
_DEBUG_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'debug-ee96ab.log',
)
index_hits = 0
index_rebuilds = 0


def _agent_log(hypothesis_id, location, message, data):
    # #region agent log
    try:
        with open(_DEBUG_LOG, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({
                'sessionId': 'ee96ab',
                'runId': 'post-fix',
                'hypothesisId': hypothesis_id,
                'location': location,
                'message': message,
                'data': data,
                'timestamp': int(time.time() * 1000),
            }) + '\n')
    except Exception:
        pass
    # #endregion


class InvoiceIndex:
    """Positional invoice rows and cached min–max VisitDate ranges."""

    def __init__(self, nrows, invoice_to_indices, invoice_date_range):
        self.nrows = int(nrows)
        self.invoice_to_indices = invoice_to_indices
        self.invoice_date_range = invoice_date_range


def attach_invoice_index(df):
    """Build the index in one pass and store it on ``df.attrs``.

    VisitDate / InvoiceNumber are not rewritten during a run, so the index
    stays valid for the life of this dataframe.
    """
    from availity_app.utils import (
        format_invoice_number,
        normalize_date_range,
        normalize_search_date,
    )

    n = 0 if df is None else len(df)
    started = time.perf_counter()
    invoice_to_indices = defaultdict(list)
    parsed_by_invoice = defaultdict(list)

    if df is not None and n and 'InvoiceNumber' in df.columns:
        invoices = df['InvoiceNumber'].to_numpy()
        visits = (
            df['VisitDate'].to_numpy() if 'VisitDate' in df.columns else None
        )
        for i in range(n):
            inv = format_invoice_number(invoices[i], '')
            if not inv:
                continue
            invoice_to_indices[inv].append(i)
            if visits is None:
                continue
            normalized = normalize_search_date(visits[i])
            if not normalized:
                continue
            try:
                dt = pd.to_datetime(normalized, format='%m/%d/%Y')
                if pd.notna(dt):
                    parsed_by_invoice[inv].append(dt)
            except Exception:
                continue

    invoice_date_range = {}
    for inv, parsed in parsed_by_invoice.items():
        if not parsed:
            continue
        min_dt = min(parsed)
        max_dt = max(parsed)
        start = min_dt.strftime('%m/%d/%Y')
        end = max_dt.strftime('%m/%d/%Y')
        invoice_date_range[inv] = normalize_date_range(f"{start}-{end}")

    index = InvoiceIndex(n, dict(invoice_to_indices), invoice_date_range)
    if df is not None:
        try:
            df.attrs[ATTR_KEY] = index
        except Exception:
            pass
    # #region agent log
    _agent_log(
        'A',
        'invoice_index.py:attach_invoice_index',
        'rebuilt invoice index',
        {
            'nrows': n,
            'invoices': len(index.invoice_to_indices),
            'ms': round((time.perf_counter() - started) * 1000, 1),
        },
    )
    # #endregion
    return index


def get_invoice_index(df):
    """Return a valid index for ``df``, rebuilding when missing or stale."""
    global index_hits, index_rebuilds
    if df is None:
        return InvoiceIndex(0, {}, {})
    stored = None
    try:
        stored = df.attrs.get(ATTR_KEY)
    except Exception:
        stored = None
    if (
        isinstance(stored, InvoiceIndex)
        and stored.nrows == len(df)
    ):
        index_hits += 1
        return stored
    index_rebuilds += 1
    reason = 'missing'
    if stored is not None:
        try:
            reason = f'stale_nrows_{getattr(stored, "nrows", None)}_vs_{len(df)}'
        except Exception:
            reason = 'stale'
    # #region agent log
    if index_rebuilds <= 5 or index_rebuilds % 50 == 0:
        _agent_log(
            'A',
            'invoice_index.py:get_invoice_index',
            'index cache miss',
            {'rebuilds': index_rebuilds, 'hits': index_hits, 'reason': reason},
        )
    # #endregion
    return attach_invoice_index(df)
