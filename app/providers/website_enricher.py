from dataclasses import dataclass
import asyncio
import json
import logging
import re
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import dns.exception
import dns.resolver
import httpx
import phonenumbers

from app.core.config import Settings
from app.utils.normalizers import normalize_website_url

logger = logging.getLogger(__name__)

CONTACT_LINK_HINTS = ("contact", "about", "team", "staff", "leadership", "our-story")
OWNER_TITLE_NORMALIZATION = [
    ("chief executive officer", "CEO"),
    ("co-owner", "Co-Owner"),
    ("owner", "Owner"),
    ("co-founder", "Co-Founder"),
    ("founder", "Founder"),
    ("chief executive", "CEO"),
    ("ceo", "CEO"),
    ("president", "President"),
    ("principal", "Principal"),
    ("managing director", "Managing Director"),
    ("director", "Director"),
    ("partner", "Partner"),
    ("dentist", "Dentist"),
    ("doctor", "Doctor"),
]
STRICT_EXECUTIVE_TITLE_KEYWORDS = (
    "founder",
    "co-founder",
    "cofounder",
    "ceo",
    "chief executive",
    "chief executive officer",
)
ORG_JSONLD_TYPES = {
    "Organization",
    "Corporation",
    "LocalBusiness",
    "ProfessionalService",
    "Dentist",
    "MedicalOrganization",
    "Hospital",
    "Store",
}
BUSINESS_TITLE_SPLIT_RE = re.compile(r"\s(?:\||-|–|—|::|:)\s")
BUSINESS_NAME_STOPWORDS = {
    "home",
    "homepage",
    "welcome",
    "official site",
    "official website",
}
GENERIC_LOCAL_PARTS = {
    "admin",
    "appointments",
    "booking",
    "care",
    "contact",
    "hello",
    "info",
    "office",
    "sales",
    "smile",
    "support",
    "team",
}
GENERIC_LOCAL_HINTS = (
    "admin",
    "book",
    "care",
    "contact",
    "desk",
    "hello",
    "info",
    "office",
    "sales",
    "service",
    "smile",
    "support",
    "team",
)
NAME_STOPWORDS = {
    "all",
    "and",
    "in",
    "at",
    "for",
    "on",
    "of",
    "about",
    "appointment",
    "book",
    "clinic",
    "dental",
    "dentist",
    "gallery",
    "learn",
    "more",
    "now",
    "park",
    "practices",
    "schedule",
    "studio",
    "team",
    "today",
    "our",
    "other",
    "their",
    "your",
    "with",
    "from",
    "this",
    "that",
    "these",
    "those",
    "to",
    "best",
    "learn",
    "more",
    "business",
    "call",
    "home",
    "main",
    "menu",
    "navigation",
    "owner",
    "phone",
    "perform",
    "receive",
    "service",
    "services",
    "ship",
    "view",
    "visit",
}
NAME_TOKEN_BLOCKLIST = {
    "co",
    "com",
    "corp",
    "company",
    "clinic",
    "dental",
    "dentistry",
    "group",
    "inc",
    "llp",
    "llc",
    "ltd",
    "office",
    "services",
    "studio",
    "www",
    "http",
    "https",
}
EMAIL_EXTRACT_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
EMAIL_STRICT_RE = re.compile(r"(?i)^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$")
OBFUSCATED_EMAIL_RE = re.compile(
    r"(?i)\b([A-Z0-9._%+-]{2,})\s*(?:\(|\[)?at(?:\)|\])?\s*([A-Z0-9.-]{2,})\s*(?:\(|\[)?dot(?:\)|\])?\s*([A-Z]{2,})\b"
)
OWNER_TITLE_REGEX = "|".join(re.escape(item[0]) for item in sorted(OWNER_TITLE_NORMALIZATION, key=lambda x: len(x[0]), reverse=True))
NAME_REGEX = r"(?:Dr\.?\s+)?[A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+){1,2}"
NAME_THEN_TITLE_RE = re.compile(
    rf"(?i)\b(?P<name>{NAME_REGEX})\s*(?:,|\||-|–|—)?\s*(?P<title>{OWNER_TITLE_REGEX}(?:\s*/\s*[A-Za-z& ]+)?)\b"
)
TITLE_THEN_NAME_RE = re.compile(
    rf"(?i)\b(?P<title>{OWNER_TITLE_REGEX})\s*(?:,|:|\||-|–|—)?\s*(?P<name>{NAME_REGEX})\b"
)


@dataclass(slots=True)
class EmailEvidence:
    email: str
    source: str
    page_url: str


@dataclass(slots=True)
class PersonCandidate:
    name: str
    title: str | None
    source: str
    page_url: str


@dataclass(slots=True)
class WebsiteEnrichment:
    business_name: str | None = None
    owner_name: str | None = None
    owner_title: str | None = None
    email: str | None = None
    email_status: str = "unknown"
    email_confidence: float = 0.0
    phone: str | None = None
    phone_status: str = "unknown"

    def has_any_data(self) -> bool:
        return bool(self.business_name or self.owner_name or self.email or self.phone)


class WebsiteEnricher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def enrich(
        self,
        website_url: str | None,
        prefer_executive: bool = False,
    ) -> WebsiteEnrichment:
        normalized_url = normalize_website_url(website_url)
        if not normalized_url:
            return WebsiteEnrichment()

        pages = await self._crawl_pages(normalized_url)
        if not pages:
            return WebsiteEnrichment()

        combined_text = " ".join(page["text"] for page in pages if page.get("text"))
        business_name = self._pick_best_business_name(pages=pages, start_url=normalized_url)

        owner_name, owner_title = self._pick_best_owner(
            pages=pages,
            require_executive=prefer_executive,
        )
        email, email_status, email_confidence = await self._pick_best_email(
            pages=pages,
            start_url=normalized_url,
            owner_name=owner_name,
            require_owner_match=prefer_executive,
        )

        if not owner_name and email and not prefer_executive:
            inferred_name = self._name_from_email_local(email)
            if inferred_name:
                owner_name = inferred_name
                owner_title = owner_title or "Contact"

        phone, phone_status = self._pick_best_phone(pages)
        if not phone:
            phone, phone_status = self._extract_phone(combined_text)

        return WebsiteEnrichment(
            business_name=business_name,
            owner_name=owner_name,
            owner_title=owner_title,
            email=email,
            email_status=email_status,
            email_confidence=email_confidence,
            phone=phone,
            phone_status=phone_status,
        )

    async def _crawl_pages(self, start_url: str) -> list[dict[str, str]]:
        headers = {"User-Agent": self.settings.outbound_user_agent}
        root_host = self._normalize_host(urlparse(start_url).netloc)
        visited: set[str] = set()
        to_visit: list[str] = [start_url]
        pages: list[dict[str, str]] = []

        async with httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            while to_visit and len(pages) < self.settings.website_crawl_max_pages:
                url = to_visit.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                page = await self._fetch_page(client, url)
                if not page:
                    continue

                pages.append(page)

                for candidate in self._extract_candidate_links(base_url=url, html=page["html"]):
                    host = self._normalize_host(urlparse(candidate).netloc)
                    if host != root_host:
                        continue
                    if candidate in visited or candidate in to_visit:
                        continue
                    to_visit.append(candidate)

        return pages

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> dict[str, str] | None:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return None

        html = response.text[:500_000]
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "noscript"]):
            tag.extract()

        text = soup.get_text(separator=" ", strip=True)
        return {"url": url, "html": html, "text": text}

    @staticmethod
    def _extract_candidate_links(base_url: str, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for tag in soup.find_all("a", href=True):
            href = (tag.get("href") or "").strip()
            if not href:
                continue

            label = f"{href} {(tag.get_text(' ', strip=True) or '')}".lower()
            if not any(hint in label for hint in CONTACT_LINK_HINTS):
                continue

            absolute = normalize_website_url(urljoin(base_url, href))
            if absolute:
                links.append(absolute)

        return links

    def _pick_best_owner(
        self,
        pages: list[dict[str, str]],
        require_executive: bool = False,
    ) -> tuple[str | None, str | None]:
        candidates = self._collect_owner_candidates(pages)
        if not candidates:
            return None, None

        if require_executive:
            candidates = [candidate for candidate in candidates if self._is_strict_executive_title(candidate.title)]
            if not candidates:
                return None, None

            high_signal = [
                candidate
                for candidate in candidates
                if candidate.source in {"jsonld_founder", "jsonld_person"}
            ]
            if not high_signal:
                return None, None
            candidates = high_signal

        ranked = sorted(candidates, key=self._owner_candidate_score, reverse=True)
        best = ranked[0]
        return best.name, best.title

    def _collect_owner_candidates(self, pages: list[dict[str, str]]) -> list[PersonCandidate]:
        found: dict[tuple[str, str], PersonCandidate] = {}

        for page in pages:
            page_url = page.get("url") or ""
            html = page.get("html") or ""
            text = page.get("text") or ""
            soup = BeautifulSoup(html, "html.parser")

            current: list[PersonCandidate] = []
            current.extend(self._jsonld_owner_candidates(soup=soup, page_url=page_url))
            current.extend(self._heading_owner_candidates(soup=soup, page_url=page_url))
            current.extend(self._text_owner_candidates(text=text, page_url=page_url))

            for candidate in current:
                key = (candidate.name.lower(), (candidate.title or "").lower())
                previous = found.get(key)
                if not previous or self._owner_candidate_score(candidate) > self._owner_candidate_score(previous):
                    found[key] = candidate

        return list(found.values())

    def _pick_best_business_name(self, pages: list[dict[str, str]], start_url: str) -> str | None:
        root_host = self._normalize_host(urlparse(start_url).netloc)
        candidates = self._collect_business_name_candidates(pages=pages)
        if not candidates:
            return None

        ranked = sorted(
            candidates,
            key=lambda item: self._score_business_name(candidate=item[0], source=item[1], root_host=root_host),
            reverse=True,
        )
        return ranked[0][0]

    def _collect_business_name_candidates(self, pages: list[dict[str, str]]) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        for page in pages:
            html = page.get("html") or ""
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")

            jsonld_names = self._business_name_candidates_from_jsonld(soup)
            for name in jsonld_names:
                normalized = self._normalize_business_name(name)
                if normalized and normalized.lower() not in seen:
                    seen.add(normalized.lower())
                    candidates.append((normalized, "jsonld"))

            for selector, source in (
                ("meta[property='og:site_name']", "meta_site_name"),
                ("meta[name='application-name']", "meta_site_name"),
                ("meta[name='apple-mobile-web-app-title']", "meta_site_name"),
            ):
                tag = soup.select_one(selector)
                raw = (tag.get("content") if tag else "") or ""
                normalized = self._normalize_business_name(raw)
                if normalized and normalized.lower() not in seen:
                    seen.add(normalized.lower())
                    candidates.append((normalized, source))

            title_text = (soup.title.get_text(" ", strip=True) if soup.title else "") or ""
            normalized_title = self._normalize_business_name(title_text)
            if normalized_title and normalized_title.lower() not in seen:
                seen.add(normalized_title.lower())
                candidates.append((normalized_title, "title"))

        return candidates

    def _business_name_candidates_from_jsonld(self, soup: BeautifulSoup) -> list[str]:
        candidates: list[str] = []

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue

            data = self._safe_json_loads(raw)
            if data is None:
                continue

            candidates.extend(self._extract_org_names_from_jsonld(data))

        return candidates

    def _extract_org_names_from_jsonld(self, data: object) -> list[str]:
        names: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return

            if not isinstance(node, dict):
                return

            types = self._normalize_jsonld_types(node.get("@type"))
            if types.intersection(ORG_JSONLD_TYPES):
                for key in ("name", "legalName", "alternateName"):
                    value = node.get(key)
                    if isinstance(value, str) and value.strip():
                        names.append(value.strip())

            for value in node.values():
                walk(value)

        walk(data)
        return names

    def _normalize_business_name(self, raw_value: str | None) -> str | None:
        if not raw_value:
            return None

        text = " ".join(str(raw_value).strip().split())
        if not text:
            return None

        parts = [part.strip() for part in BUSINESS_TITLE_SPLIT_RE.split(text) if part.strip()]
        if parts:
            text = parts[0]

        text = re.sub(r"\s+(inc\.?|llc|ltd\.?|pllc|corp\.?|company)$", "", text, flags=re.IGNORECASE).strip()
        if not text or len(text) < 2:
            return None

        if text.lower() in BUSINESS_NAME_STOPWORDS:
            return None

        if "@" in text or "http://" in text.lower() or "https://" in text.lower():
            return None

        if text.count(".") > 1 and " " not in text:
            return None

        if not re.search(r"[A-Za-z]", text):
            return None

        return text

    def _score_business_name(self, candidate: str, source: str, root_host: str) -> float:
        score = 0.0

        source_weights = {
            "jsonld": 0.78,
            "meta_site_name": 0.67,
            "title": 0.53,
        }
        score += source_weights.get(source, 0.45)

        candidate_lower = candidate.lower()
        domain_label = self._domain_label(root_host)
        if domain_label and domain_label in re.sub(r"[^a-z0-9]", "", candidate_lower):
            score += 0.12

        if " " in candidate:
            score += 0.06

        if len(candidate) > 70:
            score -= 0.2

        return score

    @staticmethod
    def _domain_label(host: str) -> str | None:
        normalized = host.lower().strip()
        if normalized.startswith("www."):
            normalized = normalized[4:]

        parts = [part for part in normalized.split(".") if part]
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return parts[-2]

    def _jsonld_owner_candidates(self, soup: BeautifulSoup, page_url: str) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = (script.string or script.get_text() or "").strip()
            if not raw:
                continue

            data = self._safe_json_loads(raw)
            if data is None:
                continue

            candidates.extend(self._extract_people_from_jsonld(data=data, page_url=page_url))

        return candidates

    def _extract_people_from_jsonld(self, data: object, page_url: str) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []

        def walk(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return

            if not isinstance(node, dict):
                return

            node_type = node.get("@type")
            types = self._normalize_jsonld_types(node_type)

            if "Person" in types:
                person_name = self._normalize_person_name(node.get("name"))
                person_title = self._normalize_owner_title(
                    (node.get("jobTitle") or "") + " " + (node.get("description") or "")
                )
                if person_name:
                    candidates.append(
                        PersonCandidate(
                            name=person_name,
                            title=person_title,
                            source="jsonld_person",
                            page_url=page_url,
                        )
                    )

            founder_node = node.get("founder") or node.get("founders")
            if founder_node:
                for founder in self._ensure_list(founder_node):
                    if isinstance(founder, dict):
                        founder_name = self._normalize_person_name(founder.get("name"))
                    else:
                        founder_name = self._normalize_person_name(str(founder))
                    if founder_name:
                        candidates.append(
                            PersonCandidate(
                                name=founder_name,
                                title="Founder",
                                source="jsonld_founder",
                                page_url=page_url,
                            )
                        )

            for value in node.values():
                walk(value)

        walk(data)
        return candidates

    def _heading_owner_candidates(self, soup: BeautifulSoup, page_url: str) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []

        for heading in soup.find_all(re.compile(r"^h[1-4]$")):
            heading_text = heading.get_text(" ", strip=True)
            name = self._normalize_person_name(heading_text)
            if not name:
                continue

            nearby = [heading_text]
            sibling = heading
            for _ in range(3):
                sibling = sibling.find_next_sibling()
                if not sibling:
                    break
                nearby.append(sibling.get_text(" ", strip=True))

            context = " ".join(nearby)
            title = self._normalize_owner_title(context)
            if not title:
                continue

            candidates.append(
                PersonCandidate(
                    name=name,
                    title=title,
                    source="heading",
                    page_url=page_url,
                )
            )

        return candidates

    def _text_owner_candidates(self, text: str, page_url: str) -> list[PersonCandidate]:
        candidates: list[PersonCandidate] = []

        for match in NAME_THEN_TITLE_RE.finditer(text):
            name = self._normalize_person_name(match.group("name"))
            title = self._normalize_owner_title(match.group("title"))
            if name and title:
                candidates.append(
                    PersonCandidate(name=name, title=title, source="text_pattern", page_url=page_url)
                )

        for match in TITLE_THEN_NAME_RE.finditer(text):
            name = self._normalize_person_name(match.group("name"))
            title = self._normalize_owner_title(match.group("title"))
            if name and title:
                candidates.append(
                    PersonCandidate(name=name, title=title, source="text_pattern", page_url=page_url)
                )

        return candidates

    async def _pick_best_email(
        self,
        pages: list[dict[str, str]],
        start_url: str,
        owner_name: str | None = None,
        require_owner_match: bool = False,
    ) -> tuple[str | None, str, float]:
        root_host = self._normalize_host(urlparse(start_url).netloc)
        candidates = self._collect_email_candidates(pages)
        if not candidates:
            return None, "unknown", 0.0

        mx_cache: dict[str, bool] = {}
        ranked: list[tuple[float, str, EmailEvidence]] = []

        for evidence in candidates.values():
            local_part, email_domain = evidence.email.split("@", 1)
            same_domain = self._is_same_or_subdomain(email_domain=email_domain, root_host=root_host)
            is_generic = self._is_generic_local(local_part)

            if self.settings.email_require_same_domain and not same_domain:
                continue

            if self.settings.email_reject_generic_locals and is_generic:
                continue

            if owner_name and require_owner_match and not self._email_local_matches_owner(local_part, owner_name):
                continue

            mx_valid = await self._domain_has_mail_route_cached(email_domain, mx_cache)
            if self.settings.email_require_mx and not mx_valid:
                continue

            score = self._score_email(
                evidence=evidence,
                same_domain=same_domain,
                mx_valid=mx_valid,
                is_generic=is_generic,
                owner_name=owner_name,
            )

            if score < self.settings.email_min_confidence:
                continue

            status = self._email_status(
                evidence=evidence,
                mx_valid=mx_valid,
                is_generic=is_generic,
            )
            ranked.append((score, status, evidence))

        if not ranked:
            return None, "rejected_low_confidence", 0.0

        ranked.sort(
            key=lambda item: (
                item[0],
                1 if item[2].source == "mailto" else 0,
                1 if self._is_contact_page(item[2].page_url) else 0,
            ),
            reverse=True,
        )

        best_score, best_status, best_evidence = ranked[0]
        return best_evidence.email, best_status, round(max(0.0, min(best_score, 1.0)), 2)

    def _collect_email_candidates(self, pages: list[dict[str, str]]) -> dict[str, EmailEvidence]:
        candidates: dict[str, EmailEvidence] = {}

        for page in pages:
            html = page.get("html") or ""
            text = page.get("text") or ""
            page_url = page.get("url") or ""
            soup = BeautifulSoup(html, "html.parser")

            evidences: list[EmailEvidence] = []
            evidences.extend(self._mailto_evidence(soup=soup, page_url=page_url))
            evidences.extend(self._cloudflare_evidence(soup=soup, page_url=page_url))
            evidences.extend(self._regex_evidence(raw_text=text, page_url=page_url, source="text"))
            evidences.extend(self._regex_evidence(raw_text=html, page_url=page_url, source="html"))
            evidences.extend(self._obfuscated_evidence(raw_text=text, page_url=page_url))

            for evidence in evidences:
                current = candidates.get(evidence.email)
                if not current:
                    candidates[evidence.email] = evidence
                    continue

                if self._evidence_priority(evidence) > self._evidence_priority(current):
                    candidates[evidence.email] = evidence

        return candidates

    def _mailto_evidence(self, soup: BeautifulSoup, page_url: str) -> list[EmailEvidence]:
        entries: list[EmailEvidence] = []
        for tag in soup.find_all("a", href=True):
            href = (tag.get("href") or "").strip()
            if not href.lower().startswith("mailto:"):
                continue

            raw = href.split(":", 1)[1]
            raw = raw.split("?", 1)[0]
            email = self._normalize_email(unquote(raw))
            if email:
                entries.append(EmailEvidence(email=email, source="mailto", page_url=page_url))

        return entries

    def _cloudflare_evidence(self, soup: BeautifulSoup, page_url: str) -> list[EmailEvidence]:
        entries: list[EmailEvidence] = []
        for tag in soup.select("[data-cfemail]"):
            encoded = (tag.get("data-cfemail") or "").strip()
            decoded = self._decode_cloudflare_email(encoded)
            email = self._normalize_email(decoded)
            if email:
                entries.append(EmailEvidence(email=email, source="cloudflare", page_url=page_url))
        return entries

    def _obfuscated_evidence(self, raw_text: str, page_url: str) -> list[EmailEvidence]:
        entries: list[EmailEvidence] = []
        for match in OBFUSCATED_EMAIL_RE.finditer(raw_text):
            local_part = match.group(1)
            domain = match.group(2)
            tld = match.group(3)
            email = self._normalize_email(f"{local_part}@{domain}.{tld}")
            if email:
                entries.append(EmailEvidence(email=email, source="obfuscated", page_url=page_url))
        return entries

    def _regex_evidence(self, raw_text: str, page_url: str, source: str) -> list[EmailEvidence]:
        entries: list[EmailEvidence] = []
        for match in EMAIL_EXTRACT_RE.findall(raw_text):
            email = self._normalize_email(match)
            if email:
                entries.append(EmailEvidence(email=email, source=source, page_url=page_url))
        return entries

    @staticmethod
    def _normalize_email(raw_value: str | None) -> str | None:
        if not raw_value:
            return None

        email = raw_value.strip().lower()
        email = email.strip(" <>[]{}()\"'.,;:")
        if not email or not EMAIL_STRICT_RE.match(email):
            return None

        local_part = email.split("@", 1)[0]
        if len(local_part) < 2:
            return None

        return email

    def _score_email(
        self,
        evidence: EmailEvidence,
        same_domain: bool,
        mx_valid: bool,
        is_generic: bool,
        owner_name: str | None,
    ) -> float:
        local = evidence.email.split("@", 1)[0]
        score = 0.0

        if evidence.source == "mailto":
            score += 0.62
        elif evidence.source == "cloudflare":
            score += 0.59
        elif evidence.source == "text":
            score += 0.50
        elif evidence.source == "obfuscated":
            score += 0.49
        else:
            score += 0.46

        if self._is_contact_page(evidence.page_url):
            score += 0.08

        if same_domain:
            score += 0.12
        else:
            score -= 0.22

        if self._looks_personal_local(local):
            score += 0.08

        score += self._owner_email_match_bonus(local, owner_name)

        if is_generic:
            score -= 0.30

        if mx_valid:
            score += 0.14
        else:
            score -= 0.45

        return max(0.0, min(score, 1.0))

    def _email_status(self, evidence: EmailEvidence, mx_valid: bool, is_generic: bool) -> str:
        if not mx_valid:
            return "no_mx"
        if is_generic:
            return "generic_inbox"
        if evidence.source in {"mailto", "cloudflare"} and self._is_contact_page(evidence.page_url):
            return "high_confidence"
        if evidence.source in {"mailto", "cloudflare"}:
            return "verified_mx"
        return "pattern_match"

    def _owner_candidate_score(self, candidate: PersonCandidate) -> float:
        score = 0.0

        source_weight = {
            "jsonld_founder": 0.72,
            "jsonld_person": 0.58,
            "heading": 0.62,
            "text_pattern": 0.52,
        }.get(candidate.source, 0.45)
        score += source_weight

        score += self._owner_title_rank(candidate.title) * 0.06
        score += self._page_context_score(candidate.page_url)

        token_count = len([t for t in candidate.name.replace(".", "").split() if t.lower() != "dr"])
        if 2 <= token_count <= 3:
            score += 0.05
        elif token_count > 4:
            score -= 0.12

        return score

    def _normalize_person_name(self, raw_name: str | None) -> str | None:
        if not raw_name:
            return None

        name = " ".join(str(raw_name).strip().split())
        name = re.sub(r"(?i),?\s*(dds|dmd|md|ms|phd|esq|rdh|faacd|fagd|jr|sr)\.?$", "", name).strip()

        if not name:
            return None

        tokens = name.replace(".", " ").split()
        normalized_tokens: list[str] = []
        for index, token in enumerate(tokens):
            clean = re.sub(r"[^A-Za-z'`-]", "", token)
            if not clean:
                continue
            if index == 0 and clean.lower() == "dr":
                normalized_tokens.append("Dr")
                continue
            normalized_tokens.append(clean.capitalize())

        if len(normalized_tokens) < 2:
            return None

        non_prefix = [token for token in normalized_tokens if token.lower() != "dr"]
        if len(non_prefix) < 2 or len(non_prefix) > 4:
            return None

        lowered_tokens = [token.lower() for token in non_prefix]
        if any(token in NAME_TOKEN_BLOCKLIST for token in lowered_tokens):
            return None

        if any(len(token) < 2 for token in non_prefix):
            return None

        if lowered_tokens[0] in NAME_STOPWORDS or lowered_tokens[-1] in NAME_STOPWORDS:
            return None

        if any(token in NAME_STOPWORDS for token in lowered_tokens):
            return None

        lowered = {token.lower() for token in non_prefix}
        if lowered.issubset(NAME_STOPWORDS):
            return None

        rebuilt = []
        for token in normalized_tokens:
            if token == "Dr":
                rebuilt.append("Dr.")
            else:
                rebuilt.append(token)

        full_name = " ".join(rebuilt)
        if not self._looks_like_person_name(full_name):
            return None

        return full_name

    @staticmethod
    def _looks_like_person_name(name: str) -> bool:
        tokens = [token for token in name.replace(".", " ").split() if token.lower() != "dr"]
        if len(tokens) < 2:
            return False

        for token in tokens:
            if not re.fullmatch(r"[A-Za-z][A-Za-z'`-]{1,}", token):
                return False

        return True

    def _normalize_owner_title(self, raw_title: str | None) -> str | None:
        if not raw_title:
            return None

        text = str(raw_title).lower()
        for keyword, normalized in OWNER_TITLE_NORMALIZATION:
            if keyword in text:
                return normalized

        return None

    def _owner_title_rank(self, title: str | None) -> int:
        if not title:
            return 0

        title_lower = title.lower()
        ranking = {
            "founder": 8,
            "co-founder": 7,
            "ceo": 7,
            "chief executive": 7,
            "owner": 6,
            "co-owner": 5,
            "president": 4,
            "principal": 4,
            "managing director": 4,
            "director": 3,
            "partner": 3,
            "dentist": 2,
            "doctor": 2,
            "contact": 1,
        }
        for keyword, rank in ranking.items():
            if keyword in title_lower:
                return rank
        return 1

    def _is_strict_executive_title(self, title: str | None) -> bool:
        if not title:
            return False

        title_lower = title.lower()
        return any(keyword in title_lower for keyword in STRICT_EXECUTIVE_TITLE_KEYWORDS)

    def _owner_email_match_bonus(self, local_part: str, owner_name: str | None) -> float:
        if not owner_name:
            return 0.0

        tokens = [token.lower() for token in owner_name.replace(".", " ").split() if token.lower() != "dr"]
        if len(tokens) < 2:
            return 0.0

        first, last = tokens[0], tokens[-1]
        local = local_part.lower()

        if first in local and last in local:
            return 0.14
        if last in local:
            return 0.09
        if first in local:
            return 0.05
        if len(first) >= 1 and f"{first[0]}{last}" in local:
            return 0.1

        return 0.0

    def _email_local_matches_owner(self, local_part: str, owner_name: str | None) -> bool:
        if not owner_name:
            return False

        tokens = [token.lower() for token in owner_name.replace(".", " ").split() if token.lower() != "dr"]
        if len(tokens) < 2:
            return False

        first, last = tokens[0], tokens[-1]
        local = re.sub(r"[^a-z0-9]", "", local_part.lower())
        if not local:
            return False

        if first in local and last in local:
            return True

        if len(first) >= 1 and f"{first[0]}{last}" in local:
            return True

        if len(last) >= 1 and f"{first}{last[0]}" in local:
            return True

        return False

    @staticmethod
    def _looks_personal_local(local_part: str) -> bool:
        if re.fullmatch(r"[a-z]+[._-][a-z]+", local_part):
            return True
        if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}[._-][a-z]{2,}", local_part):
            return True
        if re.fullmatch(r"[a-z][a-z]+[0-9]{0,2}", local_part):
            return True
        return False

    def _is_generic_local(self, local_part: str) -> bool:
        local = local_part.lower().strip()
        if local in GENERIC_LOCAL_PARTS:
            return True

        if self._looks_personal_local(local):
            return False

        return any(hint in local for hint in GENERIC_LOCAL_HINTS)

    @staticmethod
    def _evidence_priority(evidence: EmailEvidence) -> int:
        base = 0
        if evidence.source in {"mailto", "cloudflare"}:
            base += 3
        elif evidence.source == "text":
            base += 2
        else:
            base += 1

        if WebsiteEnricher._is_contact_page(evidence.page_url):
            base += 1

        return base

    async def _domain_has_mail_route_cached(self, domain: str, cache: dict[str, bool]) -> bool:
        key = domain.lower().strip()
        if key not in cache:
            cache[key] = await asyncio.to_thread(self._domain_has_mail_route, key)
        return cache[key]

    @staticmethod
    def _domain_has_mail_route(domain: str) -> bool:
        resolver = dns.resolver.Resolver(configure=True)
        resolver.lifetime = 4.0

        try:
            answers = resolver.resolve(domain, "MX")
            if answers:
                return True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass
        except dns.exception.DNSException:
            return False

        try:
            a_answers = resolver.resolve(domain, "A")
            if a_answers:
                return True
        except dns.exception.DNSException:
            pass

        try:
            aaaa_answers = resolver.resolve(domain, "AAAA")
            if aaaa_answers:
                return True
        except dns.exception.DNSException:
            return False

        return False

    @staticmethod
    def _decode_cloudflare_email(encoded: str) -> str | None:
        if len(encoded) < 4 or len(encoded) % 2 != 0:
            return None
        try:
            key = int(encoded[:2], 16)
            decoded_chars = []
            for index in range(2, len(encoded), 2):
                value = int(encoded[index:index + 2], 16) ^ key
                decoded_chars.append(chr(value))
            return "".join(decoded_chars)
        except ValueError:
            return None

    @staticmethod
    def _normalize_host(host: str) -> str:
        normalized = host.strip().lower()
        if normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized

    @staticmethod
    def _is_same_or_subdomain(email_domain: str, root_host: str) -> bool:
        candidate = WebsiteEnricher._normalize_host(email_domain)
        if candidate == root_host:
            return True
        return candidate.endswith(f".{root_host}")

    @staticmethod
    def _is_contact_page(url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(hint in path for hint in CONTACT_LINK_HINTS)

    @staticmethod
    def _page_context_score(url: str) -> float:
        path = urlparse(url).path.lower()
        if any(hint in path for hint in ("team", "staff", "leadership", "about")):
            return 0.12
        if "contact" in path:
            return 0.08
        return 0.02

    def _name_from_email_local(self, email: str) -> str | None:
        local = email.split("@", 1)[0]
        if self._is_generic_local(local):
            return None

        parts = [part for part in re.split(r"[._-]+", local) if part and part.isalpha()]
        if len(parts) < 2:
            return None

        candidate = " ".join(part.capitalize() for part in (parts[0], parts[-1]))
        if not self._looks_like_person_name(candidate):
            return None
        return candidate

    @staticmethod
    def _safe_json_loads(raw: str) -> object | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _normalize_jsonld_types(raw_type: object) -> set[str]:
        if isinstance(raw_type, list):
            return {str(item) for item in raw_type}
        if isinstance(raw_type, str):
            return {raw_type}
        return set()

    @staticmethod
    def _ensure_list(value: object) -> list[object]:
        if isinstance(value, list):
            return value
        return [value]

    def _pick_best_phone(self, pages: list[dict[str, str]]) -> tuple[str | None, str]:
        region = self.settings.phone_default_region.upper()
        best_phone: str | None = None
        best_status = "unknown"
        best_score = -1.0
        seen: set[str] = set()

        for page in pages:
            page_url = page.get("url") or ""
            text = page.get("text") or ""
            page_score = self._page_context_score(page_url)
            if self._is_contact_page(page_url):
                page_score += 0.1

            for match in phonenumbers.PhoneNumberMatcher(text, region):
                number = match.number
                if not phonenumbers.is_possible_number(number):
                    continue

                formatted = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
                if formatted in seen:
                    continue
                seen.add(formatted)

                if phonenumbers.is_valid_number(number):
                    status = "valid"
                    score = 1.0 + page_score
                else:
                    status = "possible"
                    score = 0.55 + page_score

                if score > best_score:
                    best_phone = formatted
                    best_status = status
                    best_score = score

        return best_phone, best_status

    def _extract_phone(self, text: str) -> tuple[str | None, str]:
        region = self.settings.phone_default_region.upper()
        for match in phonenumbers.PhoneNumberMatcher(text, region):
            number = match.number
            if not phonenumbers.is_possible_number(number):
                continue

            formatted = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
            if phonenumbers.is_valid_number(number):
                return formatted, "valid"
            return formatted, "possible"

        return None, "unknown"
