# Composio AI Research Pipeline

AI-powered research pipeline that analyzes 100 SaaS applications for agent toolkit buildability.

## Quick Start

```bash
cd backend

# Install dependencies
pip install uv
uv sync

# Install Playwright browsers
uv run playwright install chromium

# Set up environment
cp .env.example .env  # Add your API keys

# Run the full pipeline
uv run python -m src.cli run

# Run for a single app (testing)
uv run python -m src.cli run-one "GitHub"

# Generate report after pipeline completes
uv run python -m src.cli report

# Verify a sample
uv run python -m src.cli verify --sample 15
```

## Environment Variables

```
GEMINI_API_KEY=your-key
FIRECRAWL_API_KEY=your-key
COMPOSEIO_API_KEY=your-key
LANGSMITH_API_KEY=your-key  # Optional
```

## Architecture

```
Pipeline Orchestrator
    ├── Marketplace Lookup (Composio API)
    ├── Evidence Collection (Firecrawl + httpx fallback)
    ├── MCP Discovery (GitHub Search)
    ├── LLM Extraction (LangChain + Gemini 2.5 Flash)
    ├── LLM Validation (LangChain + Gemini)
    ├── Browser Verification (Playwright, failures only)
    └── HTML Report (Jinja2 + Plotly)
```

## Project Structure

```
backend/
├── data/apps.csv              # 100 apps to research
├── models/                    # Pydantic data models
├── prompts/                   # LLM prompt templates
├── src/
│   ├── cli.py                 # Typer CLI
│   ├── config.py              # Settings management
│   ├── orchestrator.py        # Pipeline controller
│   ├── marketplace.py         # Composio marketplace lookup
│   ├── evidence.py            # Documentation fetching
│   ├── browser_verify.py      # Playwright verification
│   ├── analysis.py            # Pandas analytics + Plotly charts
│   ├── report.py              # HTML report generator
│   └── chains/                # LangChain chains
│       ├── extraction_chain.py
│       ├── validation_chain.py
│       └── insight_chain.py
├── templates/report.html      # Jinja2 HTML template
└── output/                    # Generated results
```

## Tech Stack

- **Python 3.12+** with **uv** package manager
- **LangChain + Gemini 2.5 Flash** for structured extraction
- **Firecrawl** for documentation scraping
- **Playwright** for browser verification
- **Pandas + Plotly** for analytics
- **Jinja2** for HTML report generation
