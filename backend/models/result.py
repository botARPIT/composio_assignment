"""Result models — final composed output per application."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from models.app import AppMetadata
from models.discovery import DocumentationMap
from models.evidence import Evidence
from models.extraction import Extraction
from models.validation import ValidationSummary


class BrowserVerification(BaseModel):
    """Result of Playwright-based verification for disputed fields."""

    verified_fields: list[str] = Field(
        default_factory=list,
        description="Fields confirmed by browser verification",
    )
    corrections: dict[str, str] = Field(
        default_factory=dict,
        description="Field corrections discovered via browser",
    )
    screenshots: list[str] = Field(
        default_factory=list,
        description="Paths to verification screenshots",
    )
    notes: str = Field(default="", description="Verification notes")


class HumanReview(BaseModel):
    """Record of manual human review for ambiguous cases."""

    required: bool = Field(
        default=False, description="Whether human review was triggered"
    )
    reason: list[str] = Field(
        default_factory=list, description="Reasons why review was triggered"
    )
    status: Literal["pending", "in_progress", "completed"] = Field(
        default="pending", description="Review status"
    )
    reviewer: str | None = Field(default=None, description="Name of the human reviewer")
    reviewed_at: str | None = Field(
        default=None, description="ISO 8601 timestamp of review"
    )
    overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Field-level overrides with old/new values and reason. "
            "Format: {field_name: {old: ..., new: ..., reason: ...}}"
        ),
    )
    notes: str = Field(default="", description="Free-form review notes")


class ResearchResult(BaseModel):
    """Complete research result for one application.

    Immutable composition of all pipeline stage outputs.
    No stage mutates another stage's output.
    """

    app: AppMetadata
    documentation_map: DocumentationMap | None = Field(
        default=None, description="Discovery stage output"
    )
    evidence: list[Evidence] = Field(
        default_factory=list, description="Evidence collection output"
    )
    extraction: Extraction | None = Field(
        default=None, description="LLM extraction output"
    )
    validation: ValidationSummary | None = Field(
        default=None, description="Deterministic validation output"
    )
    browser_verification: BrowserVerification | None = Field(
        default=None, description="Browser verification (disputed fields only)"
    )
    pipeline_confidence: float = Field(
        default=0.0,
        description="Frozen confidence from validation.score — never mutated",
    )
    human_verified: bool = Field(
        default=False,
        description="Whether a human has reviewed this result",
    )
    final_status: Literal[
        "AUTO_ACCEPTED", "HUMAN_VERIFIED", "HUMAN_MODIFIED", "PENDING_REVIEW", "FAILED"
    ] = Field(default="AUTO_ACCEPTED", description="Final disposition of this result")
    human_review: HumanReview = Field(
        default_factory=HumanReview,
        description="Human review record (always present, check .required)",
    )

    @model_validator(mode="before")
    @classmethod
    def _handle_legacy_human_review(cls, data: Any) -> Any:
        """Coerce null human_review from old cached results to empty dict."""
        if isinstance(data, dict) and data.get("human_review") is None:
            data["human_review"] = HumanReview(required=False, status="pending")
        return data

    @property
    def is_complete(self) -> bool:
        """Whether all required pipeline stages completed."""
        return self.extraction is not None and self.validation is not None

    @property
    def needs_browser_verification(self) -> bool:
        """Whether this needs escalation to browser verification."""
        if self.validation is None:
            return True
        return self.validation.needs_verification

    def effective_value(self, field_name: str) -> Any:
        """Return the human override if it exists, otherwise the extraction value.

        Used only for presentation (HTML, report, CSV export).
        Analytics should read extraction and overrides separately.
        """
        if self.human_review and field_name in self.human_review.overrides:
            return self.human_review.overrides[field_name]["new"]
        if self.extraction:
            return getattr(self.extraction, field_name).value
        return None


def compute_review_reasons(result: ResearchResult) -> list[str]:
    """Deterministic rules to decide if human review is required.

    Pure function — called once, never mutated.
    """
    if result.extraction is None:
        return []

    reasons: list[str] = []

    if result.validation and result.validation.score < 0.90:
        reasons.append(
            f"Validation score {result.validation.score:.2f} below 0.90 threshold"
        )

    if result.extraction.self_serve.value == "UNKNOWN":
        reasons.append("Self-serve access model is UNKNOWN")

    raw_auth = result.extraction.auth_methods.value
    if not raw_auth or str(raw_auth).strip().upper() in ("UNKNOWN", "", "NONE", "[]"):
        reasons.append("Auth methods missing or UNKNOWN")

    if result.extraction.buildability.value == "LOW":
        reasons.append("Buildability confidence is LOW")

    if result.browser_verification and result.browser_verification.corrections:
        reasons.append("Browser verification found discrepancies")

    if len(result.evidence) < 3:
        reasons.append(
            f"Insufficient evidence ({len(result.evidence)} pieces, minimum 3)"
        )

    return reasons
