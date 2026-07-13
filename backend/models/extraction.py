"""Extraction models — structured output from the LLM extraction chain.

The LLM is ONLY responsible for semantic extraction.
Every field value must cite evidence IDs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FieldValue(BaseModel):
    """A single extracted field value with evidence traceability."""

    value: Any = Field(..., description="The extracted value")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="IDs of evidence supporting this value",
    )


class Extraction(BaseModel):
    """Complete extraction result for one application.

    Output of ExtractionChain. Input to Validator.
    """

    description: FieldValue = Field(
        ..., description="One-line description of what the app does"
    )
    category: FieldValue = Field(..., description="App category")
    auth_methods: FieldValue = Field(
        ...,
        description="Authentication methods (OAuth2, API Key, Basic, Bearer Token, JWT, etc.)",
    )
    self_serve: FieldValue = Field(
        ...,
        description="Access model: Self-serve (free/trial/paid) or Gated (admin/partner/sales)",
    )
    api_surface: FieldValue = Field(
        ..., description="API protocols, docs, GraphQL, OpenAPI, SDKs"
    )
    api_breadth: FieldValue = Field(
        ..., description="API breadth: Broad, Moderate, Narrow, Minimal, UNKNOWN"
    )
    mcp: FieldValue = Field(
        ..., description="MCP server: Official MCP, Community MCP, No known MCP, UNKNOWN"
    )
    buildability: FieldValue = Field(
        ..., description="Buildability verdict: HIGH, MEDIUM, LOW, BLOCKED"
    )
    blocker: FieldValue = Field(
        ..., description="Primary blocker for toolkit creation, or None"
    )
