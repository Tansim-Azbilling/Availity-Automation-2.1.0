"""Recheck=no + In Billing Review rows are completed without searching."""
import pandas as pd

from availity_app.automation import (
    add_output_columns,
    apply_in_billing_review_rows,
    mark_recheck_skipped_rows,
    queue_error_rows_for_retry,
    queue_recheck_yes_rows,
    row_is_in_billing_review_skip,
    row_should_process,
)
from availity_app.constants import IN_BILLING_REVIEW_STATUS


def _prepare(df):
    df = add_output_columns(df)
    df = apply_in_billing_review_rows(df)
    df = mark_recheck_skipped_rows(df)
    df = queue_recheck_yes_rows(df)
    df = queue_error_rows_for_retry(df)
    return df


def _row(**overrides):
    data = {
        'AltPatientID': '123',
        'PatientName': 'Doe, Jane',
        'DOB': '01/01/1980',
        'StartDate': '01/01/2026',
        'EndDate': '01/01/2026',
        'InvoiceNumber': 'INV1',
        'VisitDate': '01/01/2026',
        'ClaimStatus': 'Original collection status',
        'Claim status': IN_BILLING_REVIEW_STATUS,
        'Denial Reason': IN_BILLING_REVIEW_STATUS,
        'Recheck': 'no',
    }
    data.update(overrides)
    return pd.DataFrame([data])


def test_both_optional_columns_and_recheck_no_are_done_without_search():
    df = _prepare(_row())

    assert df.at[0, 'Claim Status'] == IN_BILLING_REVIEW_STATUS
    assert df.at[0, 'Denial Reason'] == IN_BILLING_REVIEW_STATUS
    assert df.at[0, 'ClaimStatus'] == 'Original collection status'
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    assert _normalize_recheck(df.at[0, 'Recheck']) == 'no'
    assert row_should_process(df, 0) is False
    assert row_is_in_billing_review_skip(df, 0) is True


def test_recheck_yes_still_searches():
    df = _prepare(_row(Recheck='yes'))

    assert row_should_process(df, 0) is True
    assert row_is_in_billing_review_skip(df, 0) is False
    assert df.at[0, 'AutomationStatus'] == 'Pending'


def test_recheck_empty_still_searches():
    df = _prepare(_row(Recheck=''))

    assert row_should_process(df, 0) is True
    assert row_is_in_billing_review_skip(df, 0) is False


def test_only_claim_status_in_billing_review_does_not_special_skip():
    df = _prepare(_row(**{'Denial Reason': 'Some other reason'}))

    assert row_is_in_billing_review_skip(df, 0) is False
    assert df.at[0, 'AutomationStatus'] == 'Skipped'
    assert row_should_process(df, 0) is False


def test_only_denial_reason_in_billing_review_does_not_special_skip():
    df = _prepare(_row(**{'Claim status': 'Paid'}))

    assert row_is_in_billing_review_skip(df, 0) is False
    assert df.at[0, 'AutomationStatus'] == 'Skipped'


def test_wrong_case_is_not_in_billing_review():
    df = _prepare(_row(
        **{
            'Claim status': 'in billing review',
            'Denial Reason': 'in billing review',
        }
    ))

    assert row_is_in_billing_review_skip(df, 0) is False
    assert df.at[0, 'AutomationStatus'] == 'Skipped'


def test_collection_claimstatus_alone_is_ignored():
    df = _prepare(_row(
        ClaimStatus=IN_BILLING_REVIEW_STATUS,
        **{
            'Claim status': '',
            'Denial Reason': IN_BILLING_REVIEW_STATUS,
        },
    ))

    assert row_is_in_billing_review_skip(df, 0) is False
    assert df.at[0, 'AutomationStatus'] == 'Skipped'
    assert df.at[0, 'ClaimStatus'] == IN_BILLING_REVIEW_STATUS


def test_prior_error_does_not_requeue_in_billing_review_row():
    df = _row(LastError='Search failed')
    df = _prepare(df)

    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    assert row_should_process(df, 0) is False


def _normalize_recheck(value):
    return str(value).strip().lower()
