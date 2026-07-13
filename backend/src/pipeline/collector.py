"""Evidence collector — fetches content from discovered URLs using Firecrawl.

Responsibilities:
- Fetch content from DocumentationMap's typed URL slots via Firecrawl
- Fallback to httpx + trafilatura
- Split content into semantic chunks (target 400-700 tokens)
- Score each chunk by relevance quality
- Deduplicate evidence

Never summarizes. Never reasons. Just collects raw evidence.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx
import trafilatura
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.console import Console

from models.discovery import CanonicalDocument, DocumentationMap, DocumentSlot
from models.evidence import Evidence
from src.config import get_settings

console = Console()


# ────────────────────────────────────────────────────────────
#  Token estimation
# ────────────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


# ────────────────────────────────────────────────────────────
#  Semantic chunking
# ────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _split_into_semantic_chunks(
    markdown: str,
    min_tokens: int = 300,
    max_tokens: int = 800,
) -> list[tuple[str, str]]:
    """Split markdown into semantically coherent chunks.

    Returns list of (section_path, content) tuples.

    Strategy:
    1. Split on markdown headings (##, ###, etc.)
    2. If a section is too long, split on paragraph breaks
    3. If a section is too short, merge with the next section
    4. Target: 400-700 tokens per chunk
    """
    if not markdown or not markdown.strip():
        return []

    # Find all heading positions
    headings = list(_HEADING_RE.finditer(markdown))

    if not headings:
        # No headings — split on paragraph breaks
        return _split_by_paragraphs(markdown, "", min_tokens, max_tokens)

    chunks: list[tuple[str, str]] = []
    heading_stack: list[str] = []

    for i, match in enumerate(headings):
        level = len(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(markdown)
        content = markdown[start:end].strip()

        if not content:
            continue

        # Update heading stack for section_path
        while len(heading_stack) >= level:
            heading_stack.pop()
        heading_stack.append(title)
        section_path = " > ".join(heading_stack)

        tokens = _estimate_tokens(content)
        if tokens <= max_tokens:
            if tokens >= min_tokens:
                chunks.append((section_path, content))
            else:
                # Too short — try to merge with next section
                chunks.append((section_path, content))
        else:
            # Too long — split by paragraphs
            sub_chunks = _split_by_paragraphs(
                content, section_path, min_tokens, max_tokens
            )
            chunks.extend(sub_chunks)

    # Merge consecutive short chunks
    merged = _merge_short_chunks(chunks, min_tokens, max_tokens)
    return merged


def _split_by_paragraphs(
    text: str,
    section_path: str,
    min_tokens: int,
    max_tokens: int,
) -> list[tuple[str, str]]:
    """Split text into paragraph-based chunks within token bounds."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [(section_path, text)] if text.strip() else []

    chunks: list[tuple[str, str]] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)
        if current_tokens + para_tokens > max_tokens and current_parts:
            chunks.append((section_path, "\n\n".join(current_parts)))
            current_parts = [para]
            current_tokens = para_tokens
        else:
            current_parts.append(para)
            current_tokens += para_tokens

    if current_parts:
        chunks.append((section_path, "\n\n".join(current_parts)))

    return chunks


def _merge_short_chunks(
    chunks: list[tuple[str, str]],
    min_tokens: int,
    max_tokens: int,
) -> list[tuple[str, str]]:
    """Merge consecutive chunks that are below min_tokens."""
    if not chunks:
        return []

    merged: list[tuple[str, str]] = []
    current_path, current_content = chunks[0]
    current_tokens = _estimate_tokens(current_content)

    for path, content in chunks[1:]:
        content_tokens = _estimate_tokens(content)
        if (
            current_tokens < min_tokens
            and current_tokens + content_tokens <= max_tokens
        ):
            # Merge
            current_content += f"\n\n{content}"
            current_tokens += content_tokens
            if path and path != current_path:
                current_path = path  # Take the more specific path
        else:
            merged.append((current_path, current_content))
            current_path = path
            current_content = content
            current_tokens = content_tokens

    merged.append((current_path, current_content))
    return merged


# ────────────────────────────────────────────────────────────
#  Quality scoring
# ────────────────────────────────────────────────────────────

# Keywords that signal high-value content for our extraction targets
_AUTH_KEYWORDS = {
    "oauth",
    "api key",
    "bearer",
    "token",
    "authentication",
    "authorization",
    "api_key",
    "jwt",
    "saml",
    "sso",
}
_PRICING_KEYWORDS = {
    "pricing",
    "free tier",
    "trial",
    "sign up",
    "signup",
    "plans",
    "enterprise",
    "contact sales",
}
_API_KEYWORDS = {
    "rest api",
    "graphql",
    "endpoint",
    "openapi",
    "swagger",
    "sdk",
    "webhook",
    "api reference",
}
_MCP_KEYWORDS = {"mcp", "model context protocol", "mcp server"}


def _compute_quality_score(
    content: str,
    source_type: str,
    is_official: bool,
    discovery_confidence: float,
) -> float:
    """Compute quality score for an evidence chunk.

    Factors:
    - Discovery confidence (from DocumentationMap)
    - Official domain bonus
    - Keyword density for target extraction fields
    - Source type priority
    """
    content_lower = content.lower()

    # Base from discovery confidence
    score = discovery_confidence * 0.4

    # Official domain bonus
    if is_official:
        score += 0.2

    # Keyword density bonus (max 0.3)
    keyword_hits = 0
    for kw_set in [_AUTH_KEYWORDS, _PRICING_KEYWORDS, _API_KEYWORDS, _MCP_KEYWORDS]:
        if any(kw in content_lower for kw in kw_set):
            keyword_hits += 1
    score += min(0.3, keyword_hits * 0.1)

    # Source type priority bonus (max 0.1)
    high_value_types = {"auth_docs", "api_reference", "pricing_page", "llms_txt"}
    if source_type in high_value_types:
        score += 0.1

    return min(1.0, round(score, 2))


# ────────────────────────────────────────────────────────────
#  Fetching
# ────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10), reraise=True)
async def _fetch_firecrawl(url: str) -> dict | None:
    """Fetch a URL using Firecrawl Scrape API."""
    settings = get_settings()
    if not settings.firecrawl_api_key:
        return None

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "waitFor": 3000,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                return data.get("data", {})
    return None


async def _fetch_fallback(url: str) -> dict | None:
    """Fallback: httpx + trafilatura for content extraction."""
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None

            content = trafilatura.extract(
                resp.text,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
            )

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(resp.text, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else ""

            if content and len(content.strip()) > 50:
                return {
                    "markdown": content,
                    "metadata": {"title": title, "sourceURL": url},
                }
    except Exception:
        pass
    return None


async def _fetch_url(url: str) -> dict | None:
    """Fetch a URL — Firecrawl first, fallback second."""
    try:
        result = await _fetch_firecrawl(url)
        if result:
            return result
    except Exception:
        pass
    return await _fetch_fallback(url)


def _url_domain(url: str) -> str:
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    return (parsed.netloc or "").lower()


# ────────────────────────────────────────────────────────────
#  Source type mapping
# ────────────────────────────────────────────────────────────

_SLOT_TO_SOURCE_TYPE = {
    "homepage": "official_docs",
    "authentication": "auth_docs",
    "api_reference": "api_reference",
    "pricing": "pricing_page",
    "developer_portal": "developer_portal",
    "sdk": "general_docs",
    "webhooks": "general_docs",
    "mcp": "mcp_registry",
    "llms_txt": "llms_txt",
}


# ────────────────────────────────────────────────────────────
#  Main collector
# ────────────────────────────────────────────────────────────


async def collect_evidence(doc_map: DocumentationMap) -> list[Evidence]:
    """Collect evidence from canonical documents only.

    Input: DocumentationMap (from DiscoveryService)
    Output: list[Evidence]

    Only processes primary documents from each slot.
    Falls back to alternatives[0] if a slot has no primary.
    No blog/community/third-party content reaches this stage.
    """
    evidence_list: list[Evidence] = []
    seen_urls: set[str] = set()

    # Build prioritized URL list from canonical documents
    # Priority: llms_txt > homepage > authentication > api_reference > pricing > dev_portal > sdk > webhooks > mcp
    slot_urls: list[tuple[CanonicalDocument, str]] = []

    if doc_map.llms_txt:
        slot_urls.append((doc_map.llms_txt, "llms_txt"))

    if doc_map.homepage:
        slot_urls.append((doc_map.homepage, "homepage"))

    # Slot-based: primary first, fallback to alternatives[0]
    _SLOT_ORDER = [
        "authentication",
        "api_reference",
        "pricing",
        "developer_portal",
        "sdk",
        "webhooks",
        "mcp",
    ]
    for slot_name in _SLOT_ORDER:
        slot: DocumentSlot = getattr(doc_map, slot_name)
        if slot.primary:
            slot_urls.append((slot.primary, slot_name))
        elif slot.alternatives:
            # Fallback: use first alternative when no primary
            slot_urls.append((slot.alternatives[0], slot_name))

    # Limit to prevent rate limiting (max 5 URLs per app)
    urls_to_fetch = slot_urls[:5]

    # Fetch in parallel with limited concurrency
    semaphore = asyncio.Semaphore(3)

    async def _fetch_and_chunk(
        canonical_url: CanonicalDocument, slot_name: str
    ) -> list[Evidence]:
        async with semaphore:
            url = canonical_url.url
            if url in seen_urls:
                return []
            seen_urls.add(url)

            result = await _fetch_url(url)
            if result is None:
                return []

            content = result.get("markdown", "")
            metadata = result.get("metadata", {})

            if not content or len(content.strip()) < 50:
                return []

            source_url = metadata.get("sourceURL", url)
            domain = _url_domain(source_url)
            page_title = metadata.get("title", canonical_url.title)
            source_type = _SLOT_TO_SOURCE_TYPE.get(slot_name, "official_docs")

            # Semantic chunking
            chunks = _split_into_semantic_chunks(content)

            if not chunks:
                # Fallback: treat entire content as one chunk
                token_count = _estimate_tokens(content[:5000])
                quality = _compute_quality_score(
                    content[:2000],
                    source_type,
                    canonical_url.is_official,
                    canonical_url.confidence,
                )
                return [
                    Evidence(
                        source_type=source_type,
                        url=source_url,
                        domain=domain,
                        page_title=page_title,
                        content=content[:5000],
                        is_official=canonical_url.is_official,
                        section_path="",
                        chunk_index=0,
                        token_count=token_count,
                        quality_score=quality,
                    )
                ]

            evidence_chunks: list[Evidence] = []
            for idx, (section_path, chunk_content) in enumerate(chunks):
                token_count = _estimate_tokens(chunk_content)
                quality = _compute_quality_score(
                    chunk_content,
                    source_type,
                    canonical_url.is_official,
                    canonical_url.confidence,
                )
                evidence_chunks.append(
                    Evidence(
                        source_type=source_type,
                        url=source_url,
                        domain=domain,
                        page_title=page_title,
                        content=chunk_content,
                        is_official=canonical_url.is_official,
                        section_path=section_path,
                        chunk_index=idx,
                        token_count=token_count,
                        quality_score=quality,
                    )
                )
            return evidence_chunks

    tasks = [_fetch_and_chunk(u, slot) for u, slot in urls_to_fetch]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, list):
            evidence_list.extend(result)

    # Sort by quality_score desc — highest quality chunks first
    evidence_list.sort(key=lambda e: e.quality_score, reverse=True)

    if not evidence_list:
        console.print(f"[yellow]No evidence collected for {doc_map.app_name}[/yellow]")

    return evidence_list
