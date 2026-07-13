"""Deterministic validator — pure Python validation checks.

NO LLM CALLS. Only Python logic.

Checks:
1. Every extraction field cites at least one valid evidence ID
2. Cited evidence IDs exist in the evidence list
3. Extracted values are non-empty and non-UNKNOWN
4. Auth methods contain recognized values
5. Official domain evidence is present
"""

from __future__ import annotations

from models.evidence import Evidence
from models.extraction import Extraction, FieldValue
from models.validation import ValidationIssue, ValidationSummary

VALID_AUTH_METHODS = frozenset([
    "oauth2", "oauth", "api key", "api_key", "apikey", "bearer",
    "bearer token", "jwt", "basic", "basic auth", "saml", "hmac",
    "session", "cookie", "token", "personal access token", "pat",
    "api token", "access token", "service account",
])

VALID_SELF_SERVE_VALUES = frozenset([
    "self-serve", "self serve", "free", "trial", "freemium", "paid",
    "gated", "contact sales", "enterprise", "partner", "admin approval",
    "request access", "invite only", "waitlist",
])

VALID_API_BREADTH = frozenset(["broad", "moderate", "narrow", "minimal", "unknown"])

VALID_MCP_VALUES = frozenset([
    "official mcp", "community mcp", "no known mcp", "unknown",
])

VALID_BUILDABILITY = frozenset(["high", "medium", "low", "blocked"])


def _check_field_has_citation(
    field_name: str,
    field_value: FieldValue,
    evidence_ids: set[str],
    issues: list[ValidationIssue],
) -> bool:
    """Check that a field cites existing evidence."""
    if not field_value.evidence_ids:
        issues.append(ValidationIssue(
            field=field_name,
            check="citation_exists",
            severity="WARNING",
            message=f"Field '{field_name}' has no evidence citations",
        ))
        return False

    valid_citations = [eid for eid in field_value.evidence_ids if eid in evidence_ids]
    if not valid_citations:
        issues.append(ValidationIssue(
            field=field_name,
            check="citation_valid",
            severity="ERROR",
            message=f"Field '{field_name}' cites non-existent evidence IDs: {field_value.evidence_ids}",
        ))
        return False

    return True


def _check_field_not_unknown(
    field_name: str,
    field_value: FieldValue,
    issues: list[ValidationIssue],
) -> bool:
    """Check that a field is not UNKNOWN."""
    val = str(field_value.value).strip().lower()
    if val in ("unknown", "", "none", "n/a"):
        issues.append(ValidationIssue(
            field=field_name,
            check="not_unknown",
            severity="WARNING",
            message=f"Field '{field_name}' is UNKNOWN — needs more evidence",
        ))
        return False
    return True


def _check_auth_methods(
    extraction: Extraction,
    issues: list[ValidationIssue],
) -> bool:
    """Check that auth methods contain recognized values."""
    val = str(extraction.auth_methods.value).lower()
    if val in ("unknown", "", "none"):
        return False

    found = any(method in val for method in VALID_AUTH_METHODS)
    if not found:
        issues.append(ValidationIssue(
            field="auth_methods",
            check="recognized_auth",
            severity="WARNING",
            message=f"Auth methods '{extraction.auth_methods.value}' — no recognized auth pattern found",
        ))
    return found


def _check_has_official_evidence(
    evidence_list: list[Evidence],
    issues: list[ValidationIssue],
) -> bool:
    """Check that at least some evidence comes from official sources."""
    official = [e for e in evidence_list if e.is_official]
    if not official:
        issues.append(ValidationIssue(
            field="evidence",
            check="official_source",
            severity="WARNING",
            message="No evidence from official domain — results may be less reliable",
        ))
        return False
    return True


def validate_extraction(
    extraction: Extraction,
    evidence_list: list[Evidence],
) -> ValidationSummary:
    """Run all deterministic validation checks.

    Input: Extraction + list[Evidence]
    Output: ValidationSummary

    Pure Python. No LLM. No exceptions.
    """
    issues: list[ValidationIssue] = []
    evidence_ids = {e.id for e in evidence_list}

    # Track pass/fail per field
    fields_to_check = [
        "description", "category", "auth_methods", "self_serve",
        "api_surface", "api_breadth", "mcp", "buildability",
    ]

    fields_supported = 0
    fields_checked = len(fields_to_check)

    for field_name in fields_to_check:
        field_value: FieldValue = getattr(extraction, field_name)
        citation_ok = _check_field_has_citation(field_name, field_value, evidence_ids, issues)
        not_unknown = _check_field_not_unknown(field_name, field_value, issues)

        if citation_ok and not_unknown:
            fields_supported += 1

    # Additional checks
    _check_auth_methods(extraction, issues)
    _check_has_official_evidence(evidence_list, issues)

    # Calculate score
    score = fields_supported / max(fields_checked, 1)

    # Determine status
    if score >= 0.75:
        status = "SUPPORTED"
    elif score >= 0.5:
        status = "UNSUPPORTED"
    else:
        status = "INSUFFICIENT_EVIDENCE"

    return ValidationSummary(
        status=status,
        score=round(score, 2),
        fields_checked=fields_checked,
        fields_supported=fields_supported,
        issues=issues,
    )
