from dataclasses import dataclass
import logging
import re
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.providers.open_corporates import OfficerCandidate
from app.utils.normalizers import extract_domain

logger = logging.getLogger(__name__)

WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"

TOKEN_STOPWORDS = {
    "and",
    "co",
    "company",
    "group",
    "inc",
    "llc",
    "ltd",
    "of",
    "the",
}
ORG_NAME_BLOCKLIST = {
    "association",
    "company",
    "corporation",
    "foundation",
    "group",
    "inc",
    "institute",
    "llc",
    "ltd",
    "organization",
    "university",
}


@dataclass(slots=True)
class EntityMatch:
    qid: str
    score: float


class WikidataExecutiveProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search_officers(
        self,
        business_name: str,
        website: str | None = None,
        limit: int = 6,
    ) -> list[OfficerCandidate]:
        name = " ".join((business_name or "").strip().split())
        if not name:
            return []

        domain = extract_domain(website)
        qids = await self._search_entities(name=name, limit=6)
        if not qids:
            return []

        matches: list[EntityMatch] = []
        entities_payload: dict[str, dict] = {}

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            headers={"User-Agent": self.settings.outbound_user_agent},
        ) as client:
            for qid in qids:
                entity = await self._fetch_entity(client=client, qid=qid)
                if not entity:
                    continue

                score = self._entity_match_score(entity=entity, business_name=name, website_domain=domain)
                if score < 0.45:
                    continue

                matches.append(EntityMatch(qid=qid, score=score))
                entities_payload[qid] = entity

            if not matches:
                return []

            matches.sort(key=lambda item: item.score, reverse=True)
            top_matches = matches[:2]

            person_ids: set[str] = set()
            entity_people: dict[str, list[tuple[str, str]]] = {}
            for match in top_matches:
                entity = entities_payload.get(match.qid)
                if not entity:
                    continue

                people: list[tuple[str, str]] = []
                for pid in self._claim_entity_ids(entity, "P112"):
                    people.append((pid, "Founder"))
                    person_ids.add(pid)

                for pid in self._claim_entity_ids(entity, "P169"):
                    people.append((pid, "CEO"))
                    person_ids.add(pid)

                if people:
                    entity_people[match.qid] = people

            if not entity_people:
                return []

            person_labels = await self._fetch_entity_labels(client=client, qids=sorted(person_ids))

        candidates: list[OfficerCandidate] = []
        for match in top_matches:
            people = entity_people.get(match.qid, [])
            source_url = f"https://www.wikidata.org/wiki/{match.qid}"
            for pid, title in people:
                name_label = person_labels.get(pid)
                normalized_name = self._normalize_person_name(name_label)
                if not normalized_name:
                    continue
                candidates.append(
                    OfficerCandidate(
                        name=normalized_name,
                        title=title,
                        source_url=source_url,
                    )
                )

        deduped: dict[tuple[str, str], OfficerCandidate] = {}
        for candidate in candidates:
            key = (candidate.name.lower(), (candidate.title or "").lower())
            if key not in deduped:
                deduped[key] = candidate

        return list(deduped.values())[:limit]

    async def _search_entities(self, name: str, limit: int) -> list[str]:
        params = {
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "type": "item",
            "limit": str(max(1, min(limit, 10))),
            "format": "json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                headers={"User-Agent": self.settings.outbound_user_agent},
            ) as client:
                response = await client.get(WIKIDATA_API_URL, params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Wikidata search failed for '%s': %s", name, exc)
            return []

        qids: list[str] = []
        for row in body.get("search", []):
            qid = (row.get("id") or "").strip()
            if qid and qid.startswith("Q"):
                qids.append(qid)

        return qids

    async def _fetch_entity(self, client: httpx.AsyncClient, qid: str) -> dict | None:
        url = WIKIDATA_ENTITY_URL.format(qid=qid)
        try:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError:
            return None

        return (body.get("entities") or {}).get(qid)

    async def _fetch_entity_labels(self, client: httpx.AsyncClient, qids: list[str]) -> dict[str, str]:
        if not qids:
            return {}

        params = {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels",
            "languages": "en",
            "format": "json",
        }
        try:
            response = await client.get(WIKIDATA_API_URL, params=params)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError:
            return {}

        labels: dict[str, str] = {}
        entities = body.get("entities") or {}
        for qid, payload in entities.items():
            label = ((payload.get("labels") or {}).get("en") or {}).get("value")
            if isinstance(label, str) and label.strip():
                labels[qid] = label.strip()

        return labels

    def _entity_match_score(self, entity: dict, business_name: str, website_domain: str | None) -> float:
        score = 0.0

        business_tokens = self._tokens(business_name)
        if not business_tokens:
            return 0.0

        label = self._entity_label(entity)
        aliases = self._entity_aliases(entity)
        text_candidates = [label] + aliases

        overlap_best = 0.0
        for text in text_candidates:
            text_tokens = self._tokens(text)
            if not text_tokens:
                continue
            overlap = len(set(business_tokens).intersection(text_tokens)) / max(1, len(set(business_tokens)))
            overlap_best = max(overlap_best, overlap)

        score += overlap_best * 0.7

        if website_domain:
            site_domains = self._entity_website_domains(entity)
            normalized_target = self._normalize_host(website_domain)
            if normalized_target and normalized_target in site_domains:
                score += 0.35

        return min(score, 1.0)

    @staticmethod
    def _claim_entity_ids(entity: dict, prop: str) -> list[str]:
        claims = (entity.get("claims") or {}).get(prop) or []
        ids: list[str] = []
        for claim in claims:
            mainsnak = claim.get("mainsnak") or {}
            datavalue = mainsnak.get("datavalue") or {}
            value = datavalue.get("value") or {}
            qid = value.get("id")
            if isinstance(qid, str) and qid.startswith("Q"):
                ids.append(qid)
        return ids

    @staticmethod
    def _entity_label(entity: dict) -> str:
        return (((entity.get("labels") or {}).get("en") or {}).get("value") or "").strip()

    @staticmethod
    def _entity_aliases(entity: dict) -> list[str]:
        rows = ((entity.get("aliases") or {}).get("en") or [])
        aliases: list[str] = []
        for row in rows:
            value = (row.get("value") or "").strip()
            if value:
                aliases.append(value)
        return aliases

    def _entity_website_domains(self, entity: dict) -> set[str]:
        claims = (entity.get("claims") or {}).get("P856") or []
        domains: set[str] = set()
        for claim in claims:
            mainsnak = claim.get("mainsnak") or {}
            datavalue = mainsnak.get("datavalue") or {}
            value = datavalue.get("value")
            if not isinstance(value, str):
                continue

            parsed = urlparse(value)
            host = self._normalize_host(parsed.netloc)
            if host:
                domains.add(host)
        return domains

    @staticmethod
    def _tokens(value: str | None) -> set[str]:
        if not value:
            return set()

        tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", value.lower())
            if len(token) >= 3 and token not in TOKEN_STOPWORDS
        }
        return tokens

    @staticmethod
    def _normalize_person_name(raw_name: str | None) -> str | None:
        if not raw_name:
            return None

        cleaned = " ".join(str(raw_name).strip().split())
        cleaned = re.sub(r"(?i),?\s*(dds|dmd|md|ms|phd|jr|sr)\.?$", "", cleaned).strip()
        if not cleaned:
            return None

        tokens = [re.sub(r"[^A-Za-z'`-]", "", token) for token in cleaned.replace(".", " ").split()]
        tokens = [token for token in tokens if token]

        if len(tokens) < 2 or len(tokens) > 4:
            return None

        lowered = [token.lower() for token in tokens]
        if any(token in ORG_NAME_BLOCKLIST for token in lowered):
            return None

        return " ".join(token.capitalize() for token in tokens)

    @staticmethod
    def _normalize_host(host: str) -> str:
        normalized = host.strip().lower()
        if normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized
