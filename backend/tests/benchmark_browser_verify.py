"""Benchmark: Playwright vs Composio browser_tool for Stage 5 verification.

Runs both implementations on the same 5 apps that previously had non-empty
browser verification results, captures output, timing, and field match quality,
then prints a side-by-side comparison table.

Usage:
    cd backend
    uv run python -m tests.benchmark_browser_verify
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

APPS_DIR = Path(__file__).parent.parent / "output" / "apps"

# Apps where Playwright found something — best candidates for comparison
TEST_APPS = ["aircall", "airtable", "magento", "jira", "gumroad"]


def load_result(app_slug: str):
    """Load a ResearchResult from final.json."""
    from models.result import ResearchResult

    fj = APPS_DIR / app_slug / "final.json"
    data = json.loads(fj.read_bytes())
    # Clear existing browser_verification so both runs start fresh
    data["browser_verification"] = None
    return ResearchResult.model_validate(data)


async def run_playwright(result) -> tuple[dict, float]:
    from src.pipeline.browser_verify import verify_with_browser

    t0 = time.perf_counter()
    bv = await verify_with_browser(result)
    elapsed = time.perf_counter() - t0
    return {
        "verified_fields": bv.verified_fields,
        "corrections": bv.corrections,
        "notes": bv.notes,
    }, elapsed


async def run_composio(result) -> tuple[dict, float]:
    from src.pipeline.browser_verify_composio import verify_with_browser

    t0 = time.perf_counter()
    bv = await verify_with_browser(result)
    elapsed = time.perf_counter() - t0
    return {
        "verified_fields": bv.verified_fields,
        "corrections": bv.corrections,
        "notes": bv.notes,
    }, elapsed


async def main() -> None:
    print("\n" + "=" * 90)
    print("BENCHMARK: Playwright  vs  Composio browser_tool  (Stage 5 — Browser Verify)")
    print("=" * 90)
    print(f"{'App':<20} {'Method':<12} {'Fields Found':<30} {'Time (s)':>8}  Notes")
    print("-" * 90)

    totals: dict[str, dict] = {
        "playwright": {"fields": 0, "time": 0.0, "errors": 0},
        "composio": {"fields": 0, "time": 0.0, "errors": 0},
    }

    for app_slug in TEST_APPS:
        try:
            result = load_result(app_slug)
        except Exception as exc:
            print(f"  {app_slug:<18} LOAD ERROR: {exc}")
            continue

        # Run Playwright
        try:
            pw_out, pw_time = await run_playwright(result)
            pw_fields = ", ".join(pw_out["verified_fields"]) or "—"
            totals["playwright"]["fields"] += len(pw_out["verified_fields"])
            totals["playwright"]["time"] += pw_time
        except Exception as exc:
            pw_out, pw_time = {"verified_fields": [], "notes": str(exc)}, 0.0
            pw_fields = "ERROR"
            totals["playwright"]["errors"] += 1

        # Run Composio
        try:
            co_out, co_time = await run_composio(result)
            co_fields = ", ".join(co_out["verified_fields"]) or "—"
            totals["composio"]["fields"] += len(co_out["verified_fields"])
            totals["composio"]["time"] += co_time
        except Exception as exc:
            co_out, co_time = {"verified_fields": [], "notes": str(exc)}, 0.0
            co_fields = "ERROR"
            totals["composio"]["errors"] += 1

        print(f"  {app_slug:<18} {'playwright':<12} {pw_fields:<30} {pw_time:>7.2f}s  {str(pw_out.get('notes') or '')[:30]}")
        print(f"  {'':18} {'composio':<12} {co_fields:<30} {co_time:>7.2f}s  {str(co_out.get('notes') or '')[:50]}")
        print()

    print("-" * 90)
    print(f"  {'TOTALS':<18} {'playwright':<12} fields={totals['playwright']['fields']}  time={totals['playwright']['time']:.2f}s  errors={totals['playwright']['errors']}")
    print(f"  {'':18} {'composio':<12} fields={totals['composio']['fields']}  time={totals['composio']['time']:.2f}s  errors={totals['composio']['errors']}")
    print("=" * 90)
    print()

    # Verdict
    pw_score = totals["playwright"]["fields"]
    co_score = totals["composio"]["fields"]
    pw_time = totals["playwright"]["time"]
    co_time = totals["composio"]["time"]

    print("VERDICT")
    print("-------")
    if co_score >= pw_score and co_time <= pw_time * 1.5:
        print("✅ Composio browser_tool matches or exceeds Playwright — safe to promote to main.")
    elif pw_score > co_score:
        print(f"⚠️  Playwright found more fields ({pw_score} vs {co_score}). Playwright remains better choice.")
    else:
        print(f"⚠️  Composio slower ({co_time:.1f}s vs {pw_time:.1f}s) but same field coverage. Acceptable trade-off.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
