"""Discovery service — finds official documentation URLs.

Architecture (official-domain-first, Tavily as fallback):

  1. Official domain detection
  2. Developer subdomain probing
  3. llms.txt probing            ← highest priority
  4. Slot path probing           ← deterministic URL discovery
  5. Tavily search fallback      ← only for still-empty slots
  6. Documentation inventory     ← all pages (debug only)
  7. Canonical ranking           ← primary per slot
  8. DocumentationMap            ← canonical pages only

Stops searching a slot once a high-confidence (>=0.75) canonical page is found.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

import httpx
import orjson
from tavily import AsyncTavilyClient
from tenacity import retry, stop_after_attempt, wait_exponential
from rich.console import Console

from models.app import AppMetadata
from models.discovery import (
    CanonicalDocument,
    DiscoveryStats,
    DocInventoryItem,
    DocumentationInventory,
    DocumentationMap,
    DocumentSlot,
)
from src.config import CACHE_DIR, get_settings

console = Console()

# ────────────────────────────────────────────────────────────
#  Excluded domains & patterns
# ────────────────────────────────────────────────────────────

EXCLUDED_DOMAINS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "reddit.com",
        "stackoverflow.com",
        "stackexchange.com",
        "superuser.com",
        "medium.com",
        "dev.to",
        "fiverr.com",
        "wikipedia.org",
        "linkedin.com",
        "npmjs.com",
        "npmjs.org",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "g2.com",
        "capterra.com",
        "getapp.com",
        "trustradius.com",
        "zapier.com",
        "make.com",
        "n8n.io",
        "integromat.com",
        "tray.io",
        "workato.com",
    }
)

EXCLUDED_PATTERNS = re.compile(
    r"\.(?:blogspot|wordpress|wix|squarespace|weebly|tumblr)\.com|"
    r"(?:/go\?|/redirect\?|/out\?|goto\?url=|click\.)",
    re.IGNORECASE,
)

TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "ref",
        "source",
        "source_url",
        "mc_cid",
        "mc_eid",
        "trk",
        "pk_source",
        "pk_medium",
        "pk_campaign",
    }
)

# ────────────────────────────────────────────────────────────
#  Developer subdomain patterns
# ────────────────────────────────────────────────────────────

DEV_SUBDOMAIN_PATTERNS = [
    "developer.{domain}",
    "docs.{domain}",
    "api.{domain}",
    "help.{domain}",
    "developers.{domain}",
]

# ────────────────────────────────────────────────────────────
#  Slot path probing — deterministic URL patterns per slot
# ────────────────────────────────────────────────────────────

SLOT_PROBE_PATHS: dict[str, list[str]] = {
    "authentication": [
        "/docs/authentication",
        "/docs/auth",
        "/docs/oauth",
        "/authentication",
        "/auth",
        "/oauth",
        "/api-keys",
        "/docs/api-keys",
        "/docs/security",
        "/authentication/api-keys",
    ],
    "api_reference": [
        "/docs/api",
        "/docs/api-reference",
        "/docs/apis",
        "/docs/reference",
        "/api/reference",
        "/api/docs",
        "/reference",
        "/api",
        "/openapi",
        "/swagger",
        "/graphql",
        "/api/v1",
        "/docs/rest",
        "/docs/rest-api",
    ],
    "pricing": [
        "/pricing",
        "/plans",
        "/pricing-plans",
        "/developer-pricing",
        "/enterprise",
        "/free-trial",
        "/pricing/api",
        "/docs/pricing",
    ],
    "developer_portal": [
        "/docs",
        "/documentation",
        "/developer",
        "/developers",
        "/getting-started",
        "/quickstart",
        "/start",
    ],
    "sdk": [
        "/docs/sdk",
        "/sdk",
        "/client-libraries",
        "/api/client-libraries",
        "/docs/client-libraries",
        "/sdks",
        "/docs/sdks",
    ],
    "webhooks": [
        "/docs/webhooks",
        "/webhooks",
        "/webhook",
        "/api/webhooks",
        "/docs/webhook",
        "/docs/events",
    ],
    "mcp": [
        "/docs/mcp",
        "/mcp",
        "/model-context-protocol",
        "/docs/model-context-protocol",
        "/mcp/server",
    ],
}

MAX_LLMS_PAGES = 75

# ────────────────────────────────────────────────────────────
#  Slot keywords for URL classification
# ────────────────────────────────────────────────────────────

SLOT_CLASSIFICATION_KEYWORDS: dict[str, list[str]] = {
    "authentication": [
        "/auth",
        "/oauth",
        "/authentication",
        "/authorization",
        "/api-keys",
        "/sso",
        "/security",
        "/access-token",
    ],
    "api_reference": [
        "/api",
        "/reference",
        "/openapi",
        "/swagger",
        "/graphql",
        "/rest-api",
        "/endpoint",
    ],
    "pricing": [
        "/pricing",
        "/plans",
        "/enterprise",
        "/free-trial",
        "/developer-pricing",
        "/subscription",
    ],
    "developer_portal": [
        "/docs",
        "/documentation",
        "/developer",
        "/developers",
        "/getting-started",
        "/quickstart",
        "/start",
        "/guide",
    ],
    "sdk": [
        "/sdk",
        "/client-libraries",
        "/sdks",
        "/libraries",
    ],
    "webhooks": [
        "/webhooks",
        "/webhook",
        "/events",
        "/event",
    ],
    "mcp": [
        "/mcp",
        "/model-context-protocol",
    ],
}

REJECT_KEYWORDS = re.compile(
    r"/(?:blog|tutorial|guide|how-to|community|forum|"
    r"discuss|chat|board|integration|zapier|n8n|make|"
    r"stackoverflow|youtube|status|changelog|release-notes)/?",
    re.IGNORECASE,
)

# ────────────────────────────────────────────────────────────
#  Domain helpers
# ────────────────────────────────────────────────────────────


def _extract_domain(url: str) -> str:
    """Extract the root domain from a URL."""
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    domain = parsed.netloc or parsed.path.split("/")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain.lower()


def _is_official(url: str, official_domain: str) -> bool:
    """Check if a URL belongs to the app's official domain."""
    url_domain = _extract_domain(url)
    return official_domain in url_domain or url_domain in official_domain


def _is_developer_subdomain(url: str, official_domain: str) -> bool:
    """Check if URL is on a developer subdomain (developer.X, docs.X, api.X, etc.)."""
    url_domain = _extract_domain(url)
    for pattern in DEV_SUBDOMAIN_PATTERNS:
        expected = pattern.format(domain=official_domain)
        if url_domain == expected:
            return True
    return False


# ────────────────────────────────────────────────────────────
#  URL canonicalization
# ────────────────────────────────────────────────────────────


def _strip_tracking_params(url: str) -> str:
    """Remove tracking query parameters from a URL."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parsed.query.split("&")
    clean = [p for p in params if p.split("=")[0].lower() not in TRACKING_PARAMS]
    return urlunparse(parsed._replace(query="&".join(clean)))


def _prefer_english(url: str) -> bool:
    """Return True if the URL is preferred (non-localized or English)."""
    path = urlparse(url).path.lower()
    localized = re.compile(
        r"^/(?:ja|jp|zh|cn|ko|kr|de|fr|es|it|pt|br|ru|ar|hi|nl|pl|tr|th|vi)/"
    )
    return not localized.match(path)


def _canonicalize_url(url: str) -> str:
    """Normalize a URL: upgrade to https, strip tracking params, drop fragment."""
    if url.startswith("http://"):
        url = f"https://{url[7:]}"
    elif not url.startswith("https://"):
        url = f"https://{url}"
    parsed = urlparse(url)
    url = urlunparse(parsed._replace(fragment=""))
    url = _strip_tracking_params(url)
    if url.endswith("/"):
        url = url.rstrip("/")
    return url


async def _resolve_redirect(url: str) -> str:
    """Follow redirects to find the canonical destination URL."""
    try:
        async with httpx.AsyncClient(
            timeout=8,
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
        ) as client:
            resp = await client.head(url)
            final = str(resp.url)
            if final and final != url:
                return _canonicalize_url(final)
    except Exception:
        pass
    return _canonicalize_url(url)


# ────────────────────────────────────────────────────────────
#  Excluded domain check
# ────────────────────────────────────────────────────────────


def _is_excluded(url: str) -> bool:
    """Check if a URL should be excluded outright."""
    url_lower = url.lower()
    for domain in EXCLUDED_DOMAINS:
        if domain in url_lower:
            return True
    if EXCLUDED_PATTERNS.search(url_lower):
        return True
    return False


# ────────────────────────────────────────────────────────────
#  Strict URL classifier
# ────────────────────────────────────────────────────────────


def _classify_url_strict(url: str, title: str) -> str | None:
    """Classify a URL into a slot. Returns None if the page should be rejected.

    Rejects: blogs, tutorials, community forums, integration platforms,
    comparison sites, directories, package indexes.
    """
    url_lower = url.lower()
    title_lower = title.lower()
    combined = f"{url_lower} {title_lower}"

    # Reject known low-quality sources
    if REJECT_KEYWORDS.search(url_lower):
        return None

    # MCP (check first — specific)
    if "/mcp" in url_lower or "model-context-protocol" in combined:
        return "mcp"

    # Webhooks
    if "/webhook" in url_lower:
        return "webhooks"

    # SDK
    if "/sdk" in url_lower or "/sdks" in url_lower:
        return "sdk"

    # Pricing
    if any(
        p in url_lower for p in ["/pricing", "/plans", "/free-trial", "/enterprise"]
    ):
        return "pricing"

    # Authentication
    if any(p in url_lower for p in ["/auth", "/oauth", "/authentication", "/api-keys"]):
        return "authentication"

    # API reference
    if any(
        p in url_lower
        for p in ["/api", "/reference", "/openapi", "/swagger", "/graphql"]
    ):
        return "api_reference"

    # Developer portal
    if any(
        p in url_lower for p in ["/docs", "/developer", "/documentation", "/quickstart"]
    ):
        return "developer_portal"

    return None


# ────────────────────────────────────────────────────────────
#  Deterministic confidence computation
# ────────────────────────────────────────────────────────────

CONFIDENCE_RULES = [
    ("official_docs", 0.40, lambda u, t, slot, domain, src: _is_official(u, domain)),
    (
        "developer_domain",
        0.25,
        lambda u, t, slot, domain, src: _is_developer_subdomain(u, domain),
    ),
    (
        "exact_slot_match",
        0.20,
        lambda u, t, slot, domain, src: _has_slot_keyword(u, t, slot),
    ),
    ("canonical_doc", 0.10, lambda u, t, slot, domain, src: _is_canonical_doc(u)),
    ("llms_txt_source", 0.05, lambda u, t, slot, domain, src: src == "llms_txt"),
]

# Canonical doc patterns — pages that look like definitive reference docs
_CANONICAL_PATTERNS = re.compile(
    r"/(?:reference|api(?:/v\d+)?(?:/docs)?|openapi|swagger|graphql|"
    r"docs/(?:api|reference|auth|webhooks|sdk|mcp))",
    re.IGNORECASE,
)


def _has_slot_keyword(url: str, title: str, slot: str) -> bool:
    """Check if URL or title contains slot-specific keywords."""
    keywords = SLOT_CLASSIFICATION_KEYWORDS.get(slot, [])
    combined = f"{url.lower()} {title.lower()}"
    return any(k in combined for k in keywords)


def _is_canonical_doc(url: str) -> bool:
    """Check if URL looks like canonical documentation (not a blog/overview page)."""
    return bool(_CANONICAL_PATTERNS.search(url))


def _compute_deterministic_confidence(
    url: str,
    title: str,
    slot: str,
    official_domain: str,
    discovered_from: str,
) -> tuple[float, list[str]]:
    """Compute confidence from explicit rules only. No LLM involvement."""
    score = 0.0
    reasons: list[str] = []

    for name, value, predicate in CONFIDENCE_RULES:
        if predicate(url, title, slot, official_domain, discovered_from):
            score += value
            reasons.append(f"{name}: +{value:.2f}")

    score = min(score, 1.0)
    return round(score, 2), reasons


# ────────────────────────────────────────────────────────────
#  SDK / MCP / Pricing validators
# ────────────────────────────────────────────────────────────

_OFFICIAL_GITHUB_RE = re.compile(
    r"github\.com/([^/]+)/",
    re.IGNORECASE,
)


def _validate_sdk(url: str, official_domain: str) -> bool:
    """Only accept SDKs from official docs, official GitHub org, or official npm."""
    if _is_official(url, official_domain):
        return True
    url_lower = url.lower()
    if "github.com" not in url_lower:
        return False
    # Accept any GitHub URL under the official domain's org
    if _is_official_github(url, official_domain):
        return True
    return False


def _validate_mcp(url: str, official_domain: str) -> bool:
    """Only accept MCP from official docs, dev portal, or official GitHub."""
    if _is_official(url, official_domain):
        return True
    url_lower = url.lower()
    if "github.com" in url_lower and _is_official_github(url, official_domain):
        return True
    return False


def _is_official_github(url: str, official_domain: str) -> bool:
    """Check if a GitHub URL belongs to the app's official organization."""
    # Heuristic: the GitHub org name often matches the domain name
    domain_name = official_domain.split(".")[0]
    match = _OFFICIAL_GITHUB_RE.search(url)
    if match:
        org = match.group(1).lower()
        return org == domain_name.lower()
    return False


def _validate_pricing(url: str) -> bool:
    """Only accept official pricing pages. Reject third-party comparison sites."""
    url_lower = url.lower()
    for domain in ["g2.com", "capterra", "getapp.com", "trustradius.com"]:
        if domain in url_lower:
            return False
    return True


# ────────────────────────────────────────────────────────────
#  Developer domain probing
# ────────────────────────────────────────────────────────────


def _dev_domain_urls(official_domain: str) -> list[str]:
    """Generate candidate developer subdomain URLs."""
    urls: list[str] = []
    for pattern in DEV_SUBDOMAIN_PATTERNS:
        domain = pattern.format(domain=official_domain)
        urls.append(f"https://{domain}")
    return urls


async def _probe_developer_domains(official_domain: str) -> list[str]:
    """Check which developer subdomains resolve for this app."""
    resolved: list[str] = []
    candidates = _dev_domain_urls(official_domain)
    async with httpx.AsyncClient(
        timeout=5,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
    ) as client:
        for url in candidates:
            try:
                resp = await client.head(url)
                if resp.status_code < 500:
                    resolved.append(url)
                    console.print(f"    [green]✓ Dev domain: {url}[/green]")
            except Exception:
                pass
    return resolved


# ────────────────────────────────────────────────────────────
#  llms.txt discovery & parsing
# ────────────────────────────────────────────────────────────


async def _try_llms_txt(
    domains: list[str],
) -> tuple[CanonicalDocument | None, list[DocInventoryItem]]:
    """Probe for llms.txt / llms-full.txt on each domain. Returns (llms_doc, pages_from_llms)."""
    candidates: list[str] = []
    for domain in domains:
        base = domain.rstrip("/")
        candidates.append(f"{base}/llms-full.txt")
        candidates.append(f"{base}/llms.txt")

    async with httpx.AsyncClient(
        timeout=10,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
    ) as client:
        for candidate_url in candidates:
            try:
                resp = await client.get(candidate_url)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    text = resp.text.strip()
                    if (
                        len(text) > 50
                        and ("text/plain" in content_type or not text.startswith("<!"))
                        and not text.startswith("<html")
                    ):
                        snippet = text[:300].replace("\n", " ")
                        console.print(f"    [green]✓ Found {candidate_url}[/green]")
                        # Build the llms.txt document
                        llms_doc = CanonicalDocument(
                            url=candidate_url,
                            title="llms.txt index",
                            confidence=1.0,
                            confidence_reason=[
                                "llms_txt_source: +0.05",
                                "official_docs: +0.40",
                                "canonical_doc: +0.10",
                            ],
                            is_official=True,
                            discovered_from="llms_txt",
                        )
                        # Parse pages from llms.txt
                        pages = _parse_llms_txt(text, candidate_url)
                        return llms_doc, pages
            except Exception:
                continue

    return None, []


def _parse_llms_txt(text: str, source_url: str) -> list[DocInventoryItem]:
    """Parse llms.txt to extract documented pages with descriptions.

    Handles common formats:
      - [Title](url): Description
      - [Title](url)
      - plain URLs

    Only retains pages that can be classified into a known documentation slot.
    Caps at MAX_LLMS_PAGES to prevent bloating the inventory.
    """
    items: list[DocInventoryItem] = []
    seen_urls: set[str] = set()
    domain = _extract_domain(source_url)
    link_count = 0

    link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    url_pattern = re.compile(r"https?://[^\s)\]]+")

    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if link_count >= MAX_LLMS_PAGES:
            break

        links = link_pattern.findall(line)
        if links:
            for title, url in links:
                if link_count >= MAX_LLMS_PAGES:
                    break
                url = _canonicalize_url(url)
                if url in seen_urls or _is_excluded(url):
                    continue
                seen_urls.add(url)
                page_type = _classify_url_strict(url, title)
                if page_type is None:
                    continue
                is_off = _is_official(url, domain)
                conf, reasons = _compute_deterministic_confidence(
                    url, title, page_type, domain, "llms_txt"
                )
                items.append(
                    DocInventoryItem(
                        url=url,
                        page_type=page_type,
                        title=title,
                        confidence=conf,
                        confidence_reason=reasons,
                        discovered_from="llms_txt",
                        is_official=is_off,
                    )
                )
                link_count += 1
        else:
            urls = url_pattern.findall(line)
            for url in urls:
                if link_count >= MAX_LLMS_PAGES:
                    break
                url = _canonicalize_url(url)
                if url in seen_urls or _is_excluded(url):
                    continue
                seen_urls.add(url)
                page_type = _classify_url_strict(url, "")
                if page_type is None:
                    continue
                is_off = _is_official(url, domain)
                conf, reasons = _compute_deterministic_confidence(
                    url, "", page_type, domain, "llms_txt"
                )
                items.append(
                    DocInventoryItem(
                        url=url,
                        page_type=page_type,
                        title="",
                        confidence=conf,
                        confidence_reason=reasons,
                        discovered_from="llms_txt",
                        is_official=is_off,
                    )
                )
                link_count += 1

    return items


# ────────────────────────────────────────────────────────────
#  Slot path probing
# ────────────────────────────────────────────────────────────


async def _probe_path(
    client: httpx.AsyncClient, base_url: str, path: str
) -> str | None:
    """Check if a URL path exists on the given base domain."""
    url = f"{base_url.rstrip('/')}{path}"
    try:
        resp = await client.head(url, follow_redirects=True)
        if resp.status_code < 400:
            return str(resp.url)
    except Exception:
        pass
    return None


async def _probe_slot_paths(
    client: httpx.AsyncClient,
    domains: list[str],
    slot: str,
    official_domain: str,
) -> list[DocInventoryItem]:
    """Probe known URL paths for a documentation slot across all available domains."""
    items: list[DocInventoryItem] = []
    seen_urls: set[str] = set()
    paths = SLOT_PROBE_PATHS.get(slot, [])

    for base in domains:
        for path in paths:
            resolved = await _probe_path(client, base, path)
            if resolved is None:
                continue
            resolved = _canonicalize_url(resolved)
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)

            is_off = _is_official(resolved, official_domain)
            if not is_off:
                continue

            conf, reasons = _compute_deterministic_confidence(
                resolved, "", slot, official_domain, "domain_probe"
            )

            items.append(
                DocInventoryItem(
                    url=resolved,
                    page_type=slot,
                    title="",
                    confidence=conf,
                    confidence_reason=reasons,
                    discovered_from="domain_probe",
                    is_official=True,
                )
            )

    return items


# ────────────────────────────────────────────────────────────
#  Tavily search (now a fallback — only for still-empty slots)
# ────────────────────────────────────────────────────────────


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15), reraise=True)
async def _tavily_search(
    client: AsyncTavilyClient,
    query: str,
    max_results: int = 5,
) -> list[dict]:
    """Execute a Tavily search."""
    response = await client.search(
        query=query,
        max_results=max_results,
        include_answer=False,
        exclude_domains=list(EXCLUDED_DOMAINS),
    )
    return response.get("results", [])


async def _tavily_fallback(
    client: AsyncTavilyClient,
    app_name: str,
    slot: str,
    official_domain: str,
) -> list[DocInventoryItem]:
    """Tavily search as LAST resort for a still-empty slot.

    Only accepts official-domain results.
    """
    query_templates: dict[str, str] = {
        "authentication": f"{{}} authentication OAuth API keys authorization",
        "api_reference": f"{{}} API reference documentation",
        "pricing": f"{{}} pricing plans API developer",
        "developer_portal": f"{{}} developer portal documentation",
        "sdk": f"{{}} SDK client library",
        "webhooks": f"{{}} webhooks events API",
        "mcp": f"{{}} MCP server Model Context Protocol",
    }

    query = query_templates.get(slot, f"{app_name} {slot}").format(app_name)
    items: list[DocInventoryItem] = []
    seen_urls: set[str] = set()

    try:
        results = await _tavily_search(client, query, max_results=3)
    except Exception as exc:
        console.print(f"    [yellow]Tavily fallback failed for {slot}: {exc}[/yellow]")
        return []

    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        url = await _resolve_redirect(url)
        url = _canonicalize_url(url)
        if url in seen_urls or _is_excluded(url):
            continue
        seen_urls.add(url)

        is_off = _is_official(url, official_domain)
        if not is_off:
            continue

        title = r.get("title", "")
        # Classify strictly
        page_type = _classify_url_strict(url, title)
        if page_type is None:
            continue

        # The slot from Tavily should match the slot we're searching for
        if page_type != slot:
            continue

        conf, reasons = _compute_deterministic_confidence(
            url, title, slot, official_domain, "tavily_search"
        )

        items.append(
            DocInventoryItem(
                url=url,
                page_type=page_type,
                title=title,
                confidence=conf,
                confidence_reason=reasons,
                discovered_from="tavily_search",
                is_official=is_off,
            )
        )

    return items


# ────────────────────────────────────────────────────────────
#  Build inventory from all sources
# ────────────────────────────────────────────────────────────


def _build_inventory(
    app_name: str,
    official_domain: str,
    all_items: list[DocInventoryItem],
    stats: DiscoveryStats,
) -> DocumentationInventory:
    """Build a DocumentationInventory from all discovered items, with stats."""
    # Count filtered items
    non_official = sum(1 for i in all_items if not i.is_official)
    blog_community = sum(
        1 for i in all_items if i.page_type is None and not i.is_official
    )
    third_party_sdk = sum(
        1 for i in all_items if i.page_type == "sdk" and not i.is_official
    )

    # Count unique URLs
    all_urls_list = [i.url for i in all_items]
    dups = len(all_urls_list) - len(set(all_urls_list))

    # Count official
    official_count = sum(1 for i in all_items if i.is_official)

    # Items with a valid page_type = will become canonical
    canonical_candidates = [i for i in all_items if i.page_type is not None]

    stats.urls_discovered = len(all_items)
    stats.official_urls = official_count
    stats.non_official_filtered = non_official
    stats.blog_community_filtered = blog_community
    stats.third_party_sdk_filtered = third_party_sdk
    stats.duplicates_removed = dups
    stats.canonical_selected = len(set(i.page_type for i in canonical_candidates))

    return DocumentationInventory(
        app_name=app_name,
        official_domain=official_domain,
        items=all_items,
        stats=stats,
    )


# ────────────────────────────────────────────────────────────
#  Rank and select canonical documents
# ────────────────────────────────────────────────────────────


def _item_to_canonical(item: DocInventoryItem) -> CanonicalDocument:
    """Convert a DocInventoryItem to a CanonicalDocument."""
    return CanonicalDocument(
        url=item.url,
        title=item.title,
        confidence=item.confidence,
        confidence_reason=item.confidence_reason,
        is_official=item.is_official,
        discovered_from=item.discovered_from,
    )


def _rank_and_select_canonical(
    inventory: DocumentationInventory,
    llms_doc: CanonicalDocument | None,
) -> DocumentationMap:
    """Per-slot: pick the best page as primary, rest as alternatives.

    Stop-early principle: if the best page has high confidence (>=0.75),
    we do not continue searching for that slot (enforced by discovery flow).
    """
    doc_map = DocumentationMap(
        app_name=inventory.app_name,
        official_domain=inventory.official_domain,
        inventory=inventory,
        stats=inventory.stats,
        llms_txt=llms_doc,
    )

    # Group items by page_type
    slot_groups: dict[str, list[DocInventoryItem]] = {}
    for item in inventory.items:
        if item.page_type is None:
            continue
        if item.page_type not in slot_groups:
            slot_groups[item.page_type] = []
        slot_groups[item.page_type].append(item)

    ALL_SLOTS = [
        "authentication",
        "api_reference",
        "pricing",
        "developer_portal",
        "sdk",
        "webhooks",
        "mcp",
    ]

    for slot_name in ALL_SLOTS:
        items = slot_groups.get(slot_name, [])
        if not items:
            continue

        # Sort by confidence desc
        items.sort(key=lambda i: i.confidence, reverse=True)

        # Best is primary
        best = items[0]
        primary = _item_to_canonical(best)

        # Rest are alternatives
        alternatives = [_item_to_canonical(i) for i in items[1:]]

        # Validate SDK / MCP / Pricing
        if slot_name == "sdk":
            alternatives = [
                a
                for a in alternatives
                if _validate_sdk(a.url, inventory.official_domain)
            ]
        elif slot_name == "mcp":
            alternatives = [
                a
                for a in alternatives
                if _validate_mcp(a.url, inventory.official_domain)
            ]
        elif slot_name == "pricing":
            alternatives = [a for a in alternatives if _validate_pricing(a.url)]

        slot = DocumentSlot(primary=primary, alternatives=alternatives)
        setattr(doc_map, slot_name, slot)

        if primary:
            console.print(
                f"    [dim]Slot '{slot_name}': {primary.url} "
                f"(conf={primary.confidence}, {len(alternatives)} alt)[/dim]"
            )

    # Homepage
    homepage_items = slot_groups.get("homepage", [])
    if homepage_items:
        homepage_items.sort(key=lambda i: i.confidence, reverse=True)
        doc_map.homepage = _item_to_canonical(homepage_items[0])

    return doc_map


# ────────────────────────────────────────────────────────────
#  Homepage discovery
# ────────────────────────────────────────────────────────────


def _make_homepage_item(app: AppMetadata, official_domain: str) -> DocInventoryItem:
    """Create a DocInventoryItem for the app's homepage."""
    url = _canonicalize_url(app.website)
    conf, reasons = _compute_deterministic_confidence(
        url, app.name, "homepage", official_domain, "domain_probe"
    )
    return DocInventoryItem(
        url=url,
        page_type="homepage",
        title=app.name,
        confidence=conf,
        confidence_reason=reasons,
        discovered_from="domain_probe",
        is_official=True,
    )


# ────────────────────────────────────────────────────────────
#  Cache
# ────────────────────────────────────────────────────────────


def _cache_path(app: AppMetadata):
    return CACHE_DIR / f"discovery_{app.slug}.json"


def _load_cache(app: AppMetadata) -> DocumentationMap | None:
    path = _cache_path(app)
    if path.exists():
        try:
            return DocumentationMap.model_validate(orjson.loads(path.read_bytes()))
        except Exception:
            return None
    return None


def _save_cache(app: AppMetadata, doc_map: DocumentationMap) -> None:
    path = _cache_path(app)
    path.write_bytes(
        orjson.dumps(doc_map.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
    )


# ────────────────────────────────────────────────────────────
#  Main discovery orchestrator
# ────────────────────────────────────────────────────────────


async def discover_documentation(app: AppMetadata) -> DocumentationMap:
    """Discover all relevant documentation URLs for an app.

    Architecture:
      1. Domain detection
      2. Developer subdomain probing
      3. llms.txt discovery (highest priority)
      4. Slot path probing (deterministic)
      5. Tavily fallback (only for empty slots)
      6. Build inventory
      7. Rank and select canonical
    """
    cached = _load_cache(app)
    if cached:
        return cached

    settings = get_settings()
    official_domain = _extract_domain(app.website)
    console.print(f"    [dim]Domain: {official_domain}[/dim]")

    all_items: list[DocInventoryItem] = []
    stats = DiscoveryStats()
    queries_executed = 0
    llms_doc: CanonicalDocument | None = None

    # ── Step 1: Developer domain probing ──
    dev_domains = await _probe_developer_domains(official_domain)
    all_domains = [f"https://{official_domain}"] + dev_domains
    console.print(f"    [dim]Probed {len(dev_domains)} developer subdomains[/dim]")

    # ── Step 2: Homepage ──
    homepage_item = _make_homepage_item(app, official_domain)
    all_items.append(homepage_item)

    # ── Step 3: llms.txt discovery (highest priority) ──
    llms_doc, llms_pages = await _try_llms_txt(all_domains)
    all_items.extend(llms_pages)
    if llms_pages:
        console.print(f"    [dim]llms.txt: {len(llms_pages)} pages discovered[/dim]")
        queries_executed += 1

    # ── Step 4: Slot path probing (deterministic, no API calls) ──
    ALL_REQUIRED_SLOTS = [
        "authentication",
        "api_reference",
        "pricing",
        "developer_portal",
        "sdk",
        "webhooks",
        "mcp",
    ]

    # Track which slots have ANY classified page — not just high-confidence ones.
    # If a slot already has classified pages from llms.txt, we skip both
    # path probing and Tavily for that slot.
    filled_slots: set[str] = set()
    for item in llms_pages:
        if item.page_type:
            filled_slots.add(item.page_type)

    async with httpx.AsyncClient(
        timeout=5,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
    ) as client:
        for slot_name in ALL_REQUIRED_SLOTS:
            if slot_name in filled_slots:
                console.print(
                    f"    [dim]Slot '{slot_name}': already filled (llms.txt), skipping probe[/dim]"
                )
                continue
            path_items = await _probe_slot_paths(
                client, all_domains, slot_name, official_domain
            )
            for item in path_items:
                if item.page_type not in filled_slots:
                    all_items.append(item)
                    filled_slots.add(item.page_type)
                    console.print(
                        f"    [green]✓ Slot '{slot_name}': found via path probe[/green]"
                    )

    # ── Step 5: Tavily fallback (only for still-empty slots) ──
    empty_slots = [s for s in ALL_REQUIRED_SLOTS if s not in filled_slots]

    if empty_slots:
        tavily_client = AsyncTavilyClient(api_key=settings.tavily_api_key)
        for slot in empty_slots:
            console.print(f"    [yellow]Tavily fallback for slot: {slot}[/yellow]")
            tavily_items = await _tavily_fallback(
                tavily_client, app.name, slot, official_domain
            )
            for item in tavily_items:
                if item.page_type not in filled_slots:
                    all_items.append(item)
                    filled_slots.add(item.page_type)
            queries_executed += 1

    stats.queries = queries_executed

    # ── Step 6: Build inventory ──
    inventory = _build_inventory(app.name, official_domain, all_items, stats)

    # ── Step 7: Rank and select canonical ──
    doc_map = _rank_and_select_canonical(inventory, llms_doc)

    if doc_map.stats:
        console.print(
            f"    [dim]Stats: {doc_map.stats.urls_discovered} raw, "
            f"{doc_map.stats.canonical_selected} canonical, "
            f"{doc_map.stats.non_official_filtered} non-official filtered, "
            f"{doc_map.stats.blog_community_filtered} blog/community filtered, "
            f"{doc_map.stats.duplicates_removed} dups[/dim]"
        )

    _save_cache(app, doc_map)
    return doc_map
