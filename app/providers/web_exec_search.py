import base64
from dataclasses import dataclass
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
import phonenumbers

from app.core.config import Settings
from app.utils.normalizers import extract_domain

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://search.brave.com/search"
BING_SEARCH_URL = "https://www.bing.com/search"
NAME_REGEX = r"(?:Dr\.?\s+)?[A-Z][A-Za-z'`-]+(?:\s+[A-Z][A-Za-z'`-]+){1,2}"
EXEC_TITLE_REGEX = r"co-?founder|founder|chief executive officer|chief executive|ceo"
NAME_NEAR_TITLE_RE = re.compile(
    rf"(?i)\b(?P<name>{NAME_REGEX})\b[^\n\r]{{0,80}}?\b(?P<title>{EXEC_TITLE_REGEX})\b"
)
TITLE_NEAR_NAME_RE = re.compile(
    rf"(?i)\b(?P<title>{EXEC_TITLE_REGEX})\b[^\n\r]{{0,48}}?\b(?P<name>{NAME_REGEX})\b"
)
FOUNDED_BY_RE = re.compile(
    rf"(?i)\b(?:founded|co-founded)\s+by\s+(?P<name>{NAME_REGEX})\b"
)
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
EMAIL_STRICT_RE = re.compile(r"(?i)^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$")

GENERIC_EMAIL_LOCALS = {
    "admin",
    "contact",
    "hello",
    "info",
    "office",
    "sales",
    "support",
    "team",
}
NAME_BLOCKLIST = {
    "and",
    "announces",
    "appoints",
    "best",
    "co",
    "com",
    "company",
    "for",
    "founder",
    "from",
    "group",
    "hit",
    "in",
    "inc",
    "learn",
    "more",
    "of",
    "our",
    "play",
    "says",
    "services",
    "the",
    "to",
    "welcomes",
    "with",
}
TOKEN_STOPWORDS = {
    "and",
    "company",
    "for",
    "group",
    "inc",
    "llc",
    "ltd",
    "of",
    "official",
    "services",
    "site",
    "the",
}
SEARCH_HOST_BLOCKLIST = {
    "bing.com",
    "www.bing.com",
    "search.brave.com",
    "duckduckgo.com",
    "html.duckduckgo.com",
}
TRUSTED_EXEC_HOST_SUFFIXES = (
    "wikipedia.org",
    "wikidata.org",
    "linkedin.com",
    "crunchbase.com",
    "pitchbook.com",
    "bloomberg.com",
    "reuters.com",
    "forbes.com",
    "britannica.com",
)


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass(slots=True)
class WebExecutiveCandidate:
    name: str
    title: str
    source_url: str
    confidence: float
    email: str | None = None
    phone: str | None = None


class WebExecutiveSearchProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search_executives(
        self,
        business_name: str,
        website: str | None,
        limit: int = 6,
        include_company_website: bool = True,
    ) -> list[WebExecutiveCandidate]:
        normalized_business = " ".join((business_name or "").strip().split())
        if not normalized_business:
            return []

        domain = extract_domain(website)
        query = self._build_query(business_name=normalized_business, domain=domain)
        results = await self._search(query=query, limit=max(limit, 10))
        if not results:
            return []

        candidates: dict[tuple[str, str], WebExecutiveCandidate] = {}

        for result in results[: max(limit, 10)]:
            text = f"{result.title}. {result.snippet}".strip()
            for candidate in self._extract_candidates_from_text(
                text=text,
                business_name=normalized_business,
                source_url=result.url,
                domain=domain,
                source_weight=0.68,
                include_company_website=include_company_website,
            ):
                self._merge_candidate(candidates, candidate)

        fetch_count = min(5, len(results))
        if fetch_count > 0:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.settings.outbound_user_agent},
            ) as client:
                for result in results[:fetch_count]:
                    page = await self._fetch_page(client=client, url=result.url)
                    if not page:
                        continue

                    page_candidates = self._extract_candidates_from_text(
                        text=page["text"],
                        business_name=normalized_business,
                        source_url=result.url,
                        domain=domain,
                        source_weight=0.58,
                        include_company_website=include_company_website,
                    )
                    if not page_candidates:
                        continue

                    emails = self._extract_emails(text=page["text"], html=page["html"], preferred_domain=domain)
                    phones = self._extract_phones(text=page["text"])

                    for candidate in page_candidates:
                        email, email_bonus = self._pick_best_email_for_candidate(
                            owner_name=candidate.name,
                            emails=emails,
                        )
                        if email:
                            candidate.email = email
                            candidate.confidence = min(1.0, candidate.confidence + email_bonus)

                        if phones:
                            candidate.phone = phones[0]
                            candidate.confidence = min(1.0, candidate.confidence + 0.03)

                        self._merge_candidate(candidates, candidate)

        ranked = sorted(
            candidates.values(),
            key=lambda item: (
                item.confidence,
                self._title_rank(item.title),
                1 if item.email else 0,
                1 if item.phone else 0,
            ),
            reverse=True,
        )
        return ranked[:limit]

    async def _search(self, query: str, limit: int) -> list[SearchResult]:
        brave = await self._search_brave(query=query, limit=limit)
        if brave:
            return brave

        bing = await self._search_bing(query=query, limit=limit)
        return bing

    async def _search_brave(self, query: str, limit: int) -> list[SearchResult]:
        params = {"q": query, "source": "web"}
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.settings.outbound_user_agent},
            ) as client:
                response = await client.get(BRAVE_SEARCH_URL, params=params)
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError as exc:
            logger.warning("Brave search failed for query '%s': %s", query, exc)
            return []

        soup = BeautifulSoup(html, "html.parser")
        rows: list[SearchResult] = []

        for node in soup.select("div[data-type='web']"):
            link = node.select_one("h2 a[href]") or node.select_one("a[href]")
            if not link:
                continue

            href = self._decode_search_result_url((link.get("href") or "").strip())
            if not href or self._is_search_engine_url(href):
                continue

            title = link.get_text(" ", strip=True)
            snippet_node = node.select_one("div.snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            rows.append(SearchResult(url=href, title=title, snippet=snippet))

            if len(rows) >= limit:
                break

        return rows

    async def _search_bing(self, query: str, limit: int) -> list[SearchResult]:
        params = {
            "q": query,
            "mkt": "en-US",
            "setLang": "en-US",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.settings.outbound_user_agent},
            ) as client:
                response = await client.get(BING_SEARCH_URL, params=params)
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError as exc:
            logger.warning("Bing search failed for query '%s': %s", query, exc)
            return []

        soup = BeautifulSoup(html, "html.parser")
        rows: list[SearchResult] = []

        for node in soup.select("li.b_algo"):
            link = node.select_one("h2 a[href]")
            if not link:
                continue

            href = self._decode_search_result_url((link.get("href") or "").strip())
            if not href or self._is_search_engine_url(href):
                continue

            title = link.get_text(" ", strip=True)
            snippet_node = node.select_one("div.b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            rows.append(SearchResult(url=href, title=title, snippet=snippet))

            if len(rows) >= limit:
                break

        return rows

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> dict[str, str] | None:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return None

        html = response.text[:450_000]
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.extract()

        text = soup.get_text(" ", strip=True)
        if len(text) > 250_000:
            text = text[:250_000]
        return {"html": html, "text": text}

    def _extract_candidates_from_text(
        self,
        text: str,
        business_name: str,
        source_url: str,
        domain: str | None,
        source_weight: float,
        include_company_website: bool,
    ) -> list[WebExecutiveCandidate]:
        if not self._source_is_trusted(
            source_url=source_url,
            preferred_domain=domain,
            include_company_website=include_company_website,
        ):
            return []

        compact = " ".join((text or "").split())
        if not compact:
            return []

        business_tokens = self._business_tokens(business_name)
        if not self._has_business_signal(compact, business_tokens, domain):
            return []

        found: dict[tuple[str, str], WebExecutiveCandidate] = {}

        for match in NAME_NEAR_TITLE_RE.finditer(compact):
            name = self._normalize_name(match.group("name"))
            title = self._normalize_title(match.group("title"))
            if not name or not title:
                continue
            if not self._context_mentions_business(compact, match.start(), match.end(), business_tokens, domain):
                continue

            confidence = source_weight + self._title_rank(title) * 0.04
            candidate = WebExecutiveCandidate(
                name=name,
                title=title,
                source_url=source_url,
                confidence=min(1.0, confidence),
            )
            self._merge_candidate(found, candidate)

        for match in TITLE_NEAR_NAME_RE.finditer(compact):
            name = self._normalize_name(match.group("name"))
            title = self._normalize_title(match.group("title"))
            if not name or not title:
                continue
            if not self._context_mentions_business(compact, match.start(), match.end(), business_tokens, domain):
                continue

            confidence = source_weight + self._title_rank(title) * 0.04
            candidate = WebExecutiveCandidate(
                name=name,
                title=title,
                source_url=source_url,
                confidence=min(1.0, confidence),
            )
            self._merge_candidate(found, candidate)

        for match in FOUNDED_BY_RE.finditer(compact):
            name = self._normalize_name(match.group("name"))
            if not name:
                continue
            if not self._context_mentions_business(compact, match.start(), match.end(), business_tokens, domain):
                continue

            confidence = source_weight + 0.22
            candidate = WebExecutiveCandidate(
                name=name,
                title="Founder",
                source_url=source_url,
                confidence=min(1.0, confidence),
            )
            self._merge_candidate(found, candidate)

        return list(found.values())

    @staticmethod
    def _merge_candidate(
        bucket: dict[tuple[str, str], WebExecutiveCandidate],
        candidate: WebExecutiveCandidate,
    ) -> None:
        key = (candidate.name.lower(), candidate.title.lower())
        current = bucket.get(key)
        if not current:
            bucket[key] = candidate
            return

        if candidate.confidence > current.confidence:
            current.confidence = candidate.confidence
            current.source_url = candidate.source_url

        if candidate.email and not current.email:
            current.email = candidate.email
        if candidate.phone and not current.phone:
            current.phone = candidate.phone

    def _extract_emails(self, text: str, html: str, preferred_domain: str | None) -> list[str]:
        found: list[str] = []

        for raw in EMAIL_RE.findall(f"{text} {html}"):
            normalized = self._normalize_email(raw)
            if not normalized:
                continue

            local, email_domain = normalized.split("@", 1)
            if local in GENERIC_EMAIL_LOCALS:
                continue

            if preferred_domain and self._normalize_host(email_domain) != self._normalize_host(preferred_domain):
                continue

            if normalized not in found:
                found.append(normalized)

        return found

    def _extract_phones(self, text: str) -> list[str]:
        region = self.settings.phone_default_region.upper()
        phones: list[str] = []
        seen: set[str] = set()

        for match in phonenumbers.PhoneNumberMatcher(text, region):
            number = match.number
            if not phonenumbers.is_possible_number(number):
                continue

            if not phonenumbers.is_valid_number(number):
                continue

            formatted = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
            if formatted in seen:
                continue
            seen.add(formatted)
            phones.append(formatted)

        return phones

    def _pick_best_email_for_candidate(
        self,
        owner_name: str,
        emails: list[str],
    ) -> tuple[str | None, float]:
        if not emails:
            return None, 0.0

        best_email: str | None = None
        best_score = -1.0

        for email in emails:
            local = email.split("@", 1)[0]
            score = 0.15

            if self._email_local_matches_name(local, owner_name):
                score += 0.65

            if re.fullmatch(r"[a-z]+[._-][a-z]+", local):
                score += 0.08

            if score > best_score:
                best_email = email
                best_score = score

        if not best_email or best_score < 0.65:
            return None, 0.0

        return best_email, min(0.22, best_score * 0.25)

    @staticmethod
    def _build_query(business_name: str, domain: str | None) -> str:
        query = f'"{business_name}" founder co-founder CEO'
        if domain:
            query = f'{query} "{domain}"'
        return query

    @staticmethod
    def _decode_search_result_url(raw_href: str) -> str | None:
        if not raw_href:
            return None

        href = raw_href.strip()
        if not href.startswith(("http://", "https://")):
            return None

        parsed = urlparse(href)
        host = parsed.netloc.lower()

        if host.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            params = parse_qs(parsed.query)
            target = (params.get("uddg") or [""])[0]
            target = unquote(target)
            return target or None

        if host.endswith("bing.com") and parsed.path.startswith("/ck/"):
            params = parse_qs(parsed.query)
            encoded = (params.get("u") or [""])[0]
            decoded = WebExecutiveSearchProvider._decode_bing_u_param(encoded)
            if decoded:
                return decoded

        return href

    @staticmethod
    def _decode_bing_u_param(encoded: str) -> str | None:
        value = (encoded or "").strip()
        if not value:
            return None

        if value.startswith("a1"):
            value = value[2:]

        if not value:
            return None

        padding = "=" * (-len(value) % 4)
        token = value + padding

        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                decoded = decoder(token.encode("ascii")).decode("utf-8", errors="ignore")
            except Exception:
                continue

            if decoded.startswith(("http://", "https://")):
                return decoded

        return None

    @staticmethod
    def _is_search_engine_url(url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host in SEARCH_HOST_BLOCKLIST

    @staticmethod
    def _source_is_trusted(
        source_url: str,
        preferred_domain: str | None,
        include_company_website: bool = True,
    ) -> bool:
        host = WebExecutiveSearchProvider._normalize_host(urlparse(source_url).netloc)
        if not host:
            return False

        if preferred_domain and include_company_website:
            normalized_domain = WebExecutiveSearchProvider._normalize_host(preferred_domain)
            if host == normalized_domain or host.endswith(f".{normalized_domain}"):
                return True

        return any(host == suffix or host.endswith(f".{suffix}") for suffix in TRUSTED_EXEC_HOST_SUFFIXES)

    @staticmethod
    def _normalize_name(raw_name: str | None) -> str | None:
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
        if any(token in NAME_BLOCKLIST for token in lowered):
            return None

        if lowered[0] in NAME_BLOCKLIST or lowered[-1] in NAME_BLOCKLIST:
            return None

        if any(len(token) < 2 for token in tokens):
            return None

        return " ".join(token.capitalize() for token in tokens)

    @staticmethod
    def _normalize_title(raw_title: str | None) -> str | None:
        if not raw_title:
            return None

        text = raw_title.lower()
        if "co-founder" in text or "cofounder" in text:
            return "Co-Founder"
        if "founder" in text:
            return "Founder"
        if "chief executive officer" in text or "chief executive" in text or "ceo" in text:
            return "CEO"
        return None

    @staticmethod
    def _title_rank(title: str) -> float:
        lowered = title.lower()
        if "founder" in lowered and "co" not in lowered:
            return 1.0
        if "co-founder" in lowered:
            return 0.95
        if "ceo" in lowered:
            return 0.92
        return 0.8

    @staticmethod
    def _business_tokens(business_name: str) -> list[str]:
        tokens = [
            token
            for token in re.split(r"[^a-z0-9]+", business_name.lower())
            if len(token) >= 3 and token not in TOKEN_STOPWORDS
        ]
        return tokens[:6]

    def _has_business_signal(self, text: str, business_tokens: list[str], domain: str | None) -> bool:
        lowered = text.lower()
        if domain and self._domain_label(domain) in lowered:
            return True
        return any(token in lowered for token in business_tokens)

    def _context_mentions_business(
        self,
        text: str,
        start: int,
        end: int,
        business_tokens: list[str],
        domain: str | None,
    ) -> bool:
        lower_text = text.lower()
        left = max(0, start - 180)
        right = min(len(lower_text), end + 180)
        snippet = lower_text[left:right]

        if domain and self._domain_label(domain) in snippet:
            return True
        return any(token in snippet for token in business_tokens)

    @staticmethod
    def _domain_label(domain: str) -> str:
        normalized = WebExecutiveSearchProvider._normalize_host(domain)
        parts = [part for part in normalized.split(".") if part]
        if not parts:
            return normalized
        if len(parts) == 1:
            return parts[0]
        return parts[-2]

    @staticmethod
    def _normalize_email(raw_value: str | None) -> str | None:
        if not raw_value:
            return None

        email = raw_value.strip().lower()
        email = email.strip(" <>[]{}()\"'.,;:")
        if not email or not EMAIL_STRICT_RE.match(email):
            return None

        if len(email.split("@", 1)[0]) < 2:
            return None

        return email

    @staticmethod
    def _email_local_matches_name(local_part: str, owner_name: str) -> bool:
        local = re.sub(r"[^a-z0-9]", "", local_part.lower())
        if not local:
            return False

        tokens = [token.lower() for token in re.sub(r"[^A-Za-z\s]", " ", owner_name).split() if token]
        if len(tokens) < 2:
            return False

        first = tokens[0]
        last = tokens[-1]

        if first in local and last in local:
            return True

        if f"{first[0]}{last}" in local:
            return True

        if f"{first}{last[0]}" in local:
            return True

        return False

    @staticmethod
    def _normalize_host(host: str) -> str:
        normalized = host.strip().lower()
        if normalized.startswith("www."):
            normalized = normalized[4:]
        return normalized
