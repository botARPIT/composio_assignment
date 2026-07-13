"""Extraction chain — LLM-based semantic extraction from evidence.

Responsibilities:
- Pack highest-quality evidence chunks within token budget
- Extract structured fields from evidence text
- Cite evidence IDs for every extracted value
- Return Extraction model

Supports multiple LLM providers with automatic fallback:
  Primary: OpenAI (gpt-4o-mini)
  Fallback: Gemini (gemini-2.5-flash)

The LLM MUST NOT reason about validation. It ONLY extracts and cites.
"""

from __future__ import annotations

import json
import os
import re

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)
from rich.console import Console

from models.evidence import Evidence
from models.extraction import Extraction, FieldValue
from src.config import get_settings

console = Console()

EXTRACTION_SYSTEM_PROMPT = """\
You are a structured data extraction agent. Your ONLY job is to extract specific fields from the provided evidence text. You MUST cite evidence IDs for every value.

RULES:
1. ONLY extract information that is EXPLICITLY stated in the evidence. Never infer or guess.
2. Every field value MUST cite at least one evidence ID (e.g., "ev_abc123").
3. If a field cannot be determined from the evidence, use "UNKNOWN".
4. Do NOT add information from your training data. Only use the evidence provided.
5. For auth_methods: Look for explicit mentions of OAuth, API Key, Bearer Token, JWT, Basic Auth, SAML, etc.
6. For self_serve: Look for explicit mentions of "sign up", "free tier", "trial", "pricing", "contact sales", "enterprise only", "request access".
7. For api_surface: Look for REST, GraphQL, gRPC, webhooks, SDKs, OpenAPI.
8. For mcp: Look for explicit mentions of "MCP", "Model Context Protocol", or MCP server implementations.

Return a JSON object with this exact schema:
{
  "description": {"value": "One-line description", "evidence_ids": ["ev_xxx"]},
  "category": {"value": "Category", "evidence_ids": ["ev_xxx"]},
  "auth_methods": {"value": "OAuth2, API Key", "evidence_ids": ["ev_xxx"]},
  "self_serve": {"value": "Self-serve (free tier available)" or "Gated (contact sales)" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
  "api_surface": {"value": "REST API, GraphQL, webhooks, SDKs", "evidence_ids": ["ev_xxx"]},
  "api_breadth": {"value": "Broad" or "Moderate" or "Narrow" or "Minimal" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
  "mcp": {"value": "Official MCP" or "Community MCP" or "No known MCP" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
  "buildability": {"value": "HIGH" or "MEDIUM" or "LOW" or "BLOCKED", "evidence_ids": ["ev_xxx"]},
  "blocker": {"value": "Description of blocker or None", "evidence_ids": ["ev_xxx"]}
}

Return ONLY the JSON object. No markdown code fences. No explanation."""


# ────────────────────────────────────────────────────────────
#  Evidence context building
# ────────────────────────────────────────────────────────────


def _build_evidence_context(
    evidence_list: list[Evidence], max_tokens: int = 6000
) -> str:
    """Build evidence context string for the LLM using priority-based packing.

    Evidence chunks are already sorted by quality_score desc from the collector.
    We pack chunks greedily until we hit the token budget.
    """
    sections = []
    total_tokens = 0

    for ev in evidence_list:
        chunk_tokens = ev.token_count if ev.token_count > 0 else len(ev.content) // 4
        overhead = 30
        if total_tokens + chunk_tokens + overhead > max_tokens:
            remaining_tokens = max_tokens - total_tokens - overhead
            if remaining_tokens > 100:
                truncated = ev.content[: remaining_tokens * 4]
                section_label = f"[{ev.source_type}]"
                if ev.section_path:
                    section_label += f" [{ev.section_path}]"
                header = f"--- EVIDENCE {ev.id} {section_label} ({ev.url}) ---"
                sections.append(f"{header}\n{truncated}\n... [truncated]")
            break

        section_label = f"[{ev.source_type}]"
        if ev.section_path:
            section_label += f" [{ev.section_path}]"
        header = f"--- EVIDENCE {ev.id} {section_label} ({ev.url}) ---"
        sections.append(f"{header}\n{ev.content}\n")
        total_tokens += chunk_tokens + overhead

    return "\n".join(sections)


# ────────────────────────────────────────────────────────────
#  Response parsing
# ────────────────────────────────────────────────────────────


def _parse_extraction_response(text: str) -> dict:
    """Parse the LLM response into an extraction dict."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


def _parse_batch_extraction_response(text: str) -> dict[str, dict]:
    """Parse batch LLM response into a dict keyed by app name."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text.startswith("json"):
        text = text[4:].strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected dict, got {type(parsed).__name__}")
    return parsed


def _safe_field_value(data: dict, field: str) -> FieldValue:
    """Extract a FieldValue from parsed data, handling malformed responses."""
    val = data.get(field, {})
    if isinstance(val, dict):
        return FieldValue(
            value=val.get("value", "UNKNOWN"),
            evidence_ids=val.get("evidence_ids", []),
        )
    return FieldValue(value=str(val) if val else "UNKNOWN", evidence_ids=[])


# ────────────────────────────────────────────────────────────
#  Rate limiting / retry
# ────────────────────────────────────────────────────────────


def _is_retryable_rate_limit(exc: BaseException) -> bool:
    """Return True if the exception is a retryable rate limit (not daily quota)."""
    msg = str(exc)
    if "quota" in msg.lower():
        return False
    return bool(
        re.search(
            r"429|RESOURCE_EXHAUSTED|rate|Too Many Requests|Resource has been exhausted",
            msg,
        )
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=15, min=30, max=90),
    retry=retry_if_exception(_is_retryable_rate_limit),
    reraise=True,
)
async def _invoke_llm_with_retry(llm, messages):
    """Invoke LLM with retry on rate limit errors."""
    return await llm.ainvoke(messages)


# ────────────────────────────────────────────────────────────
#  LLM provider factories
# ────────────────────────────────────────────────────────────


def _init_openai_llm(settings):
    """Create an OpenAI LLM instance, or None if no key configured."""
    if not settings.openai_api_key:
        return None
    return ChatOpenAI(
        model=settings.openai_model,
        temperature=settings.llm_temperature,
        api_key=settings.openai_api_key,
    )


def _init_gemini_llm(settings):
    """Create a Gemini LLM instance, or None if no key configured."""
    if not settings.gemini_api_key:
        return None
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=settings.llm_temperature,
        google_api_key=settings.gemini_api_key,
    )


def _build_providers(settings) -> list[tuple[str, object]]:
    """Build ordered list of (provider_name, llm) tuples.

    Primary: OpenAI (gpt-4o-mini)
    Fallback: Gemini (gemini-2.5-flash)
    """
    providers = [
        ("OpenAI", _init_openai_llm(settings)),
        ("Gemini", _init_gemini_llm(settings)),
    ]
    return [(name, llm) for name, llm in providers if llm is not None]


async def _extract_with_fallback(
    providers: list[tuple[str, object]],
    messages: list,
    context: str = "",
) -> str | None:
    """Try each LLM provider in order. Returns response content or None."""
    for provider_name, llm in providers:
        try:
            response = await _invoke_llm_with_retry(llm, messages)
            return response.content
        except Exception as exc:
            console.print(
                f"  [yellow]{provider_name} failed for {context}: {exc}[/yellow]"
            )
            continue
    console.print(f"  [red]All providers failed for {context}[/red]")
    return None


# ────────────────────────────────────────────────────────────
#  Single-app extraction
# ────────────────────────────────────────────────────────────


async def extract_from_evidence(
    app_name: str,
    evidence_list: list[Evidence],
) -> Extraction | None:
    """Run LLM extraction on evidence.

    Input: list[Evidence] (sorted by quality_score desc)
    Output: Extraction | None

    Uses priority-based packing: highest quality chunks first within token budget.
    Falls back from OpenAI → Gemini if primary provider fails.
    """
    if not evidence_list:
        console.print(f"[yellow]No evidence to extract from for {app_name}[/yellow]")
        return None

    settings = get_settings()
    os.environ.pop("LANGCHAIN_TRACING_V2", None)

    providers = _build_providers(settings)
    if not providers:
        console.print(
            "[red]No LLM provider configured (set OPEN_AI_API_KEY or GEMINI_API_KEY)[/red]"
        )
        return None

    evidence_context = _build_evidence_context(evidence_list)

    messages = [
        SystemMessage(content=EXTRACTION_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Extract structured data for the application: {app_name}\n\n{evidence_context}"
        ),
    ]

    response_content = await _extract_with_fallback(providers, messages, app_name)
    if response_content is None:
        return None

    try:
        parsed = _parse_extraction_response(response_content)
    except json.JSONDecodeError:
        console.print(f"[red]Failed to parse extraction response for {app_name}[/red]")
        return None

    extraction = Extraction(
        description=_safe_field_value(parsed, "description"),
        category=_safe_field_value(parsed, "category"),
        auth_methods=_safe_field_value(parsed, "auth_methods"),
        self_serve=_safe_field_value(parsed, "self_serve"),
        api_surface=_safe_field_value(parsed, "api_surface"),
        api_breadth=_safe_field_value(parsed, "api_breadth"),
        mcp=_safe_field_value(parsed, "mcp"),
        buildability=_safe_field_value(parsed, "buildability"),
        blocker=_safe_field_value(parsed, "blocker"),
    )
    return extraction


# ────────────────────────────────────────────────────────────
#  Batch extraction prompt
# ────────────────────────────────────────────────────────────

BATCH_EXTRACTION_SYSTEM_PROMPT = """\
You are a structured data extraction agent. Your ONLY job is to extract specific fields from the provided evidence text for MULTIPLE applications. You MUST cite evidence IDs for every value.

RULES:
1. ONLY extract information that is EXPLICITLY stated in the evidence. Never infer or guess.
2. Every field value MUST cite at least one evidence ID (e.g., "ev_abc123").
3. If a field cannot be determined from the evidence, use "UNKNOWN".
4. Do NOT add information from your training data. Only use the evidence provided.
5. For auth_methods: Look for explicit mentions of OAuth, API Key, Bearer Token, JWT, Basic Auth, SAML, etc.
6. For self_serve: Look for explicit mentions of "sign up", "free tier", "trial", "pricing", "contact sales", "enterprise only", "request access".
7. For api_surface: Look for REST, GraphQL, gRPC, webhooks, SDKs, OpenAPI.
8. For mcp: Look for explicit mentions of "MCP", "Model Context Protocol", or MCP server implementations.

Each application's evidence is labeled with "--- APP: <name> ---".
Return a SINGLE JSON object where top-level keys are application names.
Each value follows the same schema as single-app extraction:

{
  "AppName1": {
    "description": {"value": "One-line description", "evidence_ids": ["ev_xxx"]},
    "category": {"value": "Category", "evidence_ids": ["ev_xxx"]},
    "auth_methods": {"value": "OAuth2, API Key", "evidence_ids": ["ev_xxx"]},
    "self_serve": {"value": "Self-serve (free tier available)" or "Gated (contact sales)" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
    "api_surface": {"value": "REST API, GraphQL, webhooks, SDKs", "evidence_ids": ["ev_xxx"]},
    "api_breadth": {"value": "Broad" or "Moderate" or "Narrow" or "Minimal" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
    "mcp": {"value": "Official MCP" or "Community MCP" or "No known MCP" or "UNKNOWN", "evidence_ids": ["ev_xxx"]},
    "buildability": {"value": "HIGH" or "MEDIUM" or "LOW" or "BLOCKED", "evidence_ids": ["ev_xxx"]},
    "blocker": {"value": "Description of blocker or None", "evidence_ids": ["ev_xxx"]}
  },
  "AppName2": { ... }
}

Return ONLY the JSON object. No markdown code fences. No explanation."""


def _build_batch_evidence_context(
    app_evidence_pairs: list[tuple[str, list[Evidence]]],
    max_tokens_per_app: int = 4000,
) -> str:
    """Build combined evidence context for multiple apps.

    Uses priority-based packing per app within per-app token budget.
    """
    sections = []
    for app_name, evidence_list in app_evidence_pairs:
        header = f"\n--- APP: {app_name} ---\n"
        ctx = _build_evidence_context(evidence_list, max_tokens=max_tokens_per_app)
        sections.append(header + ctx)
    return "\n".join(sections)


# ────────────────────────────────────────────────────────────
#  Batch extraction
# ────────────────────────────────────────────────────────────


async def batch_extract_from_evidence(
    app_evidence_pairs: list[tuple[str, list[Evidence]]],
) -> list[Extraction | None]:
    """Run LLM extraction on evidence from MULTIPLE apps in a single call.

    Input: list of (app_name, evidence_list) tuples  (typically 5)
    Output: list of Extraction | None  (same order as input)

    Apps that fail parsing get None and should be retried individually.
    """
    if not app_evidence_pairs:
        return []

    settings = get_settings()
    os.environ.pop("LANGCHAIN_TRACING_V2", None)

    providers = _build_providers(settings)
    if not providers:
        console.print(
            "[red]No LLM provider configured (set OPEN_AI_API_KEY or GEMINI_API_KEY)[/red]"
        )
        return [None] * len(app_evidence_pairs)

    evidence_context = _build_batch_evidence_context(app_evidence_pairs)

    app_names = [name for name, _ in app_evidence_pairs]
    prompt = (
        f"Extract structured data for the following applications:\n\n{evidence_context}"
    )

    messages = [
        SystemMessage(content=BATCH_EXTRACTION_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    batch_label = f"batch of {len(app_names)}"
    response_content = await _extract_with_fallback(providers, messages, batch_label)
    if response_content is None:
        return [None] * len(app_evidence_pairs)

    try:
        parsed = _parse_batch_extraction_response(response_content)
    except json.JSONDecodeError:
        console.print("[red]Failed to parse batch extraction response[/red]")
        return [None] * len(app_evidence_pairs)

    results: list[Extraction | None] = []
    for app_name in app_names:
        app_data = parsed.get(app_name)
        if app_data is None:
            app_data = next(
                (v for k, v in parsed.items() if k.lower() == app_name.lower()),
                None,
            )
        if app_data is None:
            console.print(
                f"  [yellow]App '{app_name}' missing from batch response[/yellow]"
            )
            results.append(None)
            continue

        try:
            extraction = Extraction(
                description=_safe_field_value(app_data, "description"),
                category=_safe_field_value(app_data, "category"),
                auth_methods=_safe_field_value(app_data, "auth_methods"),
                self_serve=_safe_field_value(app_data, "self_serve"),
                api_surface=_safe_field_value(app_data, "api_surface"),
                api_breadth=_safe_field_value(app_data, "api_breadth"),
                mcp=_safe_field_value(app_data, "mcp"),
                buildability=_safe_field_value(app_data, "buildability"),
                blocker=_safe_field_value(app_data, "blocker"),
            )
            results.append(extraction)
        except Exception as exc:
            console.print(
                f"  [yellow]Failed to parse extraction for '{app_name}': {exc}[/yellow]"
            )
            results.append(None)

    return results
