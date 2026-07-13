"""Report generator — produces the self-contained HTML case study.

Uses Jinja2 to render the final HTML with embedded Plotly charts.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import orjson
from jinja2 import Environment, FileSystemLoader
from rich.console import Console

from models.result import ResearchResult
from src.config import (
    TEMPLATES_DIR,
    APPS_OUTPUT_DIR,
    CASE_STUDY_PATH,
    RESEARCH_DATA_CSV,
    APP_PAGE_FILENAME,
)
from src.pipeline.analytics import (
    build_dataframe,
    generate_charts,
    compute_summary_stats,
    compute_qa_metrics,
)

console = Console()


def _csv_to_embedded(results: list[ResearchResult]) -> str:
    """Convert results to a CSV string embedded in the HTML for the download button."""
    df = build_dataframe(results)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def _generate_app_pages(results: list[ResearchResult], env: Environment) -> None:
    """Generate individual HTML detail pages for each app."""
    template = env.get_template("app_detail.html")
    apps_dir = APPS_OUTPUT_DIR
    count = 0
    for result in results:
        if not result.extraction:
            continue
        slug = result.app.slug
        doc_urls = []
        doc_map = result.documentation_map
        if doc_map:
            for url in doc_map.all_urls:
                doc_urls.append(
                    {
                        "url": url.url,
                        "title": url.title,
                        "confidence": url.confidence,
                    }
                )
        # Deduplicate evidence by URL for display (one entry per unique page)
        seen_urls: set[str] = set()
        deduped_evidence = []
        for ev in result.evidence:
            if ev.url not in seen_urls:
                seen_urls.add(ev.url)
                deduped_evidence.append(ev)

        html = template.render(
            app=result.app,
            extraction=result.extraction,
            validation=result.validation,
            evidence=deduped_evidence,
            doc_map=doc_map,
            doc_urls=doc_urls,
            confidence=result.pipeline_confidence
            if result.pipeline_confidence > 0
            else (result.validation.score if result.validation else 0.0),
            evidence_count=len(deduped_evidence),
            browser_verification=result.browser_verification,
            human_review=result.human_review,
            final_status=result.final_status,
            human_verified=result.human_verified,
            result=result,
        )
        app_out = apps_dir / slug
        app_out.mkdir(parents=True, exist_ok=True)
        (app_out / APP_PAGE_FILENAME).write_text(html, encoding="utf-8")
        count += 1
    console.print(f"[green]Generated {count} individual app pages[/green]")


def generate_report(
    results: list[ResearchResult],
    output_path: Path | None = None,
) -> Path:
    """Generate the final HTML case study report.

    Input: list[ResearchResult]
    Output: Path to the generated HTML file
    """
    if output_path is None:
        output_path = CASE_STUDY_PATH

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build analytics
    df = build_dataframe(results)
    charts = generate_charts(df)
    stats = compute_summary_stats(df)

    # Count browser-verified apps
    browser_verified = sum(
        1
        for r in results
        if r.browser_verification and r.browser_verification.verified_fields
    )

    stats["browser_verified"] = browser_verified

    # QA metrics from human review pipeline
    qa_metrics = compute_qa_metrics(results)
    stats.update(qa_metrics)

    # Prepare table data (use effective_value for display)
    table_data = []
    for r in results:
        table_data.append(
            {
                "name": r.app.name,
                "slug": r.app.slug,
                "website": r.app.website,
                "category": r.effective_value("category") or "?",
                "description": r.effective_value("description") or "?",
                "auth_methods": r.effective_value("auth_methods") or "?",
                "self_serve": r.effective_value("self_serve") or "?",
                "api_surface": r.effective_value("api_surface") or "?",
                "api_breadth": r.effective_value("api_breadth") or "?",
                "mcp": r.effective_value("mcp") or "?",
                "buildability": r.effective_value("buildability") or "?",
                "blocker": r.effective_value("blocker") or "?",
                "confidence": r.pipeline_confidence
                if r.pipeline_confidence > 0
                else (r.validation.score if r.validation else 0.0),
                "validation_status": r.validation.status
                if r.validation
                else "INCOMPLETE",
                "final_status": r.final_status,
            }
        )

    # Export CSV
    csv_path = RESEARCH_DATA_CSV
    df.to_csv(csv_path, index=False)
    console.print(f"[green]CSV exported to {csv_path}[/green]")

    # Embedded CSV for download button
    csv_data = _csv_to_embedded(results)

    # Render HTML
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("report.html")

    html = template.render(
        stats=stats,
        qa_metrics=qa_metrics,
        charts=charts,
        table_data=table_data,
        total_apps=len(results),
        csv_data=csv_data,
    )

    output_path.write_text(html, encoding="utf-8")
    # Also write as index.html so Vercel serves it at the root URL
    index_path = output_path.parent / "index.html"
    index_path.write_text(html, encoding="utf-8")
    console.print(f"[green]Report generated at {output_path}[/green]")

    # Generate individual app detail pages
    _generate_app_pages(results, env)

    return output_path
