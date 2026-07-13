"""Application metadata — immutable input from apps.csv."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AppMetadata(BaseModel):
    """Immutable metadata for a SaaS application to research."""

    id: int = Field(..., description="Unique identifier (1-100)")
    name: str = Field(..., description="Application name")
    website: str = Field(..., description="Primary website or docs URL hint")
    category_hint: str | None = Field(
        default=None,
        description="Category hint from the assignment",
    )

    @property
    def slug(self) -> str:
        """URL-safe slug for file storage."""
        return (
            self.name.lower()
            .replace(" ", "_")
            .replace(".", "")
            .replace("(", "")
            .replace(")", "")
        )
