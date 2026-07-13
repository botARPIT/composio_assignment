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
    from src.config import OUTPUT_DIR
    from src.pipeline.orchestrator import load_apps
    from src.pipeline.report import generate_report
    from models.result import ResearchResult

    apps = load_apps()
    results = []

    for app in apps:
        path = OUTPUT_DIR / "apps" / app.slug / "final.json"
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
        console.print(f"[red]App '{app_name}' not found in apps.csv[/red]")
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
    console.print(f"  Confidence: {result.confidence_score}")


if __name__ == "__main__":
    app()
