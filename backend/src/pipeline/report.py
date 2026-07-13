"""Report generator — produces the self-contained HTML case study.

Uses Jinja2 to render the final HTML with embedded Plotly charts.
"""

from __future__ import annotations

from pathlib import Path

import orjson
from jinja2 import Environment, FileSystemLoader
from rich.console import Console

from models.result import ResearchResult
from src.config import TEMPLATES_DIR, OUTPUT_DIR
from src.pipeline.analytics import build_dataframe, generate_charts, compute_summary_stats

console = Console()


def generate_report(
    results: list[ResearchResult],
    output_path: Path | None = None,
) -> Path:
    """Generate the final HTML case study report.

    Input: list[ResearchResult]
    Output: Path to the generated HTML file
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "case_study.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build analytics
    df = build_dataframe(results)
    charts = generate_charts(df)
    stats = compute_summary_stats(df)

    # Prepare table data
    table_data = []
    for _, row in df.iterrows():
        table_data.append({
            "name": row["name"],
            "website": row["website"],
            "category": row["category"],
            "description": row["description"],
            "auth_methods": row["auth_methods"],
            "self_serve": row["self_serve"],
            "api_surface": row["api_surface"],
            "api_breadth": row["api_breadth"],
            "mcp": row["mcp"],
            "buildability": row["buildability"],
            "blocker": row["blocker"],
            "confidence": row["confidence"],
            "validation_status": row["validation_status"],
        })

    # Export CSV
    csv_path = OUTPUT_DIR / "research_data.csv"
    df.to_csv(csv_path, index=False)
    console.print(f"[green]CSV exported to {csv_path}[/green]")

    # Render HTML
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("report.html")

    html = template.render(
        stats=stats,
        charts=charts,
        table_data=table_data,
        total_apps=len(results),
    )

    output_path.write_text(html, encoding="utf-8")
    console.print(f"[green]Report generated at {output_path}[/green]")
    return output_path
