from dataclasses import dataclass
import logging
from urllib.parse import quote

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PhoneValidation:
    valid: bool | None
    line_type: str | None
    country_code: str | None


class TwilioLookupProvider:
    base_url = "https://lookups.twilio.com/v2/PhoneNumbers"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def lookup(self, phone_number: str) -> PhoneValidation:
        if self.settings.demo_mode:
            return PhoneValidation(valid=True, line_type="mobile", country_code="US")

        if not self.settings.twilio_account_sid or not self.settings.twilio_auth_token:
            logger.warning("Twilio credentials missing. Skipping phone lookup.")
            return PhoneValidation(valid=None, line_type=None, country_code=None)

        encoded = quote(phone_number, safe="")
        url = f"{self.base_url}/{encoded}"
        params = {"Fields": "line_type_intelligence"}
        auth = httpx.BasicAuth(self.settings.twilio_account_sid, self.settings.twilio_auth_token)

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.get(url, params=params, auth=auth)

            if response.status_code == 404:
                return PhoneValidation(valid=False, line_type=None, country_code=None)

            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Twilio Lookup failed: %s", exc)
            return PhoneValidation(valid=None, line_type=None, country_code=None)

        line_info = body.get("line_type_intelligence") or {}
        return PhoneValidation(
            valid=True,
            line_type=line_info.get("type"),
            country_code=body.get("country_code"),
        )
