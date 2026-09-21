"""Tests for search grouping helpers."""
import pandas as pd

from availity_app.constants import PAYER_CONFIG
from availity_app.search_grouping import (
    build_consecutive_search_groups,
    cluster_visit_dates,
    row_eligible_for_grouping,
    search_group_key,
    supports_patient_search_grouping,
    supports_search_grouping,
)


def _sample_df():
    return pd.DataFrame([
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/05/2025',
        },
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV2',
            'VisitDate': '01/10/2025',
        },
        {
            'AltPatientID': 'M2',
            'Last_Name': 'Smith',
            'First_Name': 'John',
            'DOB': '02/02/1985',
            'StartDate': '02/01/2025',
            'EndDate': '02/28/2025',
            'InvoiceNumber': 'INV3',
            'VisitDate': '02/05/2025',
        },
    ])


def _vcm():
    return PAYER_CONFIG['Villagecaremax']


def test_search_group_key_differs_by_invoice_visit_dates():
    """Different invoices with different VisitDates get different search keys."""
    df = _sample_df()
    assert search_group_key(df, 0) != search_group_key(df, 1)
    assert search_group_key(df, 0) != search_group_key(df, 2)


def test_search_group_key_same_invoice_aggregation():
    """Rows sharing an invoice share the aggregated min–max VisitDate key."""
    df = pd.DataFrame([
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/05/2025',
        },
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/20/2025',
        },
    ])
    key0 = search_group_key(df, 0)
    key1 = search_group_key(df, 1)
    assert key0 == key1
    assert key0[4] == '01/05/2025'
    assert key0[5] == '01/20/2025'


def test_build_consecutive_groups():
    df = _sample_df()
    payer = PAYER_CONFIG['Healthfirst']
    groups = build_consecutive_search_groups([0, 1, 2], df, payer)
    assert groups == [[0], [1], [2]]


def test_build_consecutive_groups_shared_invoice():
    df = pd.DataFrame([
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/05/2025',
        },
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/20/2025',
        },
        {
            'AltPatientID': 'M2',
            'Last_Name': 'Smith',
            'First_Name': 'John',
            'DOB': '02/02/1985',
            'StartDate': '02/01/2025',
            'EndDate': '02/28/2025',
            'InvoiceNumber': 'INV3',
            'VisitDate': '02/05/2025',
        },
    ])
    payer = PAYER_CONFIG['Healthfirst']
    groups = build_consecutive_search_groups([0, 1, 2], df, payer)
    assert groups == [[0, 1], [2]]


def test_ineligible_row_is_singleton_group():
    df = _sample_df()
    df.at[0, 'InvoiceNumber'] = ''
    payer = PAYER_CONFIG['Healthfirst']
    groups = build_consecutive_search_groups([0, 1], df, payer)
    assert groups == [[0], [1]]


def test_row_eligible_requires_invoice_and_dob():
    df = _sample_df()
    assert row_eligible_for_grouping(df, 0) is True
    df.at[0, 'DOB'] = 'Not Found'
    assert row_eligible_for_grouping(df, 0) is False


def test_cluster_visit_dates_splits_after_90_days():
    dates = [
        pd.Timestamp('2025-01-01'),
        pd.Timestamp('2025-03-31'),
        pd.Timestamp('2025-04-02'),
    ]
    clusters = cluster_visit_dates(dates, max_days=90)
    assert len(clusters) == 2
    assert clusters[0][0] == pd.Timestamp('2025-01-01')
    assert clusters[0][1] == pd.Timestamp('2025-03-31')
    assert clusters[1][0] == pd.Timestamp('2025-04-02')


def test_villagecaremax_defaults_to_patient_search_grouping():
    assert supports_search_grouping(_vcm()) is True
    assert supports_patient_search_grouping(_vcm()) is True
    assert supports_patient_search_grouping(PAYER_CONFIG['Healthfirst']) is False


def test_patient_group_key_shares_90_day_cluster_across_invoices():
    df = _sample_df()
    payer = _vcm()
    key0 = search_group_key(df, 0, payer_config=payer)
    key1 = search_group_key(df, 1, payer_config=payer)
    assert key0 == key1
    assert key0[4] == '01/05/2025'
    assert key0[5] == '01/10/2025'
    assert search_group_key(df, 0, payer_config=payer) != search_group_key(
        df, 2, payer_config=payer,
    )


def test_patient_group_key_splits_when_visits_exceed_90_days():
    df = pd.DataFrame([
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '01/01/2025',
            'EndDate': '01/31/2025',
            'InvoiceNumber': 'INV1',
            'VisitDate': '01/01/2025',
        },
        {
            'AltPatientID': 'M1',
            'Last_Name': 'Doe',
            'First_Name': 'Jane',
            'DOB': '01/01/1990',
            'StartDate': '04/01/2025',
            'EndDate': '04/30/2025',
            'InvoiceNumber': 'INV2',
            'VisitDate': '04/02/2025',
        },
    ])
    payer = _vcm()
    assert search_group_key(df, 0, payer_config=payer) != search_group_key(
        df, 1, payer_config=payer,
    )
    groups = build_consecutive_search_groups([0, 1], df, payer)
    assert groups == [[0], [1]]


def test_patient_groups_restack_nonconsecutive_rows():
    df = _sample_df()
    groups = build_consecutive_search_groups([0, 1, 2], df, _vcm())
    assert groups == [[0, 1], [2]]

    interleaved = pd.DataFrame([
        df.iloc[0].to_dict(),
        df.iloc[2].to_dict(),
        df.iloc[1].to_dict(),
    ])
    groups = build_consecutive_search_groups([0, 1, 2], interleaved, _vcm())
    assert groups == [[0, 2], [1]]


def test_patient_grouping_off_when_search_grouping_disabled():
    payer = dict(_vcm())
    payer['supports_search_grouping'] = False
    assert supports_patient_search_grouping(payer) is False
    df = _sample_df()
    groups = build_consecutive_search_groups([0, 1, 2], df, payer)
    assert groups == [[0], [1], [2]]
