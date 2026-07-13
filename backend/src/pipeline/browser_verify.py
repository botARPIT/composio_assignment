"""Browser verification — Playwright-based verification for disputed fields.

Only runs on apps where validation status is UNSUPPORTED or INSUFFICIENT_EVIDENCE.
Only checks specific disputed fields.
"""

from __future__ import annotations

import asyncio
import re

from rich.console import Console

from models.result import BrowserVerification, ResearchResult
from src.config import get_settings

console = Console()

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


async def verify_with_browser(result: ResearchResult) -> BrowserVerification:
    """Verify disputed fields using Playwright browser.

    Only checks fields flagged by the validator.
    Returns verification results without modifying the extraction.
    """
    settings = get_settings()
    if settings.skip_browser_verification:
        return BrowserVerification(notes="Browser verification skipped by config")

    verification = BrowserVerification()
    
    if not result.validation or not result.validation.needs_verification:
        verification.notes = "No disputed fields to verify"
        return verification

    # Get disputed fields
    disputed_fields = set()
    for issue in result.validation.issues:
        if issue.severity in ("ERROR", "WARNING"):
            disputed_fields.add(issue.field)

    if not disputed_fields:
        return verification

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            page.set_default_timeout(15000)

            # Build URLs to check
            urls_to_check = []
            if result.documentation_map:
                # Check pricing/signup pages for self_serve
                if "self_serve" in disputed_fields:
                    urls_to_check.extend(result.documentation_map.pricing_urls[:2])
                # Check API docs for auth
                if "auth_methods" in disputed_fields:
                    urls_to_check.extend(result.documentation_map.api_docs_urls[:2])
                # Add official URLs as fallback
                urls_to_check.extend(result.documentation_map.official_urls[:2])

            # Also check the main website
            urls_to_check.append(result.app.website)
            urls_to_check = list(dict.fromkeys(urls_to_check))[:5]  # Deduplicate, max 5

            all_text = ""
            for url in urls_to_check:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(1)
                    text = await page.inner_text("body")
                    all_text += f"\n{text[:3000]}"
                except Exception:
                    continue

            text_lower = all_text.lower()

            # Check auth methods
            if "auth_methods" in disputed_fields:
                found_auth = []
                for auth_type, keywords in AUTH_KEYWORDS.items():
                    if any(kw in text_lower for kw in keywords):
                        found_auth.append(auth_type)
                if found_auth:
                    verification.verified_fields.append("auth_methods")
                    verification.corrections["auth_methods"] = ", ".join(found_auth)

            # Check self-serve
            if "self_serve" in disputed_fields:
                self_serve_score = sum(1 for kw in SELF_SERVE_KEYWORDS["self_serve"] if kw in text_lower)
                gated_score = sum(1 for kw in SELF_SERVE_KEYWORDS["gated"] if kw in text_lower)

                if self_serve_score > 0 or gated_score > 0:
                    verification.verified_fields.append("self_serve")
                    if self_serve_score > gated_score:
                        verification.corrections["self_serve"] = "Self-serve (verified by browser)"
                    else:
                        verification.corrections["self_serve"] = "Gated (verified by browser)"

            await browser.close()

    except Exception as exc:
        verification.notes = f"Browser verification error: {exc}"
        console.print(f"[yellow]Browser verification failed for {result.app.name}: {exc}[/yellow]")

    return verification
