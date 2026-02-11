from dataclasses import dataclass
import logging
import re

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)
GENERIC_LOCAL_PARTS = {
    "admin",
    "billing",
    "careers",
    "contact",
    "hello",
    "hr",
    "info",
    "jobs",
    "office",
    "sales",
    "support",
    "team",
}


@dataclass(slots=True)
class EmailCandidate:
    email: str
    score: float
    status: str | None


@dataclass(slots=True)
class EmailVerification:
    status: str | None
    score: float | None


@dataclass(slots=True)
class DomainPerson:
    full_name: str
    title: str | None
    email: str
    confidence: float | None


class HunterProvider:
    finder_url = "https://api.hunter.io/v2/email-finder"
    verifier_url = "https://api.hunter.io/v2/email-verifier"
    domain_search_url = "https://api.hunter.io/v2/domain-search"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def find_email(self, first_name: str, last_name: str, domain: str) -> EmailCandidate | None:
        if self.settings.demo_mode:
            demo_email = f"{first_name.lower()}.{last_name.lower()}@{domain}"
            return EmailCandidate(email=demo_email, score=78.0, status="accept_all")

        if not self.settings.hunter_api_key:
            logger.warning("Hunter API key missing. Skipping email lookup.")
            return None

        params = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "api_key": self.settings.hunter_api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.get(self.finder_url, params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Hunter email-finder failed: %s", self._http_error_details(exc))
            return None

        data = body.get("data") or {}
        email = data.get("email")
        if not email:
            return None

        verification_data = data.get("verification")
        verification_status: str | None = None
        if isinstance(verification_data, dict):
            verification_status = verification_data.get("status")
        elif isinstance(verification_data, str):
            verification_status = verification_data

        return EmailCandidate(
            email=email,
            score=float(data.get("score") or 0.0),
            status=verification_status,
        )

    async def search_domain_people(
        self,
        domain: str,
        company: str | None = None,
        limit: int = 15,
    ) -> list[DomainPerson]:
        if self.settings.demo_mode:
            return self._demo_domain_people(domain=domain, limit=limit)

        if not self.settings.hunter_api_key:
            logger.warning("Hunter API key missing. Skipping domain search.")
            return []

        # Hunter free plans reject domain-search limits above 10.
        per_request_limit = max(1, min(limit, 10))
        params: dict[str, str | int] = {
            "domain": domain,
            "type": "personal",
            "limit": per_request_limit,
            "api_key": self.settings.hunter_api_key,
        }
        if company:
            params["company"] = company

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.get(self.domain_search_url, params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Hunter domain-search failed: %s", self._http_error_details(exc))
            return []

        rows = (body.get("data") or {}).get("emails") or []
        people: list[DomainPerson] = []

        for row in rows:
            email = (row.get("value") or "").strip().lower()
            if not email:
                continue

            full_name = self._full_name_from_row(row=row, fallback_email=email)
            if "@" in full_name:
                local_part = email.split("@", 1)[0]
                if local_part in GENERIC_LOCAL_PARTS:
                    continue

            confidence = row.get("confidence")
            normalized_confidence: float | None = None
            if confidence is not None:
                try:
                    normalized_confidence = max(0.0, min(float(confidence) / 100.0, 1.0))
                except (TypeError, ValueError):
                    normalized_confidence = None

            people.append(
                DomainPerson(
                    full_name=full_name,
                    title=row.get("position"),
                    email=email,
                    confidence=normalized_confidence,
                )
            )

        return people[:limit]

    async def verify_email(self, email: str) -> EmailVerification | None:
        if self.settings.demo_mode:
            return EmailVerification(status="valid", score=90.0)

        if not self.settings.hunter_api_key:
            return None

        params = {"email": email, "api_key": self.settings.hunter_api_key}

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.get(self.verifier_url, params=params)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Hunter email-verifier failed: %s", self._http_error_details(exc))
            return None

        data = body.get("data") or {}
        score = data.get("score")
        return EmailVerification(
            status=data.get("status"),
            score=float(score) if score is not None else None,
        )

    @staticmethod
    def _full_name_from_row(row: dict, fallback_email: str) -> str:
        first = (row.get("first_name") or "").strip()
        last = (row.get("last_name") or "").strip()
        full = " ".join(part for part in [first, last] if part).strip()
        if full:
            return full

        raw_full = (row.get("full_name") or "").strip()
        if raw_full:
            return raw_full

        local_part = fallback_email.split("@", 1)[0]
        tokens = [token for token in re.split(r"[._-]+", local_part) if token and token.isalpha()]
        if len(tokens) >= 2:
            return f"{tokens[0].title()} {tokens[-1].title()}"

        return fallback_email

    @staticmethod
    def _demo_domain_people(domain: str, limit: int) -> list[DomainPerson]:
        sample = [
            DomainPerson(
                full_name="Emma Carter",
                title="Owner",
                email=f"emma.carter@{domain}",
                confidence=0.86,
            ),
            DomainPerson(
                full_name="Daniel Morgan",
                title="Managing Director",
                email=f"daniel.morgan@{domain}",
                confidence=0.74,
            ),
        ]
        return sample[: max(1, min(limit, len(sample)))]

    @staticmethod
    def _http_error_details(exc: httpx.HTTPError) -> str:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            response = exc.response
            status = response.status_code
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                error_id = errors[0].get("id")
                details = errors[0].get("details")
                if error_id and details:
                    return f"status={status} id={error_id} details={details}"
                if error_id:
                    return f"status={status} id={error_id}"
            return f"status={status}"

        return exc.__class__.__name__
