"""Structured extraction outcomes and strict claim-result validation."""
from dataclasses import dataclass


FIELD_FOUND = "found"
FIELD_ABSENT = "absent"
FIELD_FAILED = "failed"

_PLACEHOLDERS = frozenset({"", "--", "n/a", "na", "none", "null"})
_DEFAULT_FINALIZED_OPTIONAL_STATUSES = frozenset({
    "PENDING",
    "ACKNOWLEDGED",
    "RETURNED-TO-PROVIDER",
    "REJECTED",
    "RECEIVED",
})
_CORE_FIELDS = ("Claim ID", "Claim Status", "Billed Amount", "Paid Amount")


@dataclass(frozen=True)
class FieldRead:
    """One portal-field read with a value, outcome, and optional error."""

    value: str = "--"
    state: str = FIELD_FAILED
    error: str = ""

    @classmethod
    def found(cls, value):
        return cls(value=str(value).strip(), state=FIELD_FOUND)

    @classmethod
    def absent(cls, value="--"):
        text = str(value or "").strip()
        return cls(value=text or "--", state=FIELD_ABSENT)

    @classmethod
    def failed(cls, error, value="--"):
        return cls(value=value, state=FIELD_FAILED, error=str(error or "read failed"))


def is_placeholder(value):
    """Return True when ``value`` is empty or a portal placeholder."""
    return str(value or "").strip().lower() in _PLACEHOLDERS


def _amount_is_parseable(value):
    if is_placeholder(value):
        return False
    cleaned = (
        str(value).strip()
        .replace("$", "")
        .replace(",", "")
        .replace("(", "-")
        .replace(")", "")
    )
    try:
        float(cleaned)
        return True
    except (TypeError, ValueError):
        return False


def _validation_policy(payer_config):
    policy = dict((payer_config or {}).get("result_validation", {}))
    optional = policy.get(
        "finalized_optional_statuses",
        _DEFAULT_FINALIZED_OPTIONAL_STATUSES,
    )
    policy["finalized_optional_statuses"] = {
        str(status).strip().upper() for status in optional
    }
    return policy


def validate_claim_result(claim_data, payer_config=None):
    """Return reliability errors for one extracted claim result."""
    if not claim_data:
        return ["claim result is empty"]

    errors = [str(err) for err in claim_data.get("_extraction_errors", []) if err]
    field_states = claim_data.get("_field_states", {})
    field_errors = claim_data.get("_field_errors", {})
    status = str(claim_data.get("Claim Status") or "").strip()
    status_upper = status.upper()
    policy = _validation_policy(payer_config)
    finalized_optional = (
        status_upper in policy["finalized_optional_statuses"]
        and is_placeholder(claim_data.get("Finalized Date"))
    )

    for field, state in field_states.items():
        if state != FIELD_FAILED:
            continue
        if field == "Finalized Date" and finalized_optional:
            continue
        detail = field_errors.get(field) or "portal field read failed"
        errors.append(f"{field}: {detail}")

    for field in _CORE_FIELDS:
        if is_placeholder(claim_data.get(field)):
            errors.append(f"{field}: required value is missing")

    for field in ("Billed Amount", "Paid Amount"):
        value = claim_data.get(field)
        if not is_placeholder(value) and not _amount_is_parseable(value):
            errors.append(f"{field}: invalid amount {value!r}")

    if status_upper in {"ERROR", "CRITICAL ERROR"}:
        errors.append(f"Claim Status: extraction returned {status}")
    if "COULD NOT BE IDENTIFIED" in status_upper:
        errors.append(f"Claim Status: {status}")

    if (
        status_upper
        and status_upper not in policy["finalized_optional_statuses"]
        and is_placeholder(claim_data.get("Finalized Date"))
    ):
        errors.append(
            f"Finalized Date: required for adjudicated status {status!r}"
        )

    if claim_data.get("_denial_required"):
        denial_state = claim_data.get("_denial_state")
        if denial_state == FIELD_FAILED:
            detail = claim_data.get("_denial_error") or "remittance lookup failed"
            errors.append(f"Denial Reason: {detail}")
        elif denial_state not in {FIELD_FOUND, FIELD_ABSENT}:
            errors.append("Denial Reason: lookup outcome was not recorded")

    return _dedupe(errors)


def validate_claim_results(results, payer_config=None, expected_count=None):
    """Validate every claim and optionally enforce the expected result count."""
    errors = []
    if expected_count is not None and len(results or []) != int(expected_count):
        errors.append(
            f"claim count mismatch: expected {expected_count}, extracted {len(results or [])}"
        )
    if not results:
        errors.append("no claim results were extracted")
        return _dedupe(errors)

    for index, claim_data in enumerate(results, start=1):
        for error in validate_claim_result(claim_data, payer_config):
            errors.append(f"claim {index}: {error}")
    return _dedupe(errors)


def format_validation_errors(errors):
    """Format validation errors for the Excel ``LastError`` cell."""
    return " | ".join(_dedupe(str(error) for error in errors if error))


def _dedupe(values):
    seen = set()
    result = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
