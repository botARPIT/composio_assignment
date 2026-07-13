"""Human review web server — evidence-driven QA interface.

Usage:
    python review_server.py

Opens http://localhost:8000/review to review flagged apps.
Review results are saved to output/apps/{slug}/final.json
"""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import orjson

from models.result import ResearchResult, HumanReview

# ── Paths ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output"
APPS_OUTPUT_DIR = OUTPUT_DIR / "apps"
RESULT_FILENAME = "final.json"
HOST = "0.0.0.0"
PORT = 8000


# ── Data helpers ───────────────────────────────────────────────────


def escape_html(s: object) -> str:
    """Escape HTML special characters."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _load_all_results() -> list[tuple[str, ResearchResult]]:
    """Load all completed results keyed by slug."""
    results: list[tuple[str, ResearchResult]] = []
    if not APPS_OUTPUT_DIR.exists():
        return results
    for slug_dir in sorted(APPS_OUTPUT_DIR.iterdir()):
        json_path = slug_dir / RESULT_FILENAME
        if not json_path.exists():
            continue
        try:
            data = orjson.loads(json_path.read_bytes())
            r = ResearchResult.model_validate(data)
            # Compute effective confidence
            r.pipeline_confidence = (
                r.pipeline_confidence
                if r.pipeline_confidence > 0
                else (r.validation.score if r.validation else 0.0)
            )
            results.append((slug_dir.name, r))
        except Exception:
            continue
    return results


# ── HTML page builders ─────────────────────────────────────────────


def _build_review_dashboard() -> str:
    """Generate the review dashboard HTML."""
    results = _load_all_results()
    flagged = [(s, r) for s, r in results if r.human_review.required]
    completed = [(s, r) for s, r in results if r.human_review.status == "completed"]

    app_rows = ""
    for slug, r in flagged:
        status_class = {
            "pending": "low",
            "in_progress": "medium",
            "completed": "high",
        }.get(r.human_review.status, "low")

        reasons_html = "".join(
            f'<li class="reason-item">{reason}</li>' for reason in r.human_review.reason
        )

        reviewer = r.human_review.reviewer or "—"
        reviewed_at = r.human_review.reviewed_at or "—"

        app_rows += f"""
        <a href="/review/{slug}" class="app-card">
            <div class="app-card-header">
                <span class="app-name">{r.app.name}</span>
                <span class="badge badge-{status_class}">{r.human_review.status}</span>
                <span class="badge badge-{"high" if r.pipeline_confidence >= 0.75 else "medium" if r.pipeline_confidence >= 0.5 else "low"}">{r.pipeline_confidence:.0%}</span>
            </div>
            {f'<ul class="reason-list">{reasons_html}</ul>' if reasons_html else ""}
            <div class="app-card-meta">
                <span>Overrides: {len(r.human_review.overrides)}</span>
                <span>Reviewer: {reviewer}</span>
                <span>{reviewed_at[:10] if reviewed_at != "—" else "—"}</span>
            </div>
        </a>
        """

    if not app_rows:
        app_rows = '<p class="empty-state">No apps require review.</p>'

    stats_html = f"""
    <div class="stats-row">
        <div class="stat-card"><span class="stat-val">{len(results)}</span><span class="stat-lbl">Total Apps</span></div>
        <div class="stat-card"><span class="stat-val">{len(flagged)}</span><span class="stat-lbl">Flagged</span></div>
        <div class="stat-card"><span class="stat-val">{len(completed)}</span><span class="stat-lbl">Reviewed</span></div>
        <div class="stat-card"><span class="stat-val">{sum(1 for _, r in flagged if r.human_review.overrides)}</span><span class="stat-lbl">Modified</span></div>
    </div>
    """

    return _base_html(
        f"""
    <style>
        .stats-row {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
        .stat-card {{ flex: 1; min-width: 120px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; text-align: center; }}
        .stat-val {{ display: block; font-size: 2rem; font-weight: 700; color: var(--text-h); font-family: var(--font-mono); }}
        .stat-lbl {{ font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; margin-top: 4px; }}
        .app-card {{ display: block; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 12px; text-decoration: none; color: inherit; transition: border-color 0.2s; }}
        .app-card:hover {{ border-color: var(--accent); }}
        .app-card-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }}
        .app-name {{ font-size: 1.1rem; font-weight: 600; color: var(--text-h); flex: 1; }}
        .reason-list {{ list-style: none; margin: 8px 0; padding: 0; }}
        .reason-item {{ font-size: 0.85rem; color: var(--status-medium); padding: 4px 0; }}
        .reason-item::before {{ content: "⚠ "; }}
        .app-card-meta {{ display: flex; gap: 16px; font-size: 0.8rem; color: var(--text-muted); }}
        .empty-state {{ color: var(--text-muted); text-align: center; padding: 60px 20px; font-size: 1.1rem; }}
    </style>
    <div class="container">
        <nav class="top-nav">
            <a href="/" class="nav-link">← Report</a>
            <span class="nav-title">Human QA Dashboard</span>
            <a href="/review" class="nav-link active">Review</a>
        </nav>
        <h1 class="page-title">Human Review Queue</h1>
        {stats_html}
        <div class="app-list">
            {app_rows}
        </div>
    </div>
    """,
        title="Human QA — Review Queue",
    )


def _build_review_app(slug: str) -> str:
    """Generate the per-app review page showing ALL data the pipeline has."""
    results = _load_all_results()
    match = next(((s, r) for s, r in results if s == slug), None)
    if not match:
        return _base_html(
            f"""
        <div class="container">
            <p style="color: var(--status-low); font-size: 1.2rem;">App '{slug}' not found.</p>
            <a href="/review">← Back to queue</a>
        </div>
        """,
            title="App Not Found",
        )

    _, r = match
    ext = r.extraction

    # ── App Metadata ─────────────────────────────────────────────
    meta = r.app
    meta_html = f"""
    <div class="data-section">
        <div class="meta-grid">
            <div class="meta-item"><span class="meta-label">App ID</span><span class="meta-val">{meta.id}</span></div>
            <div class="meta-item"><span class="meta-label">Name</span><span class="meta-val">{meta.name}</span></div>
            <div class="meta-item"><span class="meta-label">Slug</span><span class="meta-val">{slug}</span></div>
            <div class="meta-item"><span class="meta-label">Website</span><span class="meta-val"><a href="{meta.website}" target="_blank">{meta.website}</a></span></div>
            <div class="meta-item"><span class="meta-label">Category</span><span class="meta-val">{meta.category_hint or "—"}</span></div>
            <div class="meta-item"><span class="meta-label">Status</span><span class="meta-val"><span class="badge badge-{"high" if r.final_status in ("AUTO_ACCEPTED", "HUMAN_VERIFIED") else "medium" if r.final_status == "HUMAN_MODIFIED" else "low"}">{r.final_status}</span></span></div>
            <div class="meta-item"><span class="meta-label">Confidence</span><span class="meta-val">{r.pipeline_confidence:.0%}</span></div>
        </div>
    </div>
    """

    # ── Documentation Map ────────────────────────────────────────
    doc_html = ""
    if r.documentation_map:
        dm = r.documentation_map
        docs = dm.all_urls
        urls_html = "".join(
            f'<li><a href="{d.url}" target="_blank">{d.url}</a> <span class="tag {"tag-official" if d.is_official else "tag-community"}">{"official" if d.is_official else "community"}</span></li>'
            for d in docs
        )
        doc_html = f"""
    <div class="data-section collapsible">
        <div class="section-header" onclick="toggleSection(this)">
            <span>Documentation Map</span>
            <span class="section-meta">{dm.url_count} URLs ({dm.official_url_count} official)</span>
            <span class="collapse-arrow">▾</span>
        </div>
        <div class="section-body">
            <ul class="url-list">{urls_html}</ul>
        </div>
    </div>
    """

    # ── Extraction Details ───────────────────────────────────────
    ext_html = ""
    if ext:
        raw_fields = {
            "auth_methods": ext.auth_methods.value,
            "self_serve": ext.self_serve.value,
            "api_surface": ext.api_surface.value,
            "api_breadth": ext.api_breadth.value,
            "mcp": ext.mcp.value,
            "buildability": ext.buildability.value,
            "category": ext.category.value,
        }
        ext_rows = "".join(
            f'<div class="kv-row"><span class="kv-key">{k.replace("_", " ").title()}</span><span class="kv-val">{escape_html(str(v))}</span></div>'
            for k, v in raw_fields.items()
        )
        ext_html = f"""
    <div class="data-section collapsible">
        <div class="section-header" onclick="toggleSection(this)">
            <span>Extraction</span>
            <span class="section-meta">{len(raw_fields)} fields</span>
            <span class="collapse-arrow">▾</span>
        </div>
        <div class="section-body">
            <div class="kv-grid">{ext_rows}</div>
        </div>
    </div>
    """

    # ── Validation Summary ───────────────────────────────────────
    val_html = ""
    if r.validation:
        v = r.validation
        issue_rows = "".join(
            f'<tr><td>{issue.field}</td><td>{issue.check}</td><td><span class="badge badge-{"high" if issue.severity == "INFO" else "medium" if issue.severity == "WARNING" else "low"}">{issue.severity}</span></td><td>{escape_html(issue.message)}</td></tr>'
            for issue in v.issues
        )
        if not issue_rows:
            issue_rows = '<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">No issues — all checks passed</td></tr>'
        val_html = f"""
    <div class="data-section collapsible">
        <div class="section-header" onclick="toggleSection(this)">
            <span>Validation</span>
            <span class="section-meta">Score: {v.score:.0%} · Status: {v.status} · {v.fields_supported}/{v.fields_checked} supported</span>
            <span class="collapse-arrow">▾</span>
        </div>
        <div class="section-body">
            <table class="val-table">
                <thead><tr><th>Field</th><th>Check</th><th>Severity</th><th>Message</th></tr></thead>
                <tbody>{issue_rows}</tbody>
            </table>
        </div>
    </div>
    """

    # ── Browser Verification ─────────────────────────────────────
    bv_html = ""
    if r.browser_verification:
        bv = r.browser_verification
        bv_html = f"""
    <div class="data-section collapsible">
        <div class="section-header" onclick="toggleSection(this)">
            <span>Browser Verification</span>
            <span class="section-meta">{len(bv.verified_fields)} verified · {len(bv.corrections)} corrections</span>
            <span class="collapse-arrow">▾</span>
        </div>
        <div class="section-body">
            <div class="kv-row"><span class="kv-key">Verified Fields</span><span class="kv-val">{", ".join(bv.verified_fields) or "—"}</span></div>
            <div class="kv-row"><span class="kv-key">Corrections</span><span class="kv-val">{str(bv.corrections) or "—"}</span></div>
            <div class="kv-row"><span class="kv-key">Notes</span><span class="kv-val">{bv.notes or "—"}</span></div>
            {f'<div class="kv-row"><span class="kv-key">Screenshots</span><span class="kv-val">{", ".join(bv.screenshots) or "—"}</span></div>' if bv.screenshots else ""}
        </div>
    </div>
    """

    # ── All Evidence (collapsible) ────────────────────────────────
    evidence_html = ""
    if r.evidence:
        ev_items = "".join(
            f'<div class="evidence-card"><div class="evidence-url"><a href="{ev.url}" target="_blank">{ev.url}</a></div><div class="evidence-snippet">{escape_html(ev.content[:300])}{"..." if len(ev.content) > 300 else ""}</div></div>'
            for ev in r.evidence[:20]
        )
        if len(r.evidence) > 20:
            ev_items += f'<div class="evidence-more">… and {len(r.evidence) - 20} more evidence pieces</div>'
        evidence_html = f"""
    <div class="data-section collapsible">
        <div class="section-header" onclick="toggleSection(this)">
            <span>All Evidence</span>
            <span class="section-meta">{len(r.evidence)} pieces</span>
            <span class="collapse-arrow">▾</span>
        </div>
        <div class="section-body">{ev_items}</div>
    </div>
    """

    # ── Build initial state as JSON for the JS to use ────────────
    fields_data = []
    if ext:
        for field_name in [
            "auth_methods",
            "self_serve",
            "api_surface",
            "api_breadth",
            "mcp",
            "buildability",
        ]:
            fv = getattr(ext, field_name)
            pipeline_val = fv.value
            evidence_ids = fv.evidence_ids

            excerpts = []
            for eid in evidence_ids:
                for ev in r.evidence:
                    if ev.id == eid:
                        snippet = (
                            ev.content[:200].replace("\n", " ").strip()
                            if ev.content
                            else ""
                        )
                        excerpts.append({"url": ev.url, "snippet": snippet})
                        break

            fields_data.append(
                {
                    "name": field_name,
                    "display": field_name.replace("_", " ").title(),
                    "pipeline": pipeline_val,
                    "excerpts": excerpts[:3],
                    "override": r.human_review.overrides.get(field_name, None),
                }
            )

    overrides_json = json.dumps(r.human_review.overrides)
    fields_json = json.dumps(fields_data)
    reasons_json = json.dumps(r.human_review.reason)
    pipeline_conf = r.pipeline_confidence

    reasons_html = "".join(
        f'<li class="reason-item">{reason}</li>' for reason in r.human_review.reason
    )

    return _base_html(
        f"""
    <style>
        .review-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }}
        .review-header h1 {{ margin: 0; }}
        .data-section {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px; overflow: hidden; }}
        .section-header {{ display: flex; align-items: center; gap: 12px; padding: 16px 20px; cursor: pointer; user-select: none; transition: background 0.2s; }}
        .section-header:hover {{ background: rgba(255,255,255,0.03); }}
        .section-header .section-meta {{ margin-left: auto; font-size: 0.8rem; color: var(--text-muted); }}
        .collapse-arrow {{ font-size: 0.8rem; color: var(--text-muted); transition: transform 0.2s; }}
        .section-header.collapsed .collapse-arrow {{ transform: rotate(-90deg); }}
        .section-body {{ padding: 0 20px 20px; }}
        .section-header.collapsed + .section-body {{ display: none; }}
        .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }}
        .meta-item {{ padding: 8px 12px; background: rgba(255,255,255,0.03); border-radius: 8px; }}
        .meta-label {{ display: block; font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; }}
        .meta-val {{ font-size: 0.95rem; color: var(--text-h); font-weight: 500; }}
        .url-list {{ list-style: none; margin: 0; padding: 0; max-height: 300px; overflow-y: auto; }}
        .url-list li {{ padding: 6px 0; font-size: 0.85rem; border-bottom: 1px solid var(--border); }}
        .url-list li:last-child {{ border-bottom: none; }}
        .url-list a {{ word-break: break-all; }}
        .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-left: 8px; }}
        .tag-official {{ background: var(--status-high-bg); color: var(--status-high); }}
        .tag-community {{ background: var(--status-medium-bg); color: var(--status-medium); }}
        .kv-grid {{ }}
        .kv-row {{ display: flex; padding: 8px 0; border-bottom: 1px solid var(--border); }}
        .kv-row:last-child {{ border-bottom: none; }}
        .kv-key {{ flex: 0 0 180px; font-size: 0.85rem; color: var(--text-muted); }}
        .kv-val {{ flex: 1; font-size: 0.9rem; color: var(--text-h); font-family: var(--font-mono); }}
        .val-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
        .val-table th {{ text-align: left; padding: 8px 12px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); text-transform: uppercase; letter-spacing: 0.05em; font-size: 0.75rem; }}
        .val-table td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); color: var(--text-body); }}
        .val-table tr:hover td {{ background: rgba(255,255,255,0.02); }}
        .field-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
        .field-card h3 {{ margin: 0 0 12px 0; font-size: 1rem; color: var(--text-h); }}
        .field-value {{ font-size: 1.1rem; font-family: var(--font-mono); color: var(--text-h); margin-bottom: 8px; }}
        .evidence-card {{ padding: 10px 12px; background: rgba(255,255,255,0.02); border-radius: 8px; margin-bottom: 8px; }}
        .evidence-url {{ font-size: 0.8rem; margin-bottom: 4px; }}
        .evidence-url a {{ word-break: break-all; }}
        .evidence-snippet {{ font-size: 0.8rem; color: var(--text-muted); font-style: italic; line-height: 1.5; }}
        .evidence-more {{ padding: 12px; text-align: center; color: var(--text-muted); font-size: 0.85rem; }}
        .evidence-item {{ font-size: 0.85rem; color: var(--text-muted); margin: 4px 0; padding: 8px; background: rgba(255,255,255,0.03); border-radius: 6px; }}
        .evidence-item a {{ color: var(--accent-primary); }}
        .field-actions {{ display: flex; gap: 12px; margin-top: 12px; align-items: center; flex-wrap: wrap; }}
        .btn {{ padding: 8px 20px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer; font-size: 0.9rem; font-weight: 500; transition: all 0.2s; }}
        .btn-primary {{ background: var(--accent-primary); color: #fff; border-color: var(--accent-primary); }}
        .btn-primary:hover {{ opacity: 0.9; }}
        .btn-danger {{ background: var(--status-low); color: #fff; border-color: var(--status-low); }}
        .btn-danger:hover {{ opacity: 0.9; }}
        .btn-outline {{ background: transparent; color: var(--text-body); }}
        .btn-outline:hover {{ border-color: var(--text-h); color: var(--text-h); }}
        .btn-success {{ background: var(--status-high); color: #fff; border-color: var(--status-high); }}
        .btn-success:hover {{ opacity: 0.9; }}
        .override-form {{ margin-top: 12px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; display: none; }}
        .override-form input, .override-form textarea {{ width: 100%; padding: 8px 12px; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 6px; color: var(--text-h); margin-bottom: 8px; font-family: inherit; font-size: 0.9rem; }}
        .override-form textarea {{ min-height: 60px; resize: vertical; }}
        .override-form label {{ font-size: 0.85rem; color: var(--text-muted); display: block; margin-bottom: 4px; }}
        .status-badge {{ font-size: 0.85rem; }}
        .review-summary {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-top: 24px; }}
        .review-summary h3 {{ margin: 0 0 12px 0; font-size: 1rem; color: var(--text-h); }}
        .reason-list {{ list-style: none; margin: 8px 0; padding: 0; }}
        .reason-item {{ font-size: 0.85rem; color: var(--status-medium); padding: 4px 0; }}
        .reason-item::before {{ content: "⚠ "; }}
        .snackbar {{ position: fixed; bottom: 24px; right: 24px; padding: 12px 24px; border-radius: 8px; color: #fff; font-weight: 500; z-index: 1000; display: none; animation: fadeIn 0.3s; }}
        .snackbar.success {{ background: var(--status-high); }}
        .snackbar.error {{ background: var(--status-low); }}
        @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        .overridden {{ color: var(--status-high) !important; }}
        .overridden-old {{ text-decoration: line-through; color: var(--status-low); font-size: 0.9rem; }}
        .reviewer-section {{ display: flex; gap: 16px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }}
    </style>
    <div class="container">
        <nav class="top-nav">
            <a href="/review" class="nav-link">← Queue</a>
            <a href="/apps/{slug}/" class="nav-link">App Page</a>
            <span class="nav-title">{r.app.name} — Review</span>
        </nav>

        {meta_html}

        <div class="reviewer-section">
            <label style="font-size: 0.85rem; color: var(--text-muted);">Reviewer:</label>
            <input type="text" id="reviewer-name" value=""
                   placeholder="Your name" style="padding: 6px 12px; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 6px; color: var(--text-h); font-size: 0.9rem;">
        </div>

        <div id="reasons" style="margin-bottom: 20px;">
            <h3 style="font-size: 0.95rem; color: var(--text-h); margin-bottom: 8px;">Why this was flagged</h3>
            <ul class="reason-list">{reasons_html}</ul>
        </div>

        {doc_html}
        {ext_html}
        {val_html}
        {bv_html}
        {evidence_html}

        <h3 style="font-size: 1rem; color: var(--text-h); margin: 24px 0 12px;">Field Review</h3>
        <div id="fields-container"></div>

        <div class="review-summary" id="review-summary">
            <h3>Review Summary</h3>
            <p id="summary-text">No overrides yet.</p>
            <div class="field-actions" style="margin-top: 16px;">
                <button class="btn btn-success" id="btn-submit" onclick="submitReview()">Submit Review</button>
                <button class="btn btn-outline" onclick="skipReview()">Skip (keep pending)</button>
            </div>
        </div>
    </div>

    <div id="snackbar" class="snackbar"></div>

    <script>
    const FIELDS = {fields_json};
    const OVERRIDES = {overrides_json};
    const REASONS = {reasons_json};
    const PIPELINE_CONF = {pipeline_conf};

    let currentOverrides = {{}};
    Object.assign(currentOverrides, OVERRIDES);

    function getReviewer() {{
        const el = document.getElementById('reviewer-name');
        return el.value.trim() || 'anonymous';
    }}

    function toggleSection(header) {{
        header.classList.toggle('collapsed');
    }}

    function renderFields() {{
        const container = document.getElementById('fields-container');
        container.innerHTML = '';
        FIELDS.forEach(f => {{
            const override = currentOverrides[f.name];
            const pipelineHtml = override
                ? `<span class="overridden-old">${{escapeHtml(f.pipeline)}}</span> → <span class="overridden">${{escapeHtml(override.new)}}</span>`
                : `<span>${{escapeHtml(f.pipeline)}}</span>`;

            let evidenceHtml = '';
            f.excerpts.forEach(ex => {{
                evidenceHtml += `<div class="evidence-item">
                    <a href="${{ex.url}}" target="_blank">${{ex.url}}</a>
                    ${{ex.snippet ? '<div class="evidence-snippet">"' + escapeHtml(ex.snippet) + '"</div>' : ''}}
                </div>`;
            }});
            if (!evidenceHtml) {{
                evidenceHtml = '<div class="evidence-item" style="color: var(--text-muted);">No evidence citations for this field.</div>';
            }}

            const reasonHtml = override
                ? `<div style="margin-top: 8px; font-size: 0.85rem; color: var(--text-muted);">Reason: ${{escapeHtml(override.reason || '')}}</div>`
                : '';

            const card = document.createElement('div');
            card.className = 'field-card';
            card.id = `field-${{f.name}}`;
            card.innerHTML = `
                <h3>${{f.display}}</h3>
                <div class="field-value">${{pipelineHtml}}</div>
                <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 8px;">Confidence: ${{PIPELINE_CONF.toFixed(2)}} | Citations: ${{f.excerpts.length}}</div>
                ${{evidenceHtml}}
                ${{reasonHtml}}
                <div class="field-actions">
                    <button class="btn btn-primary" onclick="confirmField('${{f.name}}')">✓ Correct</button>
                    <button class="btn btn-danger" onclick="showOverrideForm('${{f.name}}')">✗ Override</button>
                </div>
                <div class="override-form" id="override-form-${{f.name}}">
                    <label>Correct value</label>
                    <input type="text" id="override-value-${{f.name}}" value="${{escapeHtml(f.pipeline)}}">
                    <label>Reason for override</label>
                    <textarea id="override-reason-${{f.name}}" placeholder="Why is the pipeline wrong?"></textarea>
                    <div style="display: flex; gap: 8px;">
                        <button class="btn btn-success" onclick="applyOverride('${{f.name}}')">Apply</button>
                        <button class="btn btn-outline" onclick="cancelOverride('${{f.name}}')">Cancel</button>
                    </div>
                </div>
            `;
            container.appendChild(card);
        }});
        updateSummary();
    }}

    function escapeHtml(str) {{
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }}

    function confirmField(name) {{
        delete currentOverrides[name];
        renderFields();
        showSnackbar('Confirmed correct', 'success');
    }}

    function showOverrideForm(name) {{
        document.querySelectorAll('.override-form').forEach(f => f.style.display = 'none');
        const form = document.getElementById('override-form-' + name);
        form.style.display = 'block';
    }}

    function cancelOverride(name) {{
        document.getElementById('override-form-' + name).style.display = 'none';
    }}

    function applyOverride(name) {{
        const newVal = document.getElementById('override-value-' + name).value.trim();
        const reason = document.getElementById('override-reason-' + name).value.trim();
        if (!newVal) {{
            showSnackbar('Value cannot be empty', 'error');
            return;
        }}
        currentOverrides[name] = {{
            old: FIELDS.find(f => f.name === name).pipeline,
            new: newVal,
            reason: reason
        }};
        renderFields();
        showSnackbar('Override recorded', 'success');
    }}

    function updateSummary() {{
        const keys = Object.keys(currentOverrides);
        const el = document.getElementById('summary-text');
        if (keys.length === 0) {{
            el.textContent = 'All fields confirmed correct. No overrides.';
        }} else {{
            el.innerHTML = keys.map(k => `<span style="color: var(--status-low);">${{k}}</span>: <span style="color: var(--status-high);">${{currentOverrides[k].new}}</span>`).join('<br>');
        }}
    }}

    function showSnackbar(msg, type) {{
        const el = document.getElementById('snackbar');
        el.textContent = msg;
        el.className = 'snackbar ' + type;
        el.style.display = 'block';
        setTimeout(() => {{ el.style.display = 'none'; }}, 3000);
    }}

    async function submitReview() {{
        const reviewer = getReviewer();
        if (reviewer === 'anonymous') {{
            showSnackbar('Please enter your name', 'error');
            return;
        }}

        const payload = {{
            reviewer: reviewer,
            overrides: currentOverrides,
            notes: ''
        }};

        try {{
            const resp = await fetch('/api/review/{slug}', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(payload)
            }});
            if (!resp.ok) {{
                const err = await resp.text();
                throw new Error(err);
            }}
            const result = await resp.json();
            showSnackbar('Review saved! Redirecting...', 'success');
            setTimeout(() => {{ window.location.href = '/review'; }}, 1000);
        }} catch (err) {{
            showSnackbar('Error: ' + err.message, 'error');
        }}
    }}

    async function skipReview() {{
        const reviewer = getReviewer();
        const payload = {{
            reviewer: reviewer,
            skip: true
        }};
        try {{
            const resp = await fetch('/api/review/{slug}', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(payload)
            }});
            if (!resp.ok) throw new Error('Failed to skip');
            showSnackbar('Skipped. Redirecting...', 'success');
            setTimeout(() => {{ window.location.href = '/review'; }}, 1000);
        }} catch (err) {{
            showSnackbar('Error: ' + err.message, 'error');
        }}
    }}

    renderFields();
    </script>
    """,
        title=f"Review — {r.app.name}",
    )


def _base_html(body: str, title: str = "Human QA") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Composio QA</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-base: #030305;
            --surface: rgba(18, 18, 24, 0.65);
            --text-h: #ffffff;
            --text-body: #a1a1aa;
            --text-muted: #71717a;
            --accent-primary: #3b82f6;
            --accent-secondary: #8b5cf6;
            --accent-tertiary: #10b981;
            --border: rgba(255, 255, 255, 0.06);
            --status-high: #10b981;
            --status-high-bg: rgba(16, 185, 129, 0.15);
            --status-medium: #f59e0b;
            --status-medium-bg: rgba(245, 158, 11, 0.15);
            --status-low: #ef4444;
            --status-low-bg: rgba(239, 68, 68, 0.15);
            --radius-sm: 8px;
            --radius-md: 16px;
            --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            --font-mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: var(--font-sans); background: var(--bg-base); color: var(--text-body); line-height: 1.6; -webkit-font-smoothing: antialiased; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 40px 20px; }}
        .top-nav {{ display: flex; align-items: center; gap: 16px; margin-bottom: 32px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
        .nav-link {{ color: var(--text-muted); text-decoration: none; font-weight: 500; font-size: 0.9rem; }}
        .nav-link:hover {{ color: var(--accent-primary); }}
        .nav-link.active {{ color: var(--accent-primary); }}
        .nav-title {{ color: var(--text-h); font-weight: 600; font-size: 1rem; margin-left: auto; }}
        .page-title {{ font-size: 1.8rem; font-weight: 700; color: var(--text-h); margin-bottom: 24px; letter-spacing: -0.02em; }}
        .badge {{ display: inline-flex; align-items: center; padding: 4px 12px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; }}
        .badge-high {{ background: var(--status-high-bg); color: var(--status-high); }}
        .badge-medium {{ background: var(--status-medium-bg); color: var(--status-medium); }}
        .badge-low {{ background: var(--status-low-bg); color: var(--status-low); }}
        a {{ color: var(--accent-primary); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    {body}
</body>
</html>"""


# ── HTTP handler ───────────────────────────────────────────────────


class ReviewHandler(SimpleHTTPRequestHandler):
    """HTTP handler serving static files + review UI."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUTPUT_DIR), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # Route dynamic review pages
        if path == "/review":
            html = _build_review_dashboard()
            self._send_html(html)
            return

        if path.startswith("/review/"):
            slug = path.split("/review/", 1)[1]
            html = _build_review_app(slug)
            self._send_html(html)
            return

        # Fall through to static file serving
        # Redirect / to case_study.html
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/case_study.html")
            self.end_headers()
            return

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/review/"):
            slug = path.split("/api/review/", 1)[1]
            self._handle_review_api(slug)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not found")

    def _handle_review_api(self, slug: str):
        """Handle POST /api/review/{slug} — save review results."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        # Locate the result file
        result_path = APPS_OUTPUT_DIR / slug / RESULT_FILENAME
        if not result_path.exists():
            self._send_json(404, {"error": f"App '{slug}' not found"})
            return

        try:
            data = orjson.loads(result_path.read_bytes())
            result = ResearchResult.model_validate(data)
        except Exception as exc:
            self._send_json(500, {"error": f"Failed to load: {exc}"})
            return

        reviewer = payload.get("reviewer", "anonymous")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if payload.get("skip"):
            # Mark as skipped — keep pending but record who skipped
            result.human_review.reviewer = reviewer
            result.human_review.notes = "Skipped during review session"
        else:
            overrides = payload.get("overrides", {})
            notes = payload.get("notes", "")

            result.human_verified = True
            result.human_review = HumanReview(
                required=True,
                reason=result.human_review.reason,
                status="completed",
                reviewer=reviewer,
                reviewed_at=now,
                overrides=overrides,
                notes=notes,
            )
            result.final_status = "HUMAN_MODIFIED" if overrides else "HUMAN_VERIFIED"

        # Save back
        result_path.write_bytes(
            orjson.dumps(
                result.model_dump(mode="json"),
                option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS,
            )
        )

        self._send_json(
            200,
            {
                "status": "ok",
                "slug": slug,
                "final_status": result.final_status,
                "overrides_count": len(result.human_review.overrides),
            },
        )

    def _send_html(self, html: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format, *args):
        """Quieter logging."""
        msg = format % args
        print(f"  [HTTP] {self.address_string()} - {msg}")


# ── Entry point ────────────────────────────────────────────────────


def main():
    server = HTTPServer((HOST, PORT), ReviewHandler)
    print(f"\n  [green]Human QA Server[/green]")
    print(f"  URL:  http://localhost:{PORT}/review")
    print(f"  Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
