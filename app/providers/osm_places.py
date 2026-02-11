from dataclasses import dataclass
import logging
import re

import httpx

from app.core.config import Settings
from app.providers.google_places import BusinessCandidate
from app.utils.normalizers import normalize_website_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OSMSearchContext:
    query: str
    limit: int


class OSMPlacesProvider:
    search_url = "https://nominatim.openstreetmap.org/search"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search_businesses(self, query: str, limit: int) -> list[BusinessCandidate]:
        if self.settings.demo_mode:
            return []

        context = OSMSearchContext(query=query, limit=limit)
        headers = {"User-Agent": self.settings.outbound_user_agent}
        unique: dict[str, BusinessCandidate] = {}
        search_queries = self._candidate_queries(context.query)

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            for search_query in search_queries:
                if len(unique) >= context.limit:
                    break

                rows = await self._run_query(client=client, query=search_query, limit=context.limit, headers=headers)
                for row in rows:
                    candidate = self._row_to_candidate(row)
                    if not candidate:
                        continue

                    dedupe_key = f"{candidate.name.lower()}::{(candidate.address or '')[:80].lower()}"
                    if dedupe_key not in unique:
                        unique[dedupe_key] = candidate

                    if len(unique) >= context.limit:
                        break

        return list(unique.values())[: context.limit]

    @staticmethod
    def _row_to_candidate(row: dict) -> BusinessCandidate | None:
        raw_name = (row.get("name") or "").strip()
        display_name = (row.get("display_name") or "").strip()
        name = raw_name or OSMPlacesProvider._name_from_display(display_name)
        if not name:
            return None

        extratags = row.get("extratags") or {}
        website = normalize_website_url(
            extratags.get("website")
            or extratags.get("contact:website")
            or extratags.get("url")
        )
        phone = (
            extratags.get("phone")
            or extratags.get("contact:phone")
            or extratags.get("mobile")
            or extratags.get("contact:mobile")
        )

        return BusinessCandidate(
            name=name,
            website=website,
            phone=phone,
            address=display_name or None,
        )

    @staticmethod
    def _name_from_display(display_name: str) -> str | None:
        if not display_name:
            return None
        parts = [part.strip() for part in display_name.split(",") if part.strip()]
        return parts[0] if parts else None

    async def _run_query(
        self,
        client: httpx.AsyncClient,
        query: str,
        limit: int,
        headers: dict[str, str],
    ) -> list[dict]:
        params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "extratags": 1,
            "limit": max(limit * 4, 20),
        }
        try:
            response = await client.get(self.search_url, params=params, headers=headers)
            response.raise_for_status()
            rows = response.json()
            if isinstance(rows, list):
                return rows
        except httpx.HTTPError as exc:
            logger.warning("OSM Nominatim search failed for '%s': %r", query, exc)
        return []

    @staticmethod
    def _candidate_queries(raw_query: str) -> list[str]:
        queries: list[str] = []

        def add(value: str) -> None:
            cleaned = " ".join(value.replace(",", " ").split())
            if cleaned and cleaned not in queries:
                queries.append(cleaned)

        add(raw_query)
        add(raw_query.replace(" in ", " "))

        if " in " in raw_query:
            niche, location = raw_query.split(" in ", 1)
            add(f"{niche} {location}")
            add(f"{niche} {location.split(',', 1)[0]}")

            niche_tokens = [token for token in re.split(r"[^A-Za-z0-9]+", niche) if len(token) >= 3]
            for token in niche_tokens:
                add(f"{token} {location}")
                add(f"{token} {location.split(',', 1)[0]}")

        return queries
