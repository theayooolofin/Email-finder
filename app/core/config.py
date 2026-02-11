from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Lead Finder API"
    app_env: str = Field(default="dev")
    demo_mode: bool = Field(default=True)
    database_url: str = Field(default="sqlite:///./leadfinder.db")

    google_places_api_key: str | None = None
    opencorporates_api_token: str | None = None
    hunter_api_key: str | None = None
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None

    max_businesses_per_request: int = Field(default=25, ge=1, le=100)
    hunter_domain_search_limit: int = Field(default=15, ge=1, le=100)
    website_crawl_max_pages: int = Field(default=3, ge=1, le=8)
    email_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    email_require_same_domain: bool = Field(default=True)
    email_require_mx: bool = Field(default=True)
    email_reject_generic_locals: bool = Field(default=True)
    phone_default_region: str = Field(default="US", min_length=2, max_length=2)
    outbound_user_agent: str = Field(
        default="LeadFinderBot/0.2 (+https://localhost; contact=operator@local)",
        min_length=10,
        max_length=200,
    )
    request_timeout_seconds: int = Field(default=20, ge=5, le=120)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
