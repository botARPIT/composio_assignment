"""CLI entry point for the research pipeline."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="research",
    help="Composio AI Research Pipeline — evidence-driven SaaS app analysis",
    add_completion=False,
)
console = Console()


@app.command()
def run(
    max_apps: Optional[int] = typer.Option(
        None, "--max", "-n", help="Max apps to process"
    ),
    skip_completed: bool = typer.Option(
        True, "--skip-completed/--no-skip", help="Skip already completed apps"
    ),
    skip_browser: bool = typer.Option(
        False, "--skip-browser", help="Skip browser verification"
    ),
    report_only: bool = typer.Option(
        False, "--report-only", help="Only generate report from existing data"
    ),
    batch: bool = typer.Option(
        False, "--batch", help="Batch 5 apps per LLM call to reduce rate limits"
    ),
):
    """Run the full research pipeline."""
    from src.config import get_settings

    settings = get_settings()
    if skip_browser:
        settings.skip_browser_verification = True

    if report_only:
        _generate_report_only()
        return

    asyncio.run(_run_pipeline(max_apps, skip_completed, batch))


async def _run_pipeline(
    max_apps: int | None, skip_completed: bool, batch: bool = False
):
    """Run the full pipeline asynchronously."""
    from src.pipeline.orchestrator import run_batch, load_apps
    from src.pipeline.report import generate_report

    apps = load_apps()
    results = await run_batch(
        apps, max_apps=max_apps, skip_completed=skip_completed, batch_mode=batch
    )

    # Generate report
    console.print("\n[bold]Generating report...[/bold]")
    report_path = generate_report(results)
    console.print(f"\n[bold green]✓ Done![/bold green] Report: {report_path}")


def _generate_report_only():
    """Generate report from cached results."""
    import orjson
    from src.config import APPS_OUTPUT_DIR, RESULT_FILENAME
    from src.pipeline.orchestrator import load_apps
    from src.pipeline.report import generate_report
    from models.result import ResearchResult

    apps = load_apps()
    results = []

    for app in apps:
        path = APPS_OUTPUT_DIR / app.slug / RESULT_FILENAME
        if path.exists():
            try:
                data = orjson.loads(path.read_bytes())
                results.append(ResearchResult.model_validate(data))
            except Exception as exc:
                console.print(f"[yellow]Failed to load {app.name}: {exc}[/yellow]")

    if not results:
        console.print("[red]No completed results found. Run the pipeline first.[/red]")
        raise typer.Exit(1)

    console.print(f"Loaded {len(results)} results from cache")
    generate_report(results)


@app.command()
def test(
    app_name: str = typer.Argument("GitHub", help="App name to test"),
):
    """Test the pipeline with a single app."""
    asyncio.run(_test_single(app_name))


async def _test_single(app_name: str):
    """Test pipeline on a single app."""
    from src.pipeline.orchestrator import load_apps, process_single_app

    apps = load_apps()
    target = next((a for a in apps if a.name.lower() == app_name.lower()), None)

    if not target:
        from src.config import APPS_CSV_PATH

        console.print(f"[red]App '{app_name}' not found in {APPS_CSV_PATH.name}[/red]")
        raise typer.Exit(1)

    result = await process_single_app(target)

    # Print result summary
    console.print(f"\n[bold]Result for {target.name}:[/bold]")
    console.print(f"  Evidence pieces: {len(result.evidence)}")
    if result.extraction:
        console.print(f"  Category: {result.extraction.category.value}")
        console.print(f"  Auth: {result.extraction.auth_methods.value}")
        console.print(f"  Self-serve: {result.extraction.self_serve.value}")
        console.print(f"  API surface: {result.extraction.api_surface.value}")
        console.print(f"  MCP: {result.extraction.mcp.value}")
        console.print(f"  Buildability: {result.extraction.buildability.value}")
    if result.validation:
        console.print(
            f"  Validation: {result.validation.status} ({result.validation.score})"
        )
    console.print(f"  Confidence: {result.pipeline_confidence}")


@app.command()
def review(
    app_name: Optional[str] = typer.Option(
        None, "--app", "-a", help="Specific app to review (bypasses queue)"
    ),
    reviewer: Optional[str] = typer.Option(
        None, "--reviewer", "-r", help="Your name for the audit trail"
    ),
):
    """Run a human verification loop on flagged apps."""
    import getpass
    import os
    import orjson
    from datetime import datetime, timezone
    from rich.prompt import Confirm, Prompt
    from src.config import APPS_OUTPUT_DIR, RESULT_FILENAME
    from src.pipeline.orchestrator import load_apps
    from models.result import ResearchResult, HumanReview

    apps = load_apps()
    completed_results: list[tuple[Path, ResearchResult]] = []

    for app_meta in apps:
        if app_name and app_meta.name.lower() != app_name.lower():
            continue
        path = APPS_OUTPUT_DIR / app_meta.slug / RESULT_FILENAME
        if path.exists():
            try:
                data = orjson.loads(path.read_bytes())
                result = ResearchResult.model_validate(data)
                if result.is_complete:
                    completed_results.append((path, result))
            except Exception:
                continue

    if not completed_results:
        console.print("[red]No completed results found. Run the pipeline first.[/red]")
        raise typer.Exit(1)

    # Filter to flagged apps (or use specific app)
    if app_name:
        flagged = completed_results
    else:
        flagged = [(p, r) for p, r in completed_results if r.human_review.required]

    if not flagged:
        console.print(
            "[green]No apps require human review.[/green] "
            "Use [bold]--app <name>[/bold] to manually inspect a specific app."
        )
        return

    if not reviewer:
        reviewer = getpass.getuser() or os.environ.get("USER", "anonymous")

    console.print(
        f"\n[bold]Loaded {len(completed_results)} apps — {len(flagged)} flagged for review[/bold]"
    )
    console.print(f"[dim]Reviewer: {reviewer}[/dim]\n")

    for idx, (path, result) in enumerate(flagged, 1):
        console.rule(f"[{idx}/{len(flagged)}] {result.app.name}")

        ext = result.extraction
        if not ext:
            continue

        overrides: dict[str, dict] = {}
        inspected_fields: list[str] = []

        # Determine which fields to inspect (all extraction fields)
        review_fields = [
            ("auth_methods", "Auth Methods"),
            ("self_serve", "Self Serve"),
            ("api_surface", "API Surface"),
            ("api_breadth", "API Breadth"),
            ("mcp", "MCP Readiness"),
            ("buildability", "Buildability"),
        ]

        # Find evidence excerpts for context
        evidence_map: dict[str, str] = {}
        for ev in result.evidence[:5]:
            snippet = ev.content[:200].replace("\n", " ").strip() if ev.content else ""
            evidence_map[ev.url] = snippet[:200]

        for field_name, display_name in review_fields:
            pipeline_value = getattr(ext, field_name).value
            evidence_ids = getattr(ext, field_name).evidence_ids

            # Find supporting evidence
            supporting_evidence = []
            for eid in evidence_ids:
                for ev in result.evidence:
                    if ev.id == eid:
                        supporting_evidence.append(ev)
                        break

            console.print(f"\n[bold cyan]  {display_name}[/bold cyan]")
            console.print(f"    Pipeline:    [white]{pipeline_value}[/white]")
            console.print(
                f"    Confidence:  [yellow]{result.pipeline_confidence:.2f}[/yellow]"
                f"    [dim]({'Flagged' if result.human_review.required else 'Manual'})[/dim]"
            )

            # Show evidence excerpts
            for ev in supporting_evidence[:2]:
                snippet = (
                    ev.content[:200].replace("\n", " ").strip() if ev.content else ""
                )
                console.print(f"    Evidence:    [link={ev.url}]{ev.url}[/link]")
                if snippet:
                    console.print(f'    Excerpt:     [dim]"{snippet}"[/dim]')

            if not supporting_evidence:
                console.print("    Evidence:    [dim]No citations for this field[/dim]")

            console.print("")
            correct = Confirm.ask(f"    Correct?", default=True)

            if not correct:
                new_value = Prompt.ask(
                    f"    Enter correct value",
                    default=str(pipeline_value),
                )
                override_reason = Prompt.ask(
                    f"    Reason for override",
                    default="",
                )
                overrides[field_name] = {
                    "old": pipeline_value,
                    "new": new_value,
                    "reason": override_reason,
                }
                console.print(
                    f"    [yellow]✓ Override recorded: {pipeline_value} → {new_value}[/yellow]"
                )

            inspected_fields.append(field_name)

        # Finalize review
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        notes = Prompt.ask(
            "\n[bold]Review notes[/bold] (optional)",
            default="",
        )

        result.human_verified = True
        result.human_review = HumanReview(
            required=True,
            reason=result.human_review.reason,  # preserve original triggers
            status="completed",
            reviewer=reviewer,
            reviewed_at=now,
            overrides=overrides,
            notes=notes,
        )
        result.final_status = "HUMAN_MODIFIED" if overrides else "HUMAN_VERIFIED"

        # Save
        path.write_bytes(
            orjson.dumps(
                result.model_dump(mode="json"),
                option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS,
            )
        )

        console.print(
            f"\n[bold green]✓ {result.app.name}[/bold green] → {result.final_status}"
        )
        if overrides:
            console.print(f"  [yellow]{len(overrides)} override(s) applied[/yellow]")

    # Summary
    modified = sum(1 for _, r in flagged if r.human_review.overrides)
    console.print(
        f"\n[bold green]Review session complete![/bold green] "
        f"{len(flagged)} reviewed, {modified} modified."
    )


if __name__ == "__main__":
    app()
