"""add_output_columns: rename input Claim Status only on a fresh collection."""
import pandas as pd

from availity_app.automation import add_output_columns


def test_fresh_collection_renames_input_claim_status():
    df = pd.DataFrame([{
        'AltPatientID': '1',
        'Claim Status': 'Original collection status',
    }])
    result = add_output_columns(df)

    assert result.at[0, 'ClaimStatus'] == 'Original collection status'
    assert result.at[0, 'Claim Status'] == ''
    assert result.at[0, 'AutomationStatus'] == 'Pending'


def test_prior_output_without_claimstatus_keeps_claim_status():
    df = pd.DataFrame([{
        'AltPatientID': '1',
        'Claim Status': 'Paid',
        'AutomationStatus': 'Done',
    }])
    result = add_output_columns(df)

    assert result.at[0, 'Claim Status'] == 'Paid'
    assert 'ClaimStatus' not in result.columns
    assert result.at[0, 'AutomationStatus'] == 'Done'


def test_prior_output_with_both_claim_status_columns_preserves_both():
    df = pd.DataFrame([{
        'AltPatientID': '1',
        'ClaimStatus': 'Original collection status',
        'Claim Status': 'Denied',
        'AutomationStatus': 'Done',
    }])
    result = add_output_columns(df)

    assert result.at[0, 'ClaimStatus'] == 'Original collection status'
    assert result.at[0, 'Claim Status'] == 'Denied'
    assert result.at[0, 'AutomationStatus'] == 'Done'


def test_claim_status_lowercase_renames_when_claimstatus_exists():
    df = pd.DataFrame([{
        'AltPatientID': '1',
        'ClaimStatus': 'Original collection status',
        'Claim status': 'In Billing Review',
        'Denial Reason': 'In Billing Review',
        'Recheck': 'no',
    }])
    result = add_output_columns(df)

    assert result.at[0, 'ClaimStatus'] == 'Original collection status'
    assert result.at[0, 'Claim Status'] == 'In Billing Review'
    assert 'Claim status' not in result.columns


def test_swhny_does_not_add_match_confidence_column():
    from availity_app.constants import PAYER_CONFIG, resolved_output_columns

    payer = PAYER_CONFIG['SWHNY']
    assert 'Match Confidence' not in resolved_output_columns(payer)

    df = pd.DataFrame([{
        'AltPatientID': '1',
        'Match Confidence': 'Need Manual Review',
        'AutomationStatus': 'Done',
    }])
    result = add_output_columns(df, payer)
    assert 'Match Confidence' not in result.columns
