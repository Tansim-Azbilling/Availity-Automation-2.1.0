"""Regression tests for strict claim-result completion rules."""
import unittest

from availity_app.result_validation import (
    FIELD_ABSENT,
    FIELD_FAILED,
    FIELD_FOUND,
    validate_claim_result,
    validate_claim_results,
)


PAYER_CONFIG = {
    "uses_remittance": True,
    "uses_results_finalized_date": False,
    "result_validation": {
        "finalized_optional_statuses": (
            "PENDING",
            "ACKNOWLEDGED",
            "RETURNED-TO-PROVIDER",
            "REJECTED",
            "RECEIVED",
        ),
    },
}


def claim_result(status="PAID", finalized="01/10/2026"):
    fields = {
        "Claim ID": "TEST-CLAIM-1",
        "Claim Status": status,
        "Billed Amount": "$100.00",
        "Paid Amount": "$100.00",
        "Finalized Date": finalized,
        "Check Number": "TEST-CHECK",
        "Check Date": "01/12/2026",
        "Denial Reason": "--",
    }
    fields["_field_states"] = {
        "Claim ID": FIELD_FOUND,
        "Claim Status": FIELD_FOUND,
        "Billed Amount": FIELD_FOUND,
        "Paid Amount": FIELD_FOUND,
        "Finalized Date": (
            FIELD_ABSENT if finalized == "--" else FIELD_FOUND
        ),
        "Check Number": FIELD_FOUND,
        "Check Date": FIELD_FOUND,
    }
    fields["_field_errors"] = {}
    fields["_denial_required"] = False
    fields["_denial_state"] = FIELD_ABSENT
    return fields


class ClaimResultValidationTests(unittest.TestCase):
    def test_complete_paid_claim_passes(self):
        self.assertEqual(validate_claim_result(claim_result(), PAYER_CONFIG), [])

    def test_adjudicated_claim_requires_finalized_date(self):
        errors = validate_claim_result(
            claim_result(status="Partially Paid", finalized="--"),
            PAYER_CONFIG,
        )
        self.assertTrue(
            any("Finalized Date" in error for error in errors),
            errors,
        )

    def test_returned_claim_may_lack_finalized_date(self):
        errors = validate_claim_result(
            claim_result(status="RETURNED-TO-PROVIDER", finalized="--"),
            PAYER_CONFIG,
        )
        self.assertEqual(errors, [])

    def test_optional_statuses_ignore_unavailable_finalized_date(self):
        for status in (
            "PENDING",
            "ACKNOWLEDGED",
            "RETURNED-TO-PROVIDER",
            "REJECTED",
            "RECEIVED",
        ):
            with self.subTest(status=status):
                result = claim_result(status=status, finalized="--")
                result["_field_states"]["Finalized Date"] = FIELD_FAILED
                result["_field_errors"]["Finalized Date"] = "selector unavailable"
                self.assertEqual(
                    validate_claim_result(result, PAYER_CONFIG),
                    [],
                )

    def test_failed_denial_lookup_is_not_valid_placeholder(self):
        result = claim_result(status="Partially Paid")
        result["_denial_required"] = True
        result["_denial_state"] = FIELD_FAILED
        result["_denial_error"] = "remittance timed out"
        errors = validate_claim_result(result, PAYER_CONFIG)
        self.assertTrue(
            any("remittance timed out" in error for error in errors),
            errors,
        )

    def test_explicit_no_adjustment_is_resolved(self):
        result = claim_result(status="Partially Paid")
        result["_denial_required"] = True
        result["_denial_state"] = FIELD_ABSENT
        self.assertEqual(validate_claim_result(result, PAYER_CONFIG), [])

    def test_failed_field_read_marks_claim_unreliable(self):
        result = claim_result()
        result["_field_states"]["Check Number"] = FIELD_FAILED
        result["_field_errors"]["Check Number"] = "selector timed out"
        errors = validate_claim_result(result, PAYER_CONFIG)
        self.assertTrue(
            any("Check Number: selector timed out" in error for error in errors),
            errors,
        )

    def test_negative_adjustment_amounts_are_parseable(self):
        result = claim_result(status="ADJUSTED")
        result["Billed Amount"] = "-$35.30"
        result["Paid Amount"] = "-$35.30"
        self.assertEqual(validate_claim_result(result, PAYER_CONFIG), [])

    def test_multi_claim_count_mismatch_fails_row(self):
        errors = validate_claim_results(
            [claim_result()], PAYER_CONFIG, expected_count=2,
        )
        self.assertTrue(
            any("claim count mismatch" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
