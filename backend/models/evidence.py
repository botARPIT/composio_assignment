"""Evidence model — traceable source data behind every extracted fact.

Supports semantic chunking: each Evidence is a section-level chunk
with quality scoring, token estimation, and heading hierarchy tracking.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """A single piece of evidence collected from a source.

    Immutable after creation. Every fact must trace back to evidence.

    Each Evidence represents a semantically coherent section (400-700 tokens)
    rather than an entire page, enabling priority-based context packing.
    """

    id: str = Field(
        default_factory=lambda: f"ev_{uuid.uuid4().hex[:12]}",
        description="Unique evidence identifier",
    )
    source_type: Literal[
        "official_docs",
        "api_reference",
        "developer_portal",
        "pricing_page",
        "github",
        "mcp_registry",
        "browser_verification",
        "llms_txt",
        "auth_docs",
        "general_docs",
    ] = Field(..., description="What kind of source this came from")
    url: str = Field(..., description="URL where evidence was found")
    domain: str = Field(default="", description="Domain of the URL")
    page_title: str = Field(default="", description="Title of the page")
    content: str = Field(..., description="Extracted text content (markdown)")
    is_official: bool = Field(
        default=False,
        description="Whether this is from the app's official domain",
    )
    retrieved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the evidence was collected",
    )

    # --- Semantic chunking fields ---
    section_path: str = Field(
        default="",
        description="Heading hierarchy, e.g. 'Authentication > OAuth2 > Scopes'",
    )
    chunk_index: int = Field(
        default=0,
        description="Position of this chunk within the source page (0-indexed)",
    )
    token_count: int = Field(
        default=0,
        description="Estimated token count for this chunk",
    )
    quality_score: float = Field(
        default=0.5,
        ge=0.0, le=1.0,
        description="Relevance quality: 1.0 = auth/API docs from official domain, 0.1 = generic third-party",
    )

    def truncated(self, max_chars: int = 2000) -> str:
        """Return content truncated to fit LLM context."""
        if len(self.content) <= max_chars:
            return self.content
        return self.content[:max_chars] + "\n... [truncated]"
