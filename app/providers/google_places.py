from dataclasses import dataclass
import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BusinessCandidate:
    name: str
    website: str | None
    phone: str | None
    address: str | None


class GooglePlacesProvider:
    search_url = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search_businesses(self, query: str, limit: int) -> list[BusinessCandidate]:
        if self.settings.demo_mode:
            return self._demo_results(limit)

        if not self.settings.google_places_api_key:
            logger.warning("Google Places API key missing. Returning empty list.")
            return []

        payload = {"textQuery": query, "pageSize": limit}
        headers = {
            "X-Goog-Api-Key": self.settings.google_places_api_key,
            "X-Goog-FieldMask": ",".join(
                [
                    "places.displayName",
                    "places.websiteUri",
                    "places.nationalPhoneNumber",
                    "places.formattedAddress",
                ]
            ),
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(self.search_url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Google Places request failed: %s", exc)
            return []

        businesses: list[BusinessCandidate] = []
        for place in body.get("places", []):
            name = (place.get("displayName") or {}).get("text")
            if not name:
                continue

            businesses.append(
                BusinessCandidate(
                    name=name.strip(),
                    website=place.get("websiteUri"),
                    phone=place.get("nationalPhoneNumber"),
                    address=place.get("formattedAddress"),
                )
            )

        return businesses[:limit]

    @staticmethod
    def _demo_results(limit: int) -> list[BusinessCandidate]:
        sample = [
            BusinessCandidate(
                name="Northside Dental Group",
                website="https://www.northsidedental.example",
                phone="+1 312-555-0142",
                address="101 N Main St, Chicago, IL",
            ),
            BusinessCandidate(
                name="Lakeview Family Dental",
                website="https://www.lakeviewfamilydental.example",
                phone="+1 312-555-0167",
                address="200 W Belmont Ave, Chicago, IL",
            ),
            BusinessCandidate(
                name="BrightSmile Dental Studio",
                website="https://www.brightsmiledental.example",
                phone="+1 312-555-0199",
                address="45 S State St, Chicago, IL",
            ),
        ]
        return sample[: max(1, min(limit, len(sample)))]
