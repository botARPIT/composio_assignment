"""Result models — final composed output per application."""

from __future__ import annotations

from pydantic import BaseModel, Field

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

    reviewed: bool = Field(default=False)
    reviewer: str = Field(default="")
    corrected_fields: list[str] = Field(default_factory=list)
    notes: str = Field(default="")


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
    human_review: HumanReview | None = Field(
        default=None, description="Human review (last resort)"
    )

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

    @property
    def confidence_score(self) -> float:
        """Overall confidence — composed from validation + verification."""
        if self.validation is None:
            return 0.0
        base = self.validation.score
        if self.browser_verification and self.browser_verification.verified_fields:
            base = min(1.0, base + 0.15)
        if self.human_review and self.human_review.reviewed:
            base = min(1.0, base + 0.2)
        return round(base, 2)
