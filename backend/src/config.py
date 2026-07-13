"""Configuration management — loads from .env and validates."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

# Ensure directories exist
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API Keys
    gemini_api_key: str = ""
    openai_api_key: str = ""
    firecrawl_api_key: str = ""
    tavily_api_key: str = ""
    composio_api_key: str = ""
    langsmith_api_key: str = ""

    # LLM Config (semantic extraction only)
    llm_model: str = "gpt-4o-mini"
    openai_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1

    # Pipeline Config
    concurrency: int = 5
    evidence_max_chars: int = 2500
    skip_browser_verification: bool = False

    # LangSmith
    langsmith_project: str = "composio-research-pipeline"

    def configure_environment(self) -> None:
        """Set environment variables for all services."""
        # Load .env into os.environ (pydantic-settings doesn't export to os.environ)
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")

        if self.gemini_api_key:
            os.environ["GOOGLE_API_KEY"] = self.gemini_api_key
        # Map OPEN_AI_API_KEY (env file name) → openai_api_key → OPENAI_API_KEY
        if not self.openai_api_key:
            self.openai_api_key = os.environ.get("OPEN_AI_API_KEY", "")
        if self.openai_api_key:
            os.environ["OPENAI_API_KEY"] = self.openai_api_key
        if self.langsmith_api_key:
            os.environ.pop("LANGCHAIN_TRACING_V2", None)
            os.environ["LANGCHAIN_API_KEY"] = self.langsmith_api_key
            os.environ["LANGCHAIN_PROJECT"] = self.langsmith_project


_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.configure_environment()
    return _settings
