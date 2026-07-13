"""Analytics engine — computes aggregates and generates charts.

Consumes only ResearchResult objects. No LLM calls.
"""

from __future__ import annotations

import json
from collections import Counter

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from models.result import ResearchResult


def _extract_flat_row(result: ResearchResult) -> dict:
    """Flatten a ResearchResult into a row for the DataFrame."""
    ext = result.extraction
    val = result.validation
    bv = result.browser_verification

    row = {
        "name": result.app.name,
        "website": result.app.website,
        "category_hint": result.app.category_hint or "",
        "confidence": result.confidence_score,
        "validation_status": val.status if val else "INCOMPLETE",
        "evidence_count": len(result.evidence),
    }

    if ext:
        row["description"] = str(ext.description.value)
        row["category"] = str(ext.category.value)
        row["auth_methods"] = str(ext.auth_methods.value)
        row["self_serve"] = str(ext.self_serve.value)
        row["api_surface"] = str(ext.api_surface.value)
        row["api_breadth"] = str(ext.api_breadth.value)
        row["mcp"] = str(ext.mcp.value)
        row["buildability"] = str(ext.buildability.value)
        row["blocker"] = str(ext.blocker.value)
    else:
        for col in ["description", "category", "auth_methods", "self_serve",
                     "api_surface", "api_breadth", "mcp", "buildability", "blocker"]:
            row[col] = "INCOMPLETE"

    # Apply browser corrections if available
    if bv and bv.corrections:
        for field, correction in bv.corrections.items():
            if field in row:
                row[field] = correction

    return row


def build_dataframe(results: list[ResearchResult]) -> pd.DataFrame:
    """Build a Pandas DataFrame from all results."""
    rows = [_extract_flat_row(r) for r in results]
    return pd.DataFrame(rows)


def _normalize_auth(auth_str: str) -> list[str]:
    """Parse auth methods string into a list of normalized methods."""
    auth_str = auth_str.lower()
    methods = []
    if any(k in auth_str for k in ["oauth2", "oauth 2", "oauth"]):
        methods.append("OAuth2")
    if any(k in auth_str for k in ["api key", "api_key", "apikey"]):
        methods.append("API Key")
    if "bearer" in auth_str:
        methods.append("Bearer Token")
    if "basic" in auth_str:
        methods.append("Basic Auth")
    if "jwt" in auth_str:
        methods.append("JWT")
    if any(k in auth_str for k in ["personal access token", "pat"]):
        methods.append("Personal Access Token")
    if not methods and auth_str not in ("unknown", "incomplete", ""):
        methods.append("Other")
    return methods if methods else ["Unknown"]


def generate_charts(df: pd.DataFrame) -> dict[str, str]:
    """Generate Plotly charts as JSON strings.

    Returns a dict of chart_name -> plotly JSON string.
    """
    charts = {}

    # 1. Auth Methods Distribution
    auth_counts = Counter()
    for auth_str in df["auth_methods"]:
        for method in _normalize_auth(auth_str):
            auth_counts[method] += 1

    if auth_counts:
        fig_auth = px.bar(
            x=list(auth_counts.keys()),
            y=list(auth_counts.values()),
            labels={"x": "Auth Method", "y": "Count"},
            title="Authentication Methods Distribution",
            color=list(auth_counts.keys()),
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig_auth.update_layout(showlegend=False, template="plotly_dark")
        charts["auth_distribution"] = fig_auth.to_json()

    # 2. Self-Serve vs Gated
    def classify_access(val):
        val = str(val).lower()
        if any(k in val for k in ["self-serve", "self serve", "free", "trial", "freemium"]):
            return "Self-Serve"
        if any(k in val for k in ["gated", "contact sales", "enterprise", "partner"]):
            return "Gated"
        return "Unknown"

    df["access_model"] = df["self_serve"].apply(classify_access)
    access_counts = df["access_model"].value_counts()

    fig_access = px.pie(
        names=access_counts.index,
        values=access_counts.values,
        title="Access Model: Self-Serve vs Gated",
        color_discrete_sequence=["#00d4aa", "#ff6b6b", "#ffd93d"],
    )
    fig_access.update_layout(template="plotly_dark")
    charts["access_model"] = fig_access.to_json()

    # 3. Buildability Distribution
    build_counts = df["buildability"].value_counts()
    fig_build = px.bar(
        x=build_counts.index,
        y=build_counts.values,
        labels={"x": "Buildability", "y": "Count"},
        title="Toolkit Buildability Assessment",
        color=build_counts.index,
        color_discrete_map={"HIGH": "#00d4aa", "MEDIUM": "#ffd93d", "LOW": "#ff6b6b", "BLOCKED": "#c0392b"},
    )
    fig_build.update_layout(showlegend=False, template="plotly_dark")
    charts["buildability"] = fig_build.to_json()

    # 4. API Breadth Distribution
    breadth_counts = df["api_breadth"].value_counts()
    fig_breadth = px.bar(
        x=breadth_counts.index,
        y=breadth_counts.values,
        labels={"x": "API Breadth", "y": "Count"},
        title="API Surface Breadth",
        color=breadth_counts.index,
        color_discrete_sequence=px.colors.qualitative.Pastel,
    )
    fig_breadth.update_layout(showlegend=False, template="plotly_dark")
    charts["api_breadth"] = fig_breadth.to_json()

    # 5. MCP Readiness
    mcp_counts = df["mcp"].apply(lambda x: x if x in ("Official MCP", "Community MCP", "No known MCP") else "Unknown").value_counts()
    fig_mcp = px.pie(
        names=mcp_counts.index,
        values=mcp_counts.values,
        title="MCP Server Readiness",
        color_discrete_sequence=["#00d4aa", "#3498db", "#e74c3c", "#95a5a6"],
    )
    fig_mcp.update_layout(template="plotly_dark")
    charts["mcp_readiness"] = fig_mcp.to_json()

    # 6. Confidence Score Distribution
    fig_conf = px.histogram(
        df, x="confidence",
        nbins=20,
        title="Confidence Score Distribution",
        labels={"confidence": "Confidence Score", "count": "Apps"},
        color_discrete_sequence=["#3498db"],
    )
    fig_conf.update_layout(template="plotly_dark")
    charts["confidence_distribution"] = fig_conf.to_json()

    # 7. Category Breakdown
    cat_counts = df["category"].value_counts().head(15)
    fig_cat = px.bar(
        x=cat_counts.values,
        y=cat_counts.index,
        orientation="h",
        labels={"x": "Count", "y": "Category"},
        title="Top 15 App Categories",
        color=cat_counts.index,
        color_discrete_sequence=px.colors.qualitative.Set3,
    )
    fig_cat.update_layout(showlegend=False, template="plotly_dark", height=500)
    charts["categories"] = fig_cat.to_json()

    return charts


def compute_summary_stats(df: pd.DataFrame) -> dict:
    """Compute summary statistics for the report."""
    total = len(df)
    complete = len(df[df["validation_status"] != "INCOMPLETE"])
    high_conf = len(df[df["confidence"] >= 0.75])
    avg_evidence = df["evidence_count"].mean()

    # Auth
    auth_counts = Counter()
    for auth_str in df["auth_methods"]:
        for method in _normalize_auth(auth_str):
            auth_counts[method] += 1

    # Access model
    def classify_access(val):
        val = str(val).lower()
        if any(k in val for k in ["self-serve", "self serve", "free", "trial"]):
            return "Self-Serve"
        if any(k in val for k in ["gated", "contact sales", "enterprise"]):
            return "Gated"
        return "Unknown"

    access_counts = Counter(df["self_serve"].apply(classify_access))

    return {
        "total_apps": total,
        "complete": complete,
        "completion_rate": f"{complete/max(total,1)*100:.0f}%",
        "high_confidence": high_conf,
        "avg_evidence_per_app": f"{avg_evidence:.1f}",
        "top_auth": auth_counts.most_common(3),
        "self_serve_count": access_counts.get("Self-Serve", 0),
        "gated_count": access_counts.get("Gated", 0),
        "high_buildability": len(df[df["buildability"] == "HIGH"]),
        "blocked_count": len(df[df["buildability"] == "BLOCKED"]),
        "mcp_official": len(df[df["mcp"].str.contains("Official", case=False, na=False)]),
        "mcp_community": len(df[df["mcp"].str.contains("Community", case=False, na=False)]),
    }
