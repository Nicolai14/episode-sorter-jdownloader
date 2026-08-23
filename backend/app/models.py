"""ORM models."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """Naive UTC, which is exactly what SQLite hands back on a read.

    Mixing this with an aware value breaks every comparison, and the API attaches
    the offset when it serialises.
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base):
    """One video file on its way from the download folder into the library."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_path: Mapped[str] = mapped_column(Text, unique=True)
    filename: Mapped[str] = mapped_column(Text)
    package_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(24), default="waiting", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parsed_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    absolute_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    special_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    candidates: Mapped[Any] = mapped_column(JSON, default=list)

    target_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    existing_folder: Mapped[str | None] = mapped_column(Text, nullable=True)

    duplicate_of: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_info: Mapped[Any] = mapped_column(JSON, nullable=True)
    companions: Mapped[Any] = mapped_column(JSON, default=list)
    parse_debug: Mapped[Any] = mapped_column(JSON, default=dict)

    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    stable_checks: Mapped[int] = mapped_column(Integer, default=0)
    last_size: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    events: Mapped[list["Event"]] = relationship(back_populates="job", cascade="all, delete-orphan")


Index("ix_jobs_status_updated", Job.status, Job.updated_at)


class Rule(Base):
    """A manual assignment saved for reuse on similar filenames."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_kind: Mapped[str] = mapped_column(String(16), default="title")  # title | regex
    pattern: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anilist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    season_offset: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class LibraryItem(Base):
    """A folder that already exists under one of the configured library roots."""

    __tablename__ = "library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    root: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, unique=True)
    folder_name: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    title_key: Mapped[str] = mapped_column(String(255), index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str] = mapped_column(String(16))
    seasons: Mapped[Any] = mapped_column(JSON, default=list)
    last_added: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class JDPackage(Base):
    """Snapshot of a JDownloader package for the dashboard."""

    __tablename__ = "jd_packages"

    uuid: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    save_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(String(48), nullable=True)
    status_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_total: Mapped[int] = mapped_column(Integer, default=0)
    bytes_loaded: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)
    extracting: Mapped[bool] = mapped_column(Boolean, default=False)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    source: Mapped[str] = mapped_column(String(24), default="core")
    message: Mapped[str] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)

    job: Mapped[Job | None] = relationship(back_populates="events")
