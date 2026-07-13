"""Orchestrator — runs the full pipeline for a single app or batch.

IMPORTANT: uses models.evidence.Evidence for type hints in batch processing.

Pipeline stages (strict order):
1. Discovery   → DocumentationMap
2. Collection  → list[Evidence]
3. Extraction  → Extraction
4. Validation  → ValidationSummary   (deterministic, no LLM)
5. Verification → BrowserVerification (failures only)

Each stage consumes ONLY the output of the previous stage.
No stage mutates another stage's output.
"""

from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path

import orjson
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID

from models.app import AppMetadata
from models.extraction import FieldValue
from models.result import ResearchResult, HumanReview, compute_review_reasons
from src.config import (
    APPS_CSV_PATH,
    APPS_OUTPUT_DIR,
    RESULT_FILENAME,
    get_settings,
)
from src.pipeline.discovery import discover_documentation
from src.pipeline.collector import collect_evidence
from src.pipeline.extraction import extract_from_evidence, batch_extract_from_evidence
from src.pipeline.validator import validate_extraction
from src.pipeline.browser_verify import verify_with_browser

console = Console()


class RateLimiter:
    """Token-bucket rate limiter for Gemini API."""

    def __init__(self, rpm: int = 8):
        self.window = 60.0 / rpm
        self.last_call = 0.0

    async def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_call
        if elapsed < self.window:
            delay = self.window - elapsed
            console.print(f"  [dim]Rate limit: waiting {delay:.1f}s[/dim]")
            await asyncio.sleep(delay)
        self.last_call = time.monotonic()


def load_apps(csv_path: Path | None = None) -> list[AppMetadata]:
    """Load applications from CSV."""
    path = csv_path or APPS_CSV_PATH
    apps = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            apps.append(
                AppMetadata(
                    id=int(row["id"]),
                    name=row["name"],
                    website=row["website"],
                    category_hint=row.get("category_hint"),
                )
            )
    return apps


def _app_output_dir(app: AppMetadata) -> Path:
    """Get the output directory for an app."""
    d = APPS_OUTPUT_DIR / app.slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cached_result(app: AppMetadata) -> ResearchResult | None:
    """Load a previously completed result if it exists."""
    path = _app_output_dir(app) / RESULT_FILENAME
    if path.exists():
        try:
            data = orjson.loads(path.read_bytes())
            return ResearchResult.model_validate(data)
        except Exception:
            return None
    return None


def _save_result(result: ResearchResult) -> None:
    """Persist a result to disk."""
    path = _app_output_dir(result.app) / RESULT_FILENAME
    path.write_bytes(
        orjson.dumps(
            result.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS,
        )
    )


def _mark_failed(result: ResearchResult, reason: str) -> None:
    """Mark an incomplete pipeline result as failed and review-required."""
    result.pipeline_confidence = (
        round(result.validation.score, 2) if result.validation else 0.0
    )
    result.final_status = "FAILED"
    result.human_review = HumanReview(required=True, reason=[reason], status="pending")


async def process_single_app(app: AppMetadata) -> ResearchResult:
    """Run the full pipeline for a single application.

    Returns a fully populated ResearchResult.
    """
    result = ResearchResult(app=app)

    # Stage 1: Discovery
    console.print(f"  [cyan]1/5 Discovery[/cyan] — finding docs for {app.name}")
    try:
        doc_map = await discover_documentation(app)
        result.documentation_map = doc_map
        console.print(
            f"      Found {doc_map.url_count} URLs ({doc_map.official_url_count} official)"
        )
    except Exception as exc:
        console.print(f"  [red]Discovery failed: {exc}[/red]")
        _mark_failed(result, f"Discovery failed: {exc}")
        _save_result(result)
        return result

    # Stage 2: Evidence Collection
    console.print(
        f"  [cyan]2/5 Evidence[/cyan] — collecting from {doc_map.url_count} URLs"
    )
    try:
        evidence_list = await collect_evidence(doc_map)
        result.evidence = evidence_list
        console.print(f"      Collected {len(evidence_list)} evidence pieces")
    except Exception as exc:
        console.print(f"  [red]Evidence collection failed: {exc}[/red]")
        _mark_failed(result, f"Evidence collection failed: {exc}")
        _save_result(result)
        return result

    if not evidence_list:
        console.print(f"  [yellow]No evidence — skipping extraction[/yellow]")
        _mark_failed(result, "No evidence collected")
        _save_result(result)
        return result

    # Stage 3: Extraction (LLM)
    console.print(f"  [cyan]3/5 Extraction[/cyan] — LLM semantic extraction")
    try:
        extraction = await extract_from_evidence(app.name, evidence_list)
        result.extraction = extraction
        if extraction:
            console.print(
                f"      Auth: {extraction.auth_methods.value} | Self-serve: {extraction.self_serve.value}"
            )
    except Exception as exc:
        console.print(f"  [red]Extraction failed: {exc}[/red]")
        _mark_failed(result, f"Extraction failed: {exc}")
        _save_result(result)
        return result

    if not extraction:
        _mark_failed(result, "Extraction returned no result")
        _save_result(result)
        return result

    # Overwrite category from CSV — no need for LLM to infer it
    extraction.category = FieldValue(
        value=app.category_hint or "UNKNOWN", evidence_ids=[]
    )

    # Stage 4: Validation (deterministic Python)
    console.print(f"  [cyan]4/5 Validation[/cyan] — deterministic checks")
    validation = validate_extraction(extraction, evidence_list)
    result.validation = validation
    console.print(
        f"      Status: {validation.status} | Score: {validation.score} | {validation.fields_supported}/{validation.fields_checked} fields"
    )

    # Stage 5: Browser Verification (failures only)
    if validation.needs_verification:
        console.print(
            f"  [cyan]5/5 Verification[/cyan] — browser check for disputed fields"
        )
        try:
            browser_result = await verify_with_browser(result)
            result.browser_verification = browser_result
            if browser_result.verified_fields:
                console.print(
                    f"      Verified: {', '.join(browser_result.verified_fields)}"
                )
            if browser_result.corrections:
                console.print(f"      Corrections: {browser_result.corrections}")
        except Exception as exc:
            console.print(f"  [yellow]Browser verification failed: {exc}[/yellow]")
    else:
        console.print(f"  [green]5/5 Verification[/green] — not needed (SUPPORTED)")

    # Stage 6: Human Review Queue
    result.pipeline_confidence = (
        round(result.validation.score, 2) if result.validation else 0.0
    )
    if result.extraction is None:
        result.final_status = "FAILED"
    else:
        reasons = compute_review_reasons(result)
        if reasons:
            result.human_review = HumanReview(
                required=True, reason=reasons, status="pending"
            )
            result.final_status = "PENDING_REVIEW"
            console.print(
                f"  [yellow]⚠ Queued for review: {len(reasons)} trigger(s)[/yellow]"
            )
        else:
            result.final_status = "AUTO_ACCEPTED"

    # Persist
    _save_result(result)
    return result


async def run_batch(
    apps: list[AppMetadata] | None = None,
    max_apps: int | None = None,
    skip_completed: bool = True,
    batch_mode: bool = False,
) -> list[ResearchResult]:
    """Run the pipeline for a batch of applications.

    In sequential mode (default): processes one app at a time with RateLimiter gaps.
    In batch mode: groups apps into batches of 5, sends one LLM call per batch.

    Caches results to support resumability.
    """
    if apps is None:
        apps = load_apps()

    if max_apps:
        apps = apps[:max_apps]

    if batch_mode:
        return await _run_batch_in_batches(apps, skip_completed=skip_completed)

    return await _run_batch_sequentially(apps, skip_completed=skip_completed)


async def _run_batch_sequentially(
    apps: list[AppMetadata],
    skip_completed: bool = True,
) -> list[ResearchResult]:
    """Run pipeline one app at a time with rate limiting."""
    rate_limiter = RateLimiter(rpm=8)
    results: list[ResearchResult] = []

    console.print(f"\n[bold]Starting sequential pipeline for {len(apps)} apps[/bold]\n")

    for i, app in enumerate(apps, 1):
        console.rule(f"[{i}/{len(apps)}] {app.name}")

        if skip_completed:
            cached = _load_cached_result(app)
            if cached and cached.is_complete:
                console.print(
                    f"  [dim]Cached — confidence {cached.pipeline_confidence}[/dim]"
                )
                results.append(cached)
                continue

        await rate_limiter.wait()

        try:
            result = await process_single_app(app)
            results.append(result)
            console.print(
                f"  [bold green]✓[/bold green] Confidence: {result.pipeline_confidence}"
            )
        except Exception as exc:
            console.print(f"  [bold red]✗ Fatal error: {exc}[/bold red]")
            results.append(ResearchResult(app=app))

    complete = sum(1 for r in results if r.is_complete)
    high_conf = sum(1 for r in results if r.pipeline_confidence >= 0.75)
    console.print(
        f"\n[bold]Pipeline complete:[/bold] {complete}/{len(results)} complete, {high_conf} high-confidence"
    )
    return results


async def _run_batch_in_batches(
    apps: list[AppMetadata],
    skip_completed: bool = True,
    batch_size: int = 5,
) -> list[ResearchResult]:
    """Run pipeline in batches — one LLM extraction call per batch of apps."""
    rate_limiter = RateLimiter(rpm=8)
    results: list[ResearchResult] = []
    uncached_apps: list[AppMetadata] = []

    console.print(
        f"\n[bold]Starting batched pipeline for {len(apps)} apps (batch size: {batch_size})[/bold]\n"
    )

    # Separate cached from uncached
    for app in apps:
        if skip_completed:
            cached = _load_cached_result(app)
            if cached and cached.is_complete:
                console.print(
                    f"  [dim]{app.name}: cached — confidence {cached.pipeline_confidence}[/dim]"
                )
                results.append(cached)
                continue
        uncached_apps.append(app)

    if not uncached_apps:
        console.print("[bold green]All apps cached — nothing to do[/bold green]")
        return results

    # Process in batches
    total_batches = (len(uncached_apps) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(uncached_apps), batch_size):
        batch = uncached_apps[batch_idx : batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1
        console.rule(f"Batch [{batch_num}/{total_batches}] — {len(batch)} apps")

        # Stage 1+2: Discovery + Evidence for each app in batch
        from models.evidence import Evidence

        app_evidence_pairs: list[tuple[str, list[Evidence]]] = []
        batch_results: list[ResearchResult] = []

        for app in batch:
            result = ResearchResult(app=app)
            batch_results.append(result)

            console.print(f"  [cyan]1/2 Discovery[/cyan] — finding docs for {app.name}")
            try:
                doc_map = await discover_documentation(app)
                result.documentation_map = doc_map
                console.print(
                    f"      Found {doc_map.url_count} URLs ({doc_map.official_url_count} official)"
                )
            except Exception as exc:
                console.print(f"  [red]Discovery failed for {app.name}: {exc}[/red]")
                _mark_failed(result, f"Discovery failed: {exc}")
                _save_result(result)
                app_evidence_pairs.append((app.name, []))
                continue

            console.print(
                f"  [cyan]2/2 Evidence[/cyan] — collecting from {doc_map.url_count} URLs"
            )
            try:
                evidence_list = await collect_evidence(doc_map)
                result.evidence = evidence_list
                console.print(f"      Collected {len(evidence_list)} evidence pieces")
            except Exception as exc:
                console.print(
                    f"  [red]Evidence collection failed for {app.name}: {exc}[/red]"
                )
                _mark_failed(result, f"Evidence collection failed: {exc}")
                _save_result(result)
                app_evidence_pairs.append((app.name, []))
                continue

            app_evidence_pairs.append((app.name, evidence_list))

        # Stage 3: Batch Extraction (single LLM call)
        valid_pairs = [(n, ev) for n, ev in app_evidence_pairs if ev]
        console.print(
            f"  [cyan]Batch Extraction[/cyan] — LLM extraction for {len(valid_pairs)} apps in one call"
        )

        if valid_pairs:
            await rate_limiter.wait()
            extractions = await batch_extract_from_evidence(valid_pairs)

            # Map extractions back to results
            for (app_name, _), extraction in zip(valid_pairs, extractions):
                # Find the matching result
                for r in batch_results:
                    if r.app.name == app_name:
                        r.extraction = extraction
                        if extraction:
                            console.print(
                                f"      {app_name}: Auth: {extraction.auth_methods.value} | Self-serve: {extraction.self_serve.value}"
                            )
                        break

            # Retry failed apps individually
            for idx, (app_name, evidence_list) in enumerate(valid_pairs):
                if extractions[idx] is None and evidence_list:
                    console.print(
                        f"  [yellow]Retrying {app_name} individually...[/yellow]"
                    )
                    try:
                        extraction = await extract_from_evidence(
                            app_name, evidence_list
                        )
                        for r in batch_results:
                            if r.app.name == app_name:
                                r.extraction = extraction
                                break
                    except Exception as exc:
                        console.print(
                            f"  [red]Individual retry failed for {app_name}: {exc}[/red]"
                        )

        # Overwrite category from CSV for all batch results
        for result in batch_results:
            if result.extraction:
                result.extraction.category = FieldValue(
                    value=result.app.category_hint or "UNKNOWN",
                    evidence_ids=[],
                )

        # Stage 4+5: Validation + Browser Verification per app
        for result in batch_results:
            if not result.extraction or not result.evidence:
                if not result.evidence:
                    _mark_failed(result, "No evidence collected")
                else:
                    _mark_failed(result, "Extraction returned no result")
                _save_result(result)
                results.append(result)
                continue

            console.print(f"  [cyan]Validation[/cyan] — {result.app.name}")
            validation = validate_extraction(result.extraction, result.evidence)
            result.validation = validation
            console.print(
                f"      Status: {validation.status} | Score: {validation.score}"
            )

            if validation.needs_verification:
                console.print(
                    f"  [cyan]Verification[/cyan] — browser check for {result.app.name}"
                )
                try:
                    browser_result = await verify_with_browser(result)
                    result.browser_verification = browser_result
                except Exception as exc:
                    console.print(
                        f"  [yellow]Browser verification failed: {exc}[/yellow]"
                    )

            # Stage 6: Human Review Queue
            result.pipeline_confidence = (
                round(result.validation.score, 2) if result.validation else 0.0
            )
            reasons = compute_review_reasons(result)
            if reasons:
                result.human_review = HumanReview(
                    required=True, reason=reasons, status="pending"
                )
                result.final_status = "PENDING_REVIEW"
            else:
                result.final_status = "AUTO_ACCEPTED"

            _save_result(result)
            results.append(result)
            console.print(
                f"  [bold green]✓ {result.app.name}[/bold green] Confidence: {result.pipeline_confidence}"
            )

        # Rate limit between batches
        if batch_idx + batch_size < len(uncached_apps):
            await rate_limiter.wait()

    complete = sum(1 for r in results if r.is_complete)
    high_conf = sum(1 for r in results if r.pipeline_confidence >= 0.75)
    console.print(
        f"\n[bold]Pipeline complete:[/bold] {complete}/{len(results)} complete, {high_conf} high-confidence"
    )
    return results
