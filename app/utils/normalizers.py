import re
from urllib.parse import urlparse

PREFIXES = {"mr", "mrs", "ms", "dr", "sir", "madam"}


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None

    normalized = normalize_website_url(url)
    if not normalized:
        return None

    parsed = urlparse(normalized)
    host = parsed.netloc.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_website_url(url: str | None) -> str | None:
    if not url:
        return None

    cleaned = url.strip()
    if not cleaned:
        return None

    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    elif not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    if not parsed.netloc:
        return None

    scheme = parsed.scheme or "https"
    path = parsed.path or ""
    normalized = f"{scheme}://{parsed.netloc}{path}"
    return normalized.rstrip("/")


def split_first_last(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None

    cleaned = re.sub(r"[^A-Za-z\s]", " ", full_name)
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return None, None

    if tokens[0].lower() in PREFIXES and len(tokens) > 1:
        tokens = tokens[1:]

    if len(tokens) == 1:
        return tokens[0], None

    return tokens[0], tokens[-1]
