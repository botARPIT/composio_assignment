"""Composio marketplace enrichment stage.

Queries Composio's own marketplace API to cross-reference each researched app
against what Composio already knows — auth schemes, tool count, trigger count,
and whether the app is officially integrated.

This stage adds ground-truth Composio data alongside the web-scraped evidence,
enabling direct comparison between pipeline-extracted values and Composio's
verified integration catalog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ComposioAppRecord:
    """Data from the Composio marketplace for a single app."""

    in_marketplace: bool = False
    composio_slug: Optional[str] = None
    composio_name: Optional[str] = None
    auth_schemes: list[str] = field(default_factory=list)
    composio_managed_auth: list[str] = field(default_factory=list)
    no_auth: bool = False
    tools_count: int = 0
    triggers_count: int = 0
    categories: list[str] = field(default_factory=list)
    app_url: Optional[str] = None
    logo_url: Optional[str] = None


class ComposioEnricher:
    """Queries the Composio marketplace API and returns enrichment data per app.

    Fetches the full toolkit catalog once (cached in memory for the session)
    and does O(1) slug-based lookup per app.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._catalog: dict[str, ComposioAppRecord] | None = None  # slug → record

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_catalog(self) -> dict[str, ComposioAppRecord]:
        """Fetch all toolkits once and cache them keyed by slug."""
        if self._catalog is not None:
            return self._catalog

        try:
            from composio import Composio  # type: ignore[import]

            client = Composio(api_key=self._api_key)
            toolkits = client.toolkits.get()

            catalog: dict[str, ComposioAppRecord] = {}
            for tk in toolkits:
                try:
                    categories = [
                        c.name for c in (tk.meta.categories or []) if hasattr(c, "name")
                    ]
                    record = ComposioAppRecord(
                        in_marketplace=True,
                        composio_slug=tk.slug,
                        composio_name=tk.name,
                        auth_schemes=list(tk.auth_schemes or []),
                        composio_managed_auth=list(tk.composio_managed_auth_schemes or []),
                        no_auth=bool(tk.no_auth),
                        tools_count=int(tk.meta.tools_count or 0),
                        triggers_count=int(tk.meta.triggers_count or 0),
                        categories=categories,
                        app_url=tk.meta.app_url,
                        logo_url=tk.meta.logo,
                    )
                    catalog[tk.slug.lower()] = record
                except Exception as e:  # noqa: BLE001
                    logger.debug("Skipping toolkit %s: %s", getattr(tk, "slug", "?"), e)

            self._catalog = catalog
            logger.info("Loaded %d apps from Composio marketplace", len(catalog))
            return catalog

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load Composio catalog: %s", exc)
            self._catalog = {}
            return {}

    @staticmethod
    def _candidate_slugs(app_name: str) -> list[str]:
        """Generate slug candidates from an app name for fuzzy matching."""
        name = app_name.lower().strip()
        candidates = [
            name,
            name.replace(" ", ""),
            name.replace(" ", "-"),
            name.replace(" ", "_"),
            name.split(" ")[0],  # first word only (e.g. "salesforce" from "Salesforce CRM")
        ]
        return list(dict.fromkeys(candidates))  # deduplicated, order-preserving

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enrich(self, app_name: str) -> ComposioAppRecord:
        """Return Composio marketplace data for the given app name.

        Returns a ComposioAppRecord with in_marketplace=False if the app
        is not found in the Composio catalog.
        """
        catalog = self._load_catalog()
        if not catalog:
            return ComposioAppRecord(in_marketplace=False)

        for slug in self._candidate_slugs(app_name):
            if slug in catalog:
                logger.debug("Composio match: %s → %s", app_name, slug)
                return catalog[slug]

        logger.debug("No Composio match for: %s", app_name)
        return ComposioAppRecord(in_marketplace=False)

    def enrich_batch(self, app_names: list[str]) -> dict[str, ComposioAppRecord]:
        """Enrich a list of apps in one catalog fetch."""
        return {name: self.enrich(name) for name in app_names}
