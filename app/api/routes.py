import csv
from io import StringIO

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.repositories import list_leads, save_leads
from app.schemas import HealthResponse, LeadDiscoveryRequest, LeadDiscoveryResponse, LeadPublic
from app.services.lead_pipeline import LeadPipeline

router = APIRouter()
settings = get_settings()
pipeline = LeadPipeline(settings)
PREMIUM_LIVE_KEYS = [
    "google_places_api_key",
    "hunter_api_key",
]


def get_missing_premium_keys() -> list[str]:
    return [key for key in PREMIUM_LIVE_KEYS if not getattr(settings, key, None)]


def get_discovery_mode() -> str:
    if settings.demo_mode:
        return "demo"
    if not get_missing_premium_keys():
        return "premium"
    return "free_fallback"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    mode = get_discovery_mode()
    missing_premium = get_missing_premium_keys()
    return HealthResponse(
        status="ok",
        demo_mode=settings.demo_mode,
        live_ready=mode in {"premium", "free_fallback"},
        missing_required_keys=[],
        missing_recommended_keys=missing_premium if mode == "free_fallback" else [],
        discovery_mode=mode,
    )


@router.get("/api/v1/readiness")
async def readiness() -> dict[str, object]:
    mode = get_discovery_mode()
    missing_premium = get_missing_premium_keys()
    return {
        "demo_mode": settings.demo_mode,
        "live_ready": mode in {"premium", "free_fallback"},
        "discovery_mode": mode,
        "missing_required_keys": [],
        "missing_recommended_keys": missing_premium if mode == "free_fallback" else [],
        "premium_keys_configured": {
            "google_places_api_key": bool(settings.google_places_api_key),
            "hunter_api_key": bool(settings.hunter_api_key),
        },
        "optional_keys_configured": {
            "opencorporates_api_token": bool(settings.opencorporates_api_token),
            "twilio_account_sid": bool(settings.twilio_account_sid),
            "twilio_auth_token": bool(settings.twilio_auth_token),
        },
    }


@router.post("/api/v1/discover", response_model=LeadDiscoveryResponse)
async def discover_leads(payload: LeadDiscoveryRequest, db: Session = Depends(get_db)) -> LeadDiscoveryResponse:
    leads_to_save = await pipeline.discover(payload)
    saved = save_leads(db, leads_to_save)
    return LeadDiscoveryResponse(
        total=len(saved),
        demo_mode=settings.demo_mode,
        leads=[LeadPublic.model_validate(row) for row in saved],
    )


@router.get("/api/v1/leads", response_model=list[LeadPublic])
async def get_leads(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[LeadPublic]:
    rows = list_leads(db, limit=limit, offset=offset)
    return [LeadPublic.model_validate(row) for row in rows]


@router.get("/api/v1/leads/export.csv")
async def export_leads_csv(
    limit: int = Query(default=1000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    rows = list_leads(db, limit=limit, offset=offset)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "created_at",
            "business_name",
            "business_website",
            "business_phone",
            "city",
            "search_query",
            "owner_name",
            "owner_title",
            "email",
            "email_status",
            "email_confidence",
            "phone_status",
            "phone_type",
            "lead_score",
            "data_source_notes",
        ]
    )

    for row in rows:
        writer.writerow(
            [
                row.id,
                row.created_at.isoformat(),
                row.business_name,
                row.business_website or "",
                row.business_phone or "",
                row.city or "",
                row.search_query,
                row.owner_name or "",
                row.owner_title or "",
                row.email or "",
                row.email_status or "",
                row.email_confidence,
                row.phone_status or "",
                row.phone_type or "",
                row.lead_score,
                row.data_source_notes or "",
            ]
        )

    payload = output.getvalue()
    output.close()

    headers = {"Content-Disposition": 'attachment; filename="leads_export.csv"'}
    return StreamingResponse(iter([payload]), media_type="text/csv", headers=headers)
