"""Regression tests for retried reads, claim identity, and reconciliation."""
import unittest
from unittest.mock import patch

from availity_app import automation
from availity_app.constants import REMIT_NOT_FOUND_DENIAL
from availity_app.result_validation import (
    FIELD_ABSENT,
    FIELD_FAILED,
    FIELD_FOUND,
)


class SequenceLocator:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs):
        return None

    def text_content(self, **_kwargs):
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        if isinstance(value, Exception):
            raise value
        return value


class FakeFrame:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, _selector):
        return self._locator


class StatusElement:
    def __init__(self, text, *, inside_results=False, visible=True):
        self.text = text
        self.inside_results = inside_results
        self.visible = visible

    def is_visible(self):
        return self.visible

    def evaluate(self, _script):
        return self.inside_results

    def text_content(self, **_kwargs):
        return self.text


class StatusCandidates:
    def __init__(self, elements=()):
        self.elements = list(elements)

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class StatusFrame:
    def __init__(self, selectors):
        self.selectors = selectors

    def locator(self, selector):
        return StatusCandidates(self.selectors.get(selector, ()))


class NavigationControl:
    def __init__(self, visible):
        self.visible = visible
        self.clicked = False

    @property
    def first(self):
        return self

    def or_(self, other):
        return self if self.visible else other

    def wait_for(self, **_kwargs):
        return None

    def is_visible(self):
        return self.visible

    def click(self, **_kwargs):
        self.clicked = True


class NavigationFrame:
    def __init__(self, member, tab):
        self.member = member
        self.tab = tab

    def locator(self, selector):
        return self.member if selector == "#subscriberMemberId" else self.tab


class NavigationPage:
    def __init__(self, frame, events):
        self.frame = frame
        self.events = events

    def frame_locator(self, _selector):
        return self.frame

    def goto(self, _url, **_kwargs):
        self.events.append("payer")
        return None


class ExtractionReliabilityTests(unittest.TestCase):
    def test_field_read_retries_then_succeeds(self):
        frame = FakeFrame(
            SequenceLocator([RuntimeError("not ready"), "TEST-CLAIM-1"]),
        )
        result = automation._read_selector_checked(
            frame,
            "#claim",
            "Claim ID",
            attempts=2,
            retry_delay=0,
        )
        self.assertEqual(result.state, FIELD_FOUND)
        self.assertEqual(result.value, "TEST-CLAIM-1")

    def test_visible_placeholder_is_explicitly_absent(self):
        frame = FakeFrame(SequenceLocator(["--", "--"]))
        result = automation._read_selector_checked(
            frame,
            "#finalized",
            "Finalized Date",
            attempts=2,
            retry_delay=0,
        )
        self.assertEqual(result.state, FIELD_ABSENT)
        self.assertEqual(result.value, "--")

    def test_unavailable_selector_is_failed_not_absent(self):
        frame = FakeFrame(
            SequenceLocator([RuntimeError("missing"), RuntimeError("missing")]),
        )
        result = automation._read_selector_checked(
            frame,
            "#check",
            "Check Number",
            attempts=2,
            retry_delay=0,
            quiet=True,
        )
        self.assertEqual(result.state, FIELD_FAILED)
        self.assertIn("missing", result.error)

    def test_claim_detail_status_ignores_results_table_badges(self):
        frame = StatusFrame({
            "#results-status": [
                StatusElement(
                    "RETURNED TO PROVIDER", inside_results=True,
                ),
            ],
            "#detail-status": [StatusElement("ADJUSTED")],
        })
        result = automation._read_claim_detail_status_checked(
            frame,
            ["#results-status", "#detail-status"],
            timeout=100,
            attempts=1,
            retry_delay=0,
        )
        self.assertEqual(result.state, FIELD_FOUND)
        self.assertEqual(result.value, "ADJUSTED")

    def test_results_badge_is_never_a_detail_status_fallback(self):
        frame = StatusFrame({
            "#results-status": [
                StatusElement(
                    "RETURNED TO PROVIDER", inside_results=True,
                ),
            ],
        })
        result = automation._read_claim_detail_status_checked(
            frame,
            ["#results-status"],
            timeout=50,
            attempts=1,
            retry_delay=0,
        )
        self.assertEqual(result.state, FIELD_FAILED)

    def test_duplicate_result_rows_count_as_one_claim(self):
        claim_ids = {
            "row-0": "CLAIM-A",
            "row-1": "CLAIM-A",
            "row-2": "CLAIM-B",
        }
        with (
            patch.object(
                automation,
                "_claim_row_locator_at_index",
                side_effect=lambda _iframe, index: f"row-{index}",
            ),
            patch.object(
                automation,
                "_peek_results_row_claim_number",
                side_effect=lambda _iframe, row: claim_ids[row],
            ),
        ):
            count = automation._count_unique_matching_claims(
                object(), [0, 1, 2],
            )
        self.assertEqual(count, 2)

    def test_distinct_claim_ids_keep_distinct_expected_count(self):
        with (
            patch.object(
                automation,
                "_claim_row_locator_at_index",
                side_effect=lambda _iframe, index: f"row-{index}",
            ),
            patch.object(
                automation,
                "_peek_results_row_claim_number",
                side_effect=lambda _iframe, row: row,
            ),
        ):
            count = automation._count_unique_matching_claims(
                object(), [0, 1],
            )
        self.assertEqual(count, 2)

    def test_claim_identity_waits_for_new_detail(self):
        frame = FakeFrame(
            SequenceLocator(["OLD-CLAIM", "OLD-CLAIM", "EXPECTED-CLAIM"]),
        )
        actual = automation._wait_for_claim_detail_identity(
            frame,
            "EXPECTED-CLAIM",
            payer_config={},
            timeout_ms=1000,
        )
        self.assertEqual(actual, "EXPECTED-CLAIM")

    def test_claim_identity_rejects_stale_detail(self):
        frame = FakeFrame(SequenceLocator(["OLD-CLAIM"]))
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            automation._wait_for_claim_detail_identity(
                frame,
                "EXPECTED-CLAIM",
                payer_config={},
                timeout_ms=100,
            )

    def test_results_finalized_date_fills_failed_detail_read(self):
        claim = {
            "Finalized Date": "--",
            "Check Date": "01/15/2026",
            "_field_states": {"Finalized Date": FIELD_FAILED},
            "_field_errors": {"Finalized Date": "detail timed out"},
        }
        automation._apply_finalized_date_priority(
            claim,
            "01/10/2026",
            {"uses_results_finalized_date": False, "reconcile_finalized_date_conflicts": True},
        )
        self.assertEqual(claim["Finalized Date"], "01/10/2026")
        self.assertEqual(
            claim["_field_states"]["Finalized Date"], FIELD_FOUND,
        )
        self.assertNotIn("Finalized Date", claim["_field_errors"])

    def test_conflicting_finalized_dates_are_flagged(self):
        claim = {
            "Finalized Date": "01/10/2026",
            "Check Date": "01/15/2026",
            "_field_states": {"Finalized Date": FIELD_FOUND},
            "_field_errors": {},
        }
        automation._apply_finalized_date_priority(
            claim,
            "01/11/2026",
            {"uses_results_finalized_date": False, "reconcile_finalized_date_conflicts": True},
        )
        self.assertEqual(claim["Finalized Date"], "01/10/2026")
        self.assertTrue(
            any(
                "Finalized Date conflict" in error
                for error in claim["_extraction_errors"]
            )
        )

    def test_molina_does_not_use_check_date_as_finalized_date(self):
        claim = {
            "Finalized Date": "--",
            "Check Date": "01/15/2026",
            "_field_states": {"Finalized Date": FIELD_ABSENT},
            "_field_errors": {},
        }
        automation._apply_finalized_date_priority(
            claim,
            None,
            {"uses_results_finalized_date": False, "reconcile_finalized_date_conflicts": True},
        )
        self.assertEqual(claim["Finalized Date"], "--")

    @patch.object(automation, "_reset_remit_search")
    @patch.object(
        automation,
        "_read_remit_adjustment_rows",
        return_value=[{
            "billed": "--",
            "paid": "--",
            "quantity": "",
            "date_range": "",
            "denial": "45 - Test adjustment",
            "raw_denial": "45 - Test adjustment",
        }],
    )
    @patch.object(automation, "_open_remit_claim_details")
    @patch.object(automation.time, "sleep")
    def test_remittance_failure_retries_and_recovers(
        self, _sleep, open_details, _read_rows, _close_page,
    ):
        iframe = FakeFrame(SequenceLocator(["unused"]))
        open_details.side_effect = [
            RuntimeError("temporary failure"),
            (object(), iframe),
        ]
        result = automation.extract_denial_reason_villagecaremax(
            object(),
            "TEST-CLAIM-1",
            payer_config={
                "result_validation": {
                    "remittance_attempts": 2,
                    "remittance_retry_delay_s": 0,
                },
            },
        )
        self.assertEqual(result.state, FIELD_FOUND)
        self.assertEqual(result.value, "Test adjustment")
        self.assertEqual(open_details.call_count, 2)

    @patch.object(
        automation,
        "_open_remit_claim_details",
        return_value=(None, None),
    )
    def test_explicit_remittance_not_found_is_resolved(self, _open_details):
        result = automation.extract_denial_reason_villagecaremax(
            object(),
            "TEST-CLAIM-1",
            payer_config={"result_validation": {"remittance_attempts": 1}},
        )
        self.assertEqual(result.state, FIELD_ABSENT)
        self.assertEqual(result.value, REMIT_NOT_FOUND_DENIAL)


if __name__ == "__main__":
    unittest.main()
