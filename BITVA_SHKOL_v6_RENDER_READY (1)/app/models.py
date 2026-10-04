from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class SiteStats(Base):
    __tablename__ = "site_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    total_visits: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)




class Visitor(Base):
    __tablename__ = "visitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visitor_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

class Country(Base):
    __tablename__ = "countries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    flag: Mapped[str] = mapped_column(String(8), default="🌍")
    real_clicks: Mapped[int] = mapped_column(Integer, default=0)
    event_points: Mapped[int] = mapped_column(Integer, default=0)

    cities: Mapped[list["City"]] = relationship(back_populates="country", cascade="all, delete-orphan")


class City(Base):
    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    real_clicks: Mapped[int] = mapped_column(Integer, default=0)
    event_points: Mapped[int] = mapped_column(Integer, default=0)

    country: Mapped[Country] = relationship(back_populates="cities")
    institutions: Mapped[list["Institution"]] = relationship(back_populates="city", cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("country_id", "name", name="uq_city_country_name"),
        Index("ix_city_country_name", "country_id", "name"),
    )


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id", ondelete="CASCADE"), index=True)
    type_code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(500), index=True)
    normalized_name: Mapped[str] = mapped_column(String(500), index=True)
    real_clicks: Mapped[int] = mapped_column(Integer, default=0)
    event_points: Mapped[int] = mapped_column(Integer, default=0)

    city: Mapped[City] = relationship(back_populates="institutions")
    media: Mapped["InstitutionMedia | None"] = relationship(back_populates="institution", uselist=False, cascade="all, delete-orphan")
    photo_submissions: Mapped[list["PhotoSubmission"]] = relationship(back_populates="institution", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_inst_city_type_name", "city_id", "type_code", "name"),
        Index("ix_inst_score", "real_clicks", "event_points"),
    )

    @property
    def score(self) -> int:
        return self.real_clicks + self.event_points


class InstitutionMedia(Base):
    __tablename__ = "institution_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id", ondelete="CASCADE"), unique=True, index=True)
    desktop_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mobile_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    desktop_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mobile_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    desktop_mime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mobile_mime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    institution: Mapped[Institution] = relationship(back_populates="media")


class PhotoSubmission(Base):
    __tablename__ = "photo_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id", ondelete="CASCADE"), index=True)
    image_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(80), default="image/jpeg")
    original_name: Mapped[str] = mapped_column(String(255), default="upload.jpg")
    uploader_ip_hash: Mapped[str] = mapped_column(String(128), default="")
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    moderator_note: Mapped[str] = mapped_column(Text, default="")

    institution: Mapped[Institution] = relationship(back_populates="photo_submissions")


class AdminEvent(Base):
    __tablename__ = "admin_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), default="Admin event")
    duration_seconds: Mapped[int] = mapped_column(Integer)
    per_click: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, default="")
    audio_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    audio_mime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    ip_hash: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
