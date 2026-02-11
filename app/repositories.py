from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lead
from app.schemas import LeadCreate


def save_leads(db: Session, leads: Iterable[LeadCreate]) -> list[Lead]:
    rows: list[Lead] = []
    for lead in leads:
        row = Lead(**lead.model_dump())
        db.add(row)
        rows.append(row)

    db.commit()

    for row in rows:
        db.refresh(row)

    return rows


def list_leads(db: Session, limit: int = 50, offset: int = 0) -> list[Lead]:
    stmt = select(Lead).order_by(Lead.created_at.desc()).offset(offset).limit(limit)
    return list(db.scalars(stmt))
