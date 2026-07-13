"""Validation models — output from DETERMINISTIC Python validation.

NO LLM is used for validation. Pure Python checks only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    """A single validation issue found by deterministic checks."""

    field: str = Field(..., description="The extraction field with an issue")
    check: str = Field(..., description="Which validation check failed")
    severity: Literal["INFO", "WARNING", "ERROR"] = Field(
        ..., description="Issue severity"
    )
    message: str = Field(..., description="Human-readable description")


class ValidationSummary(BaseModel):
    """Overall validation result. Pure Python, no LLM.

    States:
    - SUPPORTED: All fields have valid evidence citations
    - UNSUPPORTED: One or more fields lack evidence
    - INSUFFICIENT_EVIDENCE: Not enough evidence collected
    """

    status: Literal["SUPPORTED", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE"] = Field(
        ..., description="Overall validation status"
    )
    score: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence score (0-1) based on check pass rate",
    )
    fields_checked: int = Field(default=0, description="Total fields checked")
    fields_supported: int = Field(default=0, description="Fields with valid evidence")
    issues: list[ValidationIssue] = Field(
        default_factory=list, description="All issues found"
    )

    @property
    def needs_verification(self) -> bool:
        """Whether this result should be escalated to browser verification."""
        return self.status in ("UNSUPPORTED", "INSUFFICIENT_EVIDENCE")

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARNING")
