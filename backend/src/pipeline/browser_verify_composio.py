"""Browser verification — Composio browser_tool adapter (Stage 5 alternative).

Replaces the Playwright-based browser_verify.py with Composio's built-in
browser_tool toolkit. This module is intentionally a drop-in replacement:
it exports the same `verify_with_browser(result)` coroutine and returns
the same `BrowserVerification` model.

Composio browser_tool actions used:
  - BROWSER_TOOL_GET_WEB_TEXT_CONTENT  → fetch rendered page text (no JS eval needed)

No external API key required — browser_tool uses NO_AUTH (runs locally via
Composio's bundled Playwright/Pyppeteer session).

Branch: feature/composio-browser-tool
Comparison target: src/pipeline/browser_verify.py (Playwright direct)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from models.result import BrowserVerification, ResearchResult
from src.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── keyword tables (identical to browser_verify.py for fair comparison) ─────

AUTH_KEYWORDS = {
    "oauth2": ["oauth", "oauth2", "oauth 2.0", "authorize"],
    "api_key": ["api key", "api-key", "apikey", "api_key", "x-api-key"],
    "bearer": ["bearer", "bearer token", "authorization: bearer"],
    "basic": ["basic auth", "basic authentication"],
    "jwt": ["jwt", "json web token"],
    "token": ["access token", "personal access token", "api token"],
}

SELF_SERVE_KEYWORDS = {
    "self_serve": [
        "sign up", "signup", "register", "create account", "get started",
        "free tier", "free plan", "free trial", "start free", "try for free",
        "pricing", "per month", "/mo", "developer plan",
    ],
    "gated": [
        "contact sales", "talk to sales", "request access", "request demo",
        "enterprise only", "contact us for pricing", "apply for access",
        "invite only", "waitlist", "by invitation",
    ],
}


def _get_page_text_via_composio(url: str, api_key: str) -> str:
    """Fetch rendered page text using Composio browser_tool.

    Uses BROWSER_TOOL_GET_WEB_TEXT_CONTENT action which renders the page
    (JS included) and returns extracted text content.
    """
    try:
        from composio import Composio  # type: ignore[import]

        client = Composio(api_key=api_key)
        result = client.tools.execute(
            slug="BROWSER_TOOL_GET_WEB_TEXT_CONTENT",
            request={"url": url},
        )
        # result is a dict; the text is typically under 'text' or 'content' key
        if isinstance(result, dict):
            return str(result.get("text") or result.get("content") or result.get("data") or "")
        return str(result)
    except Exception as exc:
        logger.debug("Composio browser_tool failed for %s: %s", url, exc)
        return ""


async def verify_with_browser(result: ResearchResult) -> BrowserVerification:
    """Verify disputed fields using Composio browser_tool.

    Drop-in replacement for browser_verify.verify_with_browser().
    Same inputs, same outputs, different underlying transport.
    """
    settings = get_settings()
    if settings.skip_browser_verification:
        return BrowserVerification(notes="Browser verification skipped by config")

    if not settings.composio_api_key:
        return BrowserVerification(notes="Composio browser_tool: no API key configured")

    verification = BrowserVerification()

    if not result.validation or not result.validation.needs_verification:
        verification.notes = "No disputed fields to verify"
        return verification

    # Collect disputed fields
    disputed_fields: set[str] = set()
    for issue in result.validation.issues:
        if issue.severity in ("ERROR", "WARNING"):
            disputed_fields.add(issue.field)

    if not disputed_fields:
        return verification

    # Build URL list (same heuristic as Playwright version)
    urls_to_check: list[str] = []
    if result.documentation_map:
        if "self_serve" in disputed_fields:
            urls_to_check.extend(result.documentation_map.pricing_urls[:2])
        if "auth_methods" in disputed_fields:
            urls_to_check.extend(result.documentation_map.api_docs_urls[:2])
        urls_to_check.extend(result.documentation_map.official_urls[:2])
    urls_to_check.append(result.app.website)
    urls_to_check = list(dict.fromkeys(urls_to_check))[:5]

    # Fetch text via Composio browser_tool (synchronous calls — no asyncio needed)
    all_text = ""
    fetched_count = 0
    for url in urls_to_check:
        if not url:
            continue
        text = _get_page_text_via_composio(url, settings.composio_api_key)
        if text:
            all_text += f"\n{text[:3000]}"
            fetched_count += 1

    if not all_text:
        verification.notes = "Composio browser_tool: no pages fetched"
        return verification

    verification.notes = f"Composio browser_tool: fetched {fetched_count} pages"
    text_lower = all_text.lower()

    # Auth methods check
    if "auth_methods" in disputed_fields:
        found_auth = [
            auth_type
            for auth_type, keywords in AUTH_KEYWORDS.items()
            if any(kw in text_lower for kw in keywords)
        ]
        if found_auth:
            verification.verified_fields.append("auth_methods")
            verification.corrections["auth_methods"] = ", ".join(found_auth)

    # Self-serve check
    if "self_serve" in disputed_fields:
        self_serve_score = sum(1 for kw in SELF_SERVE_KEYWORDS["self_serve"] if kw in text_lower)
        gated_score = sum(1 for kw in SELF_SERVE_KEYWORDS["gated"] if kw in text_lower)
        if self_serve_score > 0 or gated_score > 0:
            verification.verified_fields.append("self_serve")
            verdict = "Self-serve" if self_serve_score >= gated_score else "Gated"
            verification.corrections["self_serve"] = f"{verdict} (verified by Composio browser_tool)"

    return verification
