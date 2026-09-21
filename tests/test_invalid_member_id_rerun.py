"""Invalid Member ID and Claim Not Found are completed outcomes, not retryable."""
import pandas as pd

from availity_app.automation import (
    apply_claim_not_found_completed_rows,
    queue_error_rows_for_retry,
    queue_recheck_yes_rows,
    row_should_process,
)
from availity_app.constants import (
    CLAIM_NOT_FOUND_STATUS,
    INVALID_MEMBER_ID_STATUS,
)


def _row(claim_status, automation_status='Done', recheck='', last_error=''):
    return pd.DataFrame([{
        'Claim Status': claim_status,
        'AutomationStatus': automation_status,
        'Recheck': recheck,
        'LastError': last_error,
    }])


def test_invalid_member_id_does_not_rerun_when_done():
    df = _row(INVALID_MEMBER_ID_STATUS)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert row_should_process(df, 0) is False


def test_invalid_member_id_does_not_rerun_when_recheck_no():
    df = _row(INVALID_MEMBER_ID_STATUS, recheck='no')
    df = queue_error_rows_for_retry(df)
    assert row_should_process(df, 0) is False


def test_invalid_member_id_reruns_when_recheck_yes():
    df = _row(INVALID_MEMBER_ID_STATUS, recheck='yes')
    df = queue_recheck_yes_rows(df)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Pending'
    assert row_should_process(df, 0) is True


def test_uppercase_known_error_claim_status_still_retries():
    df = _row('NAVIGATION FAILED', automation_status='Done')
    assert row_should_process(df, 0) is True


def test_claim_not_found_does_not_rerun_when_done():
    df = _row(CLAIM_NOT_FOUND_STATUS)
    df = apply_claim_not_found_completed_rows(df)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    assert row_should_process(df, 0) is False


def test_legacy_claim_not_found_error_is_completed():
    df = _row(
        CLAIM_NOT_FOUND_STATUS,
        automation_status='Error',
        last_error='Claim not found',
    )
    df = apply_claim_not_found_completed_rows(df)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    assert row_should_process(df, 0) is False


def test_claim_not_found_does_not_rerun_when_recheck_no():
    df = _row(
        CLAIM_NOT_FOUND_STATUS,
        automation_status='Error',
        recheck='no',
        last_error='Claim not found',
    )
    df = apply_claim_not_found_completed_rows(df)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Done'
    assert df.at[0, 'LastError'] == ''
    assert row_should_process(df, 0) is False


def test_claim_not_found_reruns_when_recheck_yes():
    df = _row(
        CLAIM_NOT_FOUND_STATUS,
        automation_status='Error',
        recheck='yes',
        last_error='Claim not found',
    )
    df = apply_claim_not_found_completed_rows(df)
    df = queue_recheck_yes_rows(df)
    df = queue_error_rows_for_retry(df)
    assert df.at[0, 'AutomationStatus'] == 'Pending'
    assert row_should_process(df, 0) is True
