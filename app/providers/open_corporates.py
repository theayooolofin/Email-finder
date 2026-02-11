from dataclasses import dataclass
import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OfficerCandidate:
    name: str
    title: str | None
    source_url: str | None


class OpenCorporatesProvider:
    search_url = "https://api.opencorporates.com/v0.4/officers/search"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search_officers(self, business_name: str, limit: int = 6) -> list[OfficerCandidate]:
        if self.settings.demo_mode:
            return self._demo_results(business_name, limit)

        if not self.settings.opencorporates_api_token:
            logger.debug("OpenCorporates API token missing. Skipping officer lookup.")
            return []

        params: dict[str, str | int] = {"q": business_name, "per_page": limit}
        params["api_token"] = self.settings.opencorporates_api_token

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.get(self.search_url, params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("OpenCorporates request failed: %s", exc)
            return []

        candidates: list[OfficerCandidate] = []
        rows = (body.get("results") or {}).get("officers", [])

        for row in rows:
            officer = row.get("officer") or {}
            if officer.get("inactive"):
                continue

            name = officer.get("name")
            if not name:
                continue

            candidates.append(
                OfficerCandidate(
                    name=name,
                    title=officer.get("position"),
                    source_url=officer.get("opencorporates_url"),
                )
            )

        return candidates[:limit]

    @staticmethod
    def _demo_results(business_name: str, limit: int) -> list[OfficerCandidate]:
        sample = [
            OfficerCandidate(name="Emma Carter", title="Founder", source_url=None),
            OfficerCandidate(name="Daniel Morgan", title="Managing Director", source_url=None),
            OfficerCandidate(name="Sophia Bennett", title="Owner", source_url=None),
        ]
        if business_name:
            sample[0] = OfficerCandidate(name="Emma Carter", title="Owner", source_url=None)
        return sample[: max(1, min(limit, len(sample)))]
