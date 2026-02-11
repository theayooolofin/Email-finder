from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeadDiscoveryRequest(BaseModel):
    niche: str | None = Field(default=None, min_length=2, max_length=80)
    city: str | None = Field(default=None, min_length=2, max_length=80)
    country: str = Field(default="US", min_length=2, max_length=56)
    max_businesses: int = Field(default=10, ge=1, le=50)
    website: str | None = Field(default=None, min_length=4, max_length=500)
    business_name: str | None = Field(default=None, min_length=2, max_length=255)

    @model_validator(mode="after")
    def validate_discovery_mode(self) -> "LeadDiscoveryRequest":
        if self.website:
            return self

        if self.niche and self.city:
            return self

        raise ValueError("Provide either website, or both niche and city.")


class LeadCreate(BaseModel):
    business_name: str
    business_website: str | None = None
    business_phone: str | None = None
    city: str | None = None
    search_query: str

    owner_name: str | None = None
    owner_title: str | None = None

    email: str | None = None
    email_status: str | None = None
    email_confidence: float = 0.0

    phone_status: str | None = None
    phone_type: str | None = None

    data_source_notes: str | None = None
    lead_score: float = 0.0


class LeadPublic(LeadCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class LeadDiscoveryResponse(BaseModel):
    total: int
    demo_mode: bool
    leads: list[LeadPublic]


class HealthResponse(BaseModel):
    status: str
    demo_mode: bool
    live_ready: bool
    missing_required_keys: list[str] = Field(default_factory=list)
    missing_recommended_keys: list[str] = Field(default_factory=list)
    discovery_mode: str = "demo"
