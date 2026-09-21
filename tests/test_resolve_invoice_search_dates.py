"""Tests for invoice VisitDate aggregation as primary search dates."""
import pandas as pd

from availity_app.utils import resolve_invoice_search_dates


def _row(**overrides):
    base = {
        'AltPatientID': 'M1',
        'Last_Name': 'Doe',
        'First_Name': 'Jane',
        'DOB': '01/01/1990',
        'StartDate': '01/01/2025',
        'EndDate': '01/31/2025',
        'InvoiceNumber': 'INV1',
        'VisitDate': '01/05/2025',
    }
    base.update(overrides)
    return base


def test_multi_row_invoice_uses_min_max_visit_date():
    df = pd.DataFrame([
        _row(VisitDate='01/10/2025'),
        _row(VisitDate='01/05/2025'),
        _row(VisitDate='01/20/2025'),
        _row(InvoiceNumber='INV2', VisitDate='02/01/2025'),
    ])
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('01/05/2025', '01/20/2025', True)


def test_single_visit_date_mirrors_both_bounds():
    df = pd.DataFrame([_row(VisitDate='03/15/2025')])
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('03/15/2025', '03/15/2025', True)


def test_missing_visit_dates_falls_back_to_start_end():
    df = pd.DataFrame([
        _row(VisitDate='', StartDate='02/01/2025', EndDate='02/28/2025'),
        _row(VisitDate='--', StartDate='02/01/2025', EndDate='02/28/2025'),
    ])
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('02/01/2025', '02/28/2025', False)


def test_missing_invoice_falls_back_to_row_dates():
    df = pd.DataFrame([
        _row(InvoiceNumber='', StartDate='04/01/2025', EndDate='04/10/2025'),
    ])
    start, end, from_agg = resolve_invoice_search_dates(df, df.iloc[0])
    assert (start, end, from_agg) == ('04/01/2025', '04/10/2025', False)


def test_no_df_falls_back_to_row_dates():
    row = _row(StartDate='05/01/2025', EndDate='05/05/2025', VisitDate='05/03/2025')
    start, end, from_agg = resolve_invoice_search_dates(None, row)
    assert (start, end, from_agg) == ('05/01/2025', '05/05/2025', False)


def test_patient_cluster_uses_minmax_across_invoices():
    from availity_app.constants import PAYER_CONFIG

    df = pd.DataFrame([
        _row(VisitDate='01/05/2025', InvoiceNumber='INV1'),
        _row(VisitDate='01/20/2025', InvoiceNumber='INV2'),
        _row(VisitDate='04/15/2025', InvoiceNumber='INV3'),
    ])
    payer = PAYER_CONFIG['Villagecaremax']
    start, end, from_agg = resolve_invoice_search_dates(
        df, df.iloc[0], payer_config=payer,
    )
    assert (start, end, from_agg) == ('01/05/2025', '01/20/2025', True)
    start, end, from_agg = resolve_invoice_search_dates(
        df, df.iloc[2], payer_config=payer,
    )
    assert (start, end, from_agg) == ('04/15/2025', '04/15/2025', True)
