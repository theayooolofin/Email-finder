import logging
import re

from app.core.config import Settings
from app.providers.google_places import BusinessCandidate, GooglePlacesProvider
from app.providers.hunter import DomainPerson, HunterProvider
from app.providers.open_corporates import OfficerCandidate, OpenCorporatesProvider
from app.providers.osm_places import OSMPlacesProvider
from app.providers.twilio_lookup import TwilioLookupProvider
from app.providers.web_exec_search import WebExecutiveCandidate, WebExecutiveSearchProvider
from app.providers.website_enricher import WebsiteEnricher, WebsiteEnrichment
from app.providers.wikidata_exec import WikidataExecutiveProvider
from app.schemas import LeadCreate, LeadDiscoveryRequest
from app.utils.normalizers import extract_domain, normalize_website_url, split_first_last

logger = logging.getLogger(__name__)

TITLE_PRIORITY = [
    "founder",
    "co-founder",
    "chief executive officer",
    "chief executive",
    "ceo",
    "owner",
    "co-owner",
    "president",
    "managing member",
    "managing director",
    "director",
    "partner",
    "principal",
]
STRICT_EXECUTIVE_KEYWORDS = (
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "chief executive",
    "chief executive officer",
)
SECONDARY_LEADERSHIP_KEYWORDS = (
    "owner",
    "co-owner",
    "principal",
    "president",
    "managing member",
    "managing director",
    "director",
    "partner",
)


class LeadPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.google_places = GooglePlacesProvider(settings)
        self.osm_places = OSMPlacesProvider(settings)
        self.open_corporates = OpenCorporatesProvider(settings)
        self.hunter = HunterProvider(settings)
        self.twilio = TwilioLookupProvider(settings)
        self.web_exec_search = WebExecutiveSearchProvider(settings)
        self.wikidata_exec = WikidataExecutiveProvider(settings)
        self.website_enricher = WebsiteEnricher(settings)

    async def discover(self, request: LeadDiscoveryRequest) -> list[LeadCreate]:
        limit = min(request.max_businesses, self.settings.max_businesses_per_request)

        if request.website:
            website = normalize_website_url(request.website)
            if not website:
                return []

            business_name = (request.business_name or extract_domain(website) or website).strip()
            manual_business = BusinessCandidate(
                name=business_name,
                website=website,
                phone=None,
                address=None,
            )
            manual_query = f"website:{website}"
            lead = await self._enrich_business(
                business=manual_business,
                city=request.city,
                search_query=manual_query,
                discovery_source="manual_website",
                strict_executive=True,
            )
            return [lead]

        query = f"{request.niche} in {request.city}, {request.country}"

        discovery_source = "google_places"
        businesses = await self.google_places.search_businesses(query=query, limit=limit)

        # Free fallback when Google Places key is unavailable or quota-limited.
        if not businesses and not self.settings.demo_mode:
            discovery_source = "osm_nominatim"
            businesses = await self.osm_places.search_businesses(query=query, limit=limit)

        leads: list[LeadCreate] = []
        for business in businesses:
            lead = await self._enrich_business(
                business=business,
                city=request.city,
                search_query=query,
                discovery_source=discovery_source,
                strict_executive=False,
            )
            leads.append(lead)

        return leads

    async def _enrich_business(
        self,
        business: BusinessCandidate,
        city: str | None,
        search_query: str,
        discovery_source: str,
        strict_executive: bool = False,
    ) -> LeadCreate:
        domain = extract_domain(business.website)
        sources = [discovery_source]
        if strict_executive:
            sources.append("strict_exec_mode")

        resolved_business_name = (business.name or "").strip() or (domain or "Unknown Business")
        if self._looks_like_domain_name(resolved_business_name):
            inferred_label = self._domain_label(resolved_business_name)
            if inferred_label:
                resolved_business_name = inferred_label

        owner_name: str | None = None
        owner_title: str | None = None

        email: str | None = None
        email_status = "unknown"
        email_confidence = 0.0
        web_exec_phone: str | None = None
        web_exec_phone_status = "unknown"
        used_secondary_leadership_fallback = False
        registry_officers: list[OfficerCandidate] = []

        website_enrichment: WebsiteEnrichment | None = None
        if business.website:
            website_enrichment = await self.website_enricher.enrich(
                business.website,
                prefer_executive=strict_executive,
            )
            if website_enrichment.has_any_data() or website_enrichment.business_name:
                sources.append("website_extract")

            if self._is_better_business_name(
                current=resolved_business_name,
                candidate=website_enrichment.business_name,
                website_domain=domain,
            ):
                resolved_business_name = (website_enrichment.business_name or resolved_business_name).strip()

        # Premium owner source when Hunter key is available.
        if domain and self.settings.hunter_api_key:
            sources.append("hunter")
            domain_people = await self.hunter.search_domain_people(
                domain=domain,
                company=resolved_business_name,
                limit=self.settings.hunter_domain_search_limit,
            )
            best_person = self._pick_best_domain_person(
                people=domain_people,
                require_executive=strict_executive,
            )

            if best_person:
                owner_name = best_person.full_name
                owner_title = best_person.title
                email = best_person.email
                email_status = "found"
                if best_person.confidence is not None:
                    email_confidence = best_person.confidence

                verification = await self.hunter.verify_email(best_person.email)
                if verification:
                    email_status = verification.status or email_status
                    if verification.score is not None:
                        email_confidence = max(0.0, min(verification.score / 100.0, 1.0))

        # Free website extraction fallback.
        if website_enrichment:
            if not owner_name and website_enrichment.owner_name:
                if not strict_executive or self._is_strict_executive_title(website_enrichment.owner_title):
                    owner_name = website_enrichment.owner_name
                    owner_title = website_enrichment.owner_title

            if not email and website_enrichment.email:
                if not strict_executive:
                    email = website_enrichment.email
                    email_status = website_enrichment.email_status
                    email_confidence = max(0.0, min(website_enrichment.email_confidence, 1.0))
                elif owner_name and self._email_matches_owner(website_enrichment.email, owner_name):
                    email = website_enrichment.email
                    email_status = website_enrichment.email_status
                    email_confidence = max(0.0, min(website_enrichment.email_confidence, 1.0))

        if strict_executive and not owner_name:
            wikidata_officers = await self.wikidata_exec.search_officers(
                business_name=resolved_business_name,
                website=business.website,
                limit=6,
            )
            if wikidata_officers:
                sources.append("wikidata_exec")
                owner = self._pick_best_owner(officers=wikidata_officers, require_executive=True)
                owner_name = owner.name if owner else None
                owner_title = owner.title if owner else None

        if strict_executive and business.website and not owner_name:
            web_exec_candidates = await self.web_exec_search.search_executives(
                business_name=resolved_business_name,
                website=business.website,
                limit=6,
            )
            best_web_exec = self._pick_best_web_exec_candidate(web_exec_candidates)
            if best_web_exec:
                sources.append("web_exec_search")
                owner_name = best_web_exec.name
                owner_title = best_web_exec.title

                if not email and best_web_exec.email and self._email_matches_owner(best_web_exec.email, owner_name):
                    email = best_web_exec.email
                    email_status = "web_exec_search"
                    email_confidence = max(email_confidence, best_web_exec.confidence)

                if best_web_exec.phone:
                    web_exec_phone = best_web_exec.phone
                    web_exec_phone_status = "valid"

        # Corporate registry fallback if still no owner.
        if not owner_name:
            registry_officers = await self.open_corporates.search_officers(resolved_business_name, limit=8)
            if registry_officers:
                sources.append("open_corporates")
            owner = self._pick_best_owner(officers=registry_officers, require_executive=strict_executive)
            owner_name = owner.name if owner else None
            owner_title = owner.title if owner else None

        # Secondary leadership fallback for strict mode when no executive is found.
        if strict_executive and not owner_name:
            secondary_website_enrichment: WebsiteEnrichment | None = None
            if business.website:
                secondary_website_enrichment = await self.website_enricher.enrich(
                    business.website,
                    prefer_executive=False,
                )
                if secondary_website_enrichment.has_any_data() or secondary_website_enrichment.business_name:
                    sources.append("website_extract_secondary")

            if (
                secondary_website_enrichment
                and secondary_website_enrichment.owner_name
                and self._is_secondary_leadership_title(secondary_website_enrichment.owner_title)
            ):
                owner_name = secondary_website_enrichment.owner_name
                owner_title = secondary_website_enrichment.owner_title
                used_secondary_leadership_fallback = True
                sources.append("secondary_leadership_fallback")

                if (
                    not email
                    and secondary_website_enrichment.email
                    and self._email_matches_owner(secondary_website_enrichment.email, owner_name)
                ):
                    email = secondary_website_enrichment.email
                    email_status = secondary_website_enrichment.email_status
                    email_confidence = max(0.0, min(secondary_website_enrichment.email_confidence, 1.0))

            if not owner_name:
                secondary_owner = self._pick_best_secondary_owner(registry_officers)
                if secondary_owner:
                    owner_name = secondary_owner.name
                    owner_title = secondary_owner.title
                    used_secondary_leadership_fallback = True
                    sources.append("secondary_leadership_fallback")

        # Pattern lookup when owner is known but email still missing.
        if owner_name and domain and not email and self.settings.hunter_api_key:
            first_name, last_name = split_first_last(owner_name)
            if first_name and last_name:
                found = await self.hunter.find_email(first_name=first_name, last_name=last_name, domain=domain)
                if found and (not strict_executive or self._email_matches_owner(found.email, owner_name)):
                    email = found.email
                    email_status = found.status or "unknown"
                    email_confidence = max(0.0, min(found.score / 100.0, 1.0))

                    verification = await self.hunter.verify_email(found.email)
                    if verification:
                        email_status = verification.status or email_status
                        if verification.score is not None:
                            email_confidence = max(0.0, min(verification.score / 100.0, 1.0))

        if strict_executive:
            if owner_name and not self._is_strict_executive_title(owner_title) and not used_secondary_leadership_fallback:
                owner_name = None
                owner_title = None

            if email and owner_name and not self._email_matches_owner(email, owner_name):
                email = None
                email_status = "owner_mismatch"
                email_confidence = 0.0

            if email and not owner_name:
                email = None
                email_status = "missing_exec_owner"
                email_confidence = 0.0

            if not owner_name and email_status == "unknown":
                email_status = "missing_exec_owner"

        business_phone = business.phone
        phone_status = "unknown"
        phone_type: str | None = None

        if not business_phone and web_exec_phone:
            business_phone = web_exec_phone
            phone_status = web_exec_phone_status

        if not business_phone and website_enrichment and website_enrichment.phone:
            business_phone = website_enrichment.phone
            phone_status = website_enrichment.phone_status

        has_twilio = bool(self.settings.twilio_account_sid and self.settings.twilio_auth_token)
        if business_phone and has_twilio:
            sources.append("twilio_lookup")
            phone_check = await self.twilio.lookup(business_phone)
            phone_type = phone_check.line_type
            if phone_check.valid is True:
                phone_status = "valid"
            elif phone_check.valid is False:
                phone_status = "invalid"

        lead_score = self._score_lead(
            email_confidence=email_confidence,
            phone_status=phone_status,
            has_owner=bool(owner_name),
        )

        if self.settings.demo_mode:
            sources.append("demo_mode")

        unique_sources = list(dict.fromkeys(sources))

        return LeadCreate(
            business_name=resolved_business_name,
            business_website=business.website,
            business_phone=business_phone,
            city=city,
            search_query=search_query,
            owner_name=owner_name,
            owner_title=owner_title,
            email=email,
            email_status=email_status,
            email_confidence=round(email_confidence, 2),
            phone_status=phone_status,
            phone_type=phone_type,
            data_source_notes=", ".join(unique_sources),
            lead_score=round(lead_score, 2),
        )

    def _pick_best_owner(
        self,
        officers: list[OfficerCandidate],
        require_executive: bool = False,
    ) -> OfficerCandidate | None:
        if not officers:
            return None

        filtered = officers
        if require_executive:
            filtered = [officer for officer in officers if self._is_strict_executive_title(officer.title)]
            if not filtered:
                return None

        ranked = sorted(filtered, key=lambda officer: self._title_rank(officer.title), reverse=True)
        return ranked[0] if ranked else None

    def _pick_best_domain_person(
        self,
        people: list[DomainPerson],
        require_executive: bool = False,
    ) -> DomainPerson | None:
        if not people:
            return None

        filtered = people
        if require_executive:
            filtered = [person for person in people if self._is_strict_executive_title(person.title)]
            if not filtered:
                return None

        ranked = sorted(
            filtered,
            key=lambda person: (
                self._title_rank(person.title),
                person.confidence or 0.0,
                1 if "@" not in person.full_name else 0,
            ),
            reverse=True,
        )
        return ranked[0]

    def _pick_best_web_exec_candidate(
        self,
        candidates: list[WebExecutiveCandidate],
    ) -> WebExecutiveCandidate | None:
        if not candidates:
            return None

        ranked = sorted(
            candidates,
            key=lambda candidate: (
                self._title_rank(candidate.title),
                candidate.confidence,
                1 if candidate.email else 0,
                1 if candidate.phone else 0,
            ),
            reverse=True,
        )
        return ranked[0]

    @staticmethod
    def _title_rank(title: str | None) -> int:
        if not title:
            return 0

        title_lower = title.lower()
        for index, keyword in enumerate(TITLE_PRIORITY):
            if keyword in title_lower:
                return len(TITLE_PRIORITY) - index
        return 1

    @staticmethod
    def _is_strict_executive_title(title: str | None) -> bool:
        if not title:
            return False
        title_lower = title.lower()
        return any(keyword in title_lower for keyword in STRICT_EXECUTIVE_KEYWORDS)

    @staticmethod
    def _is_secondary_leadership_title(title: str | None) -> bool:
        if not title:
            return False
        title_lower = title.lower()
        if LeadPipeline._is_strict_executive_title(title):
            return False
        return any(keyword in title_lower for keyword in SECONDARY_LEADERSHIP_KEYWORDS)

    def _pick_best_secondary_owner(self, officers: list[OfficerCandidate]) -> OfficerCandidate | None:
        if not officers:
            return None

        filtered = [officer for officer in officers if self._is_secondary_leadership_title(officer.title)]
        if not filtered:
            return None

        ranked = sorted(filtered, key=lambda officer: self._title_rank(officer.title), reverse=True)
        return ranked[0]

    @staticmethod
    def _email_matches_owner(email: str, owner_name: str) -> bool:
        local_part = email.split("@", 1)[0].lower()
        clean_name = re.sub(r"[^A-Za-z\s]", " ", owner_name)
        tokens = [token.lower() for token in clean_name.split() if token]
        if len(tokens) < 2:
            return False

        first = tokens[0]
        last = tokens[-1]
        compact = re.sub(r"[^a-z0-9]", "", local_part)
        if not compact:
            return False

        if first in compact and last in compact:
            return True

        if len(first) >= 1 and f"{first[0]}{last}" in compact:
            return True

        if len(last) >= 1 and f"{first}{last[0]}" in compact:
            return True

        if first == compact or last == compact:
            return True

        return False

    @staticmethod
    def _is_better_business_name(
        current: str | None,
        candidate: str | None,
        website_domain: str | None,
    ) -> bool:
        if not candidate:
            return False

        candidate_clean = " ".join(candidate.strip().split())
        if len(candidate_clean) < 2:
            return False

        current_clean = " ".join((current or "").strip().split())
        if not current_clean:
            return True

        if current_clean.lower() == candidate_clean.lower():
            return False

        if LeadPipeline._looks_like_domain_name(current_clean):
            return True

        domain_label = LeadPipeline._domain_label(website_domain)
        if domain_label and current_clean.lower() == domain_label:
            return True

        if len(candidate_clean) > len(current_clean) and " " in candidate_clean and " " not in current_clean:
            return True

        return False

    @staticmethod
    def _looks_like_domain_name(value: str) -> bool:
        return "." in value and " " not in value

    @staticmethod
    def _domain_label(domain: str | None) -> str | None:
        if not domain:
            return None

        normalized = domain.lower().strip()
        if normalized.startswith("www."):
            normalized = normalized[4:]

        parts = [part for part in normalized.split(".") if part]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return parts[-2]

    @staticmethod
    def _score_lead(email_confidence: float, phone_status: str, has_owner: bool) -> float:
        score = 0.0
        score += email_confidence * 60.0

        if phone_status == "valid":
            score += 25.0
        elif phone_status in {"possible", "unknown"}:
            score += 10.0

        if has_owner:
            score += 15.0

        return min(score, 100.0)
