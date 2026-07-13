"""Documentation discovery models — inventory, canonical map, and stats.

Architecture:
  Discovery → DocumentationInventory → CanonicalDocument → DocumentationMap
                                                              ↓
                                                    EvidenceCollection

DocumentationInventory: every page found (debug/ranking only, not consumed downstream)
DocumentationMap:       canonical pages only, one primary per slot + alternatives
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────
#  Discovery statistics
# ────────────────────────────────────────────────────────────


class DiscoveryStats(BaseModel):
    """Detailed statistics about the discovery process."""

    queries: int = Field(default=0, description="Total search queries executed")
    urls_discovered: int = Field(default=0, description="Raw URLs before any filtering")
    official_urls: int = Field(default=0, description="URLs on the official domain")
    non_official_filtered: int = Field(
        default=0, description="URLs rejected because not on official domain"
    )
    blog_community_filtered: int = Field(
        default=0, description="URLs rejected by strict classifier (blog, forum, etc.)"
    )
    third_party_sdk_filtered: int = Field(
        default=0, description="Third-party SDKs rejected"
    )
    duplicates_removed: int = Field(default=0, description="Duplicate URLs removed")
    canonical_selected: int = Field(
        default=0, description="Primary documents selected for slots"
    )


# ────────────────────────────────────────────────────────────
#  Documentation inventory (debug only — not consumed downstream)
# ────────────────────────────────────────────────────────────


SupportedPageType = Literal[
    "authentication",
    "api_reference",
    "pricing",
    "sdk",
    "mcp",
    "developer_portal",
    "webhooks",
    "homepage",
]


class DocInventoryItem(BaseModel):
    """A single page discovered during the crawl.

    Every page found — including those later rejected — is recorded here
    so that debugging and ranking decisions are fully transparent.
    """

    url: str = Field(..., description="Full URL of the discovered page")
    page_type: SupportedPageType | None = Field(
        default=None,
        description="Classified slot. None means 'unclassified / will be rejected'",
    )
    title: str = Field(
        default="", description="Page title from the search result or HTML"
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Deterministic confidence score"
    )
    confidence_reason: list[str] = Field(
        default_factory=list,
        description="Human-readable breakdown, e.g. ['official_docs: +0.40', 'developer_domain: +0.25']",
    )
    discovered_from: Literal["llms_txt", "domain_probe", "tavily_search"] = Field(
        default="tavily_search",
        description="How this URL was discovered",
    )
    is_official: bool = Field(
        default=False, description="Whether URL is on the official domain"
    )


class DocumentationInventory(BaseModel):
    """Full inventory of every documentation page discovered.

    This model is NOT consumed downstream by extraction or validation.
    It exists purely for debugging, transparency, and ranking analysis.
    """

    app_name: str = Field(..., description="Application name")
    official_domain: str = Field(..., description="Verified official root domain")
    items: list[DocInventoryItem] = Field(
        default_factory=list,
        description="Every discovered page (including rejected ones)",
    )
    stats: DiscoveryStats | None = Field(
        default=None, description="Discovery statistics"
    )


# ────────────────────────────────────────────────────────────
#  Canonical document (the selected best page for a slot)
# ────────────────────────────────────────────────────────────


class CanonicalDocument(BaseModel):
    """A single canonical documentation page selected for a slot.

    Only the highest-confidence page becomes a CanonicalDocument.
    Every other page for the same slot goes into DocumentSlot.alternatives.
    """

    url: str = Field(..., description="The canonical URL")
    title: str = Field(default="", description="Page title")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Deterministic confidence score"
    )
    confidence_reason: list[str] = Field(
        default_factory=list,
        description="Human-readable confidence breakdown",
    )
    is_official: bool = Field(
        default=False, description="Whether URL is on the official domain"
    )
    discovered_from: Literal["llms_txt", "domain_probe", "tavily_search"] = Field(
        default="tavily_search",
        description="How this URL was discovered",
    )


class DocumentSlot(BaseModel):
    """A single documentation slot with one primary and zero or more alternatives."""

    primary: CanonicalDocument | None = Field(
        default=None,
        description="The single best canonical page for this slot",
    )
    alternatives: list[CanonicalDocument] = Field(
        default_factory=list,
        description="Additional pages for this slot (fallback if primary cannot answer)",
    )

    @property
    def url_count(self) -> int:
        return (1 if self.primary else 0) + len(self.alternatives)

    @property
    def all_urls(self) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        if self.primary:
            docs.append(self.primary)
        docs.extend(self.alternatives)
        return docs


# ────────────────────────────────────────────────────────────
#  Documentation map — output of discovery, input to collection
# ────────────────────────────────────────────────────────────


class DocumentationMap(BaseModel):
    """Canonical documentation map for an application.

    Output of DiscoveryService. Input to EvidenceCollector.

    Each slot contains one primary canonical document (the best page).
    Additional pages are kept as alternatives for fallback.
    The general_docs bucket has been removed — every slot is typed.
    """

    app_name: str = Field(..., description="Application name")
    official_domain: str = Field(..., description="Verified official root domain")

    # ── Homepage (single object, not a list) ──
    homepage: CanonicalDocument | None = Field(
        default=None,
        description="Primary homepage URL",
    )

    # ── Typed documentation slots (primary + alternatives) ──
    authentication: DocumentSlot = Field(
        default_factory=DocumentSlot,
        description="OAuth, API keys, SSO, security documentation",
    )
    api_reference: DocumentSlot = Field(
        default_factory=DocumentSlot,
        description="API reference, OpenAPI/Swagger, GraphQL, REST docs",
    )
    pricing: DocumentSlot = Field(
        default_factory=DocumentSlot,
        description="Pricing, plans, enterprise, free trial pages",
    )
    sdk: DocumentSlot | None = Field(
        default=None,
        description="Official SDKs, client libraries, wrappers (nullable for backward compat)",
    )
    mcp: DocumentSlot = Field(
        default_factory=DocumentSlot,
        description="MCP server documentation or repository",
    )
    developer_portal: DocumentSlot = Field(
        default_factory=DocumentSlot,
        description="Developer portal, getting-started, quickstart docs",
    )
    webhooks: DocumentSlot | None = Field(
        default=None,
        description="Webhook documentation and event guides (nullable for backward compat)",
    )

    # ── llms.txt (highest priority, set directly when found) ──
    llms_txt: CanonicalDocument | None = Field(
        default=None,
        description="llms.txt or llms-full.txt index URL, if found",
    )

    # ── Debugging ──
    inventory: DocumentationInventory | None = Field(
        default=None,
        description="Full crawl inventory — debug only, not consumed downstream",
    )
    stats: DiscoveryStats | None = Field(
        default=None,
        description="Discovery process statistics",
    )

    # ────────────────────────────────────────────────────────
    #  Backward-compatible accessors
    # ────────────────────────────────────────────────────────

    _ALL_SLOT_NAMES = [
        "llms_txt",
        "homepage",
        "authentication",
        "api_reference",
        "pricing",
        "developer_portal",
        "sdk",
        "webhooks",
        "mcp",
    ]

    @property
    def all_urls(self) -> list[CanonicalDocument]:
        """All primary documents across all slots, in priority order."""
        result: list[CanonicalDocument] = []
        if self.llms_txt:
            result.append(self.llms_txt)
        if self.homepage:
            result.append(self.homepage)
        for name in [
            "authentication",
            "api_reference",
            "pricing",
            "developer_portal",
            "sdk",
            "webhooks",
            "mcp",
        ]:
            slot: DocumentSlot | None = getattr(self, name, None)
            if slot and slot.primary:
                result.append(slot.primary)
        return result

    @property
    def url_count(self) -> int:
        """Total number of primary documents."""
        return len(self.all_urls)

    @property
    def official_urls(self) -> list[str]:
        """URLs on the official domain only."""
        return [u.url for u in self.all_urls if u.is_official]

    @property
    def official_url_count(self) -> int:
        """Count of official-domain URLs."""
        return sum(1 for u in self.all_urls if u.is_official)

    @property
    def api_docs_urls(self) -> list[str]:
        """URLs for API documentation."""
        urls: list[str] = []
        if self.api_reference.primary:
            urls.append(self.api_reference.primary.url)
        if self.developer_portal.primary:
            urls.append(self.developer_portal.primary.url)
        return urls

    @property
    def pricing_urls(self) -> list[str]:
        """URLs for pricing pages."""
        if self.pricing.primary:
            return [self.pricing.primary.url]
        return []

    @property
    def mcp_urls(self) -> list[str]:
        """URLs for MCP pages."""
        if self.mcp.primary:
            return [self.mcp.primary.url]
        return []
