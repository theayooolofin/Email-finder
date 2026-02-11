from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    business_name: Mapped[str] = mapped_column(String(255), index=True)
    business_website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    business_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    search_query: Mapped[str] = mapped_column(String(255), index=True)

    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_status: Mapped[str | None] = mapped_column(String(60), nullable=True)
    email_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    phone_status: Mapped[str | None] = mapped_column(String(60), nullable=True)
    phone_type: Mapped[str | None] = mapped_column(String(80), nullable=True)

    data_source_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_score: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
