"""REST API for the dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_session
from ..models import Event, JDPackage, Job, LibraryItem, Rule
from ..core import jdownloader, library, mediainfo, metadata, notify, pipeline
from ..core.files import human_size
from ..core.scheduler import scheduler

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- payloads


class DecisionIn(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SettingsIn(BaseModel):
    values: dict[str, Any]


class RuleIn(BaseModel):
    match_kind: str = "title"
    pattern: str
    media_type: str
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    anilist_id: int | None = None
    target_dir: str | None = None
    enabled: bool = True


# ---------------------------------------------------------------- helpers


def iso(value: dt.datetime | None) -> str | None:
    """Timestamps are stored in UTC. Without the offset a browser reads them as local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "filename": job.filename,
        "source_path": job.source_path,
        "package_name": job.package_name,
        "size_bytes": job.size_bytes,
        "size_human": human_size(job.size_bytes or 0),
        "status": job.status,
        "reason": job.reason,
        "error": job.error,
        "media_type": job.media_type,
        "parsed_title": job.parsed_title,
        "title": job.title,
        "year": job.year,
        "season": job.season,
        "episode": job.episode,
        "episode_end": job.episode_end,
        "absolute_episode": job.absolute_episode,
        "special_kind": job.special_kind,
        "tmdb_id": job.tmdb_id,
        "anilist_id": job.anilist_id,
        "confidence": job.confidence,
        "candidates": job.candidates or [],
        "target_root": job.target_root,
        "target_dir": job.target_dir,
        "target_path": job.target_path,
        "existing_folder": job.existing_folder,
        "duplicate_of": job.duplicate_of,
        "duplicate_info": job.duplicate_info,
        "companions": job.companions or [],
        "parse_debug": job.parse_debug or {},
        "dry_run": job.dry_run,
        "attempts": job.attempts,
        "created_at": iso(job.created_at),
        "updated_at": iso(job.updated_at),
        "finished_at": iso(job.finished_at),
    }


# ---------------------------------------------------------------- status


@router.get("/status")
def status(session: Session = Depends(get_session)) -> dict[str, Any]:
    counts = pipeline.counts(session)
    download_dir = Path(str(config.get("download_dir")))
    roots = []
    for path, media_type in config.library_roots():
        target = Path(path)
        roots.append({
            "path": path,
            "media_type": media_type,
            "exists": target.is_dir(),
            "writable": target.is_dir() and os.access(target, os.W_OK),
        })
    return {
        "counts": counts,
        "open_total": sum(counts.get(state, 0) for state in pipeline.OPEN_STATES),
        "dry_run": bool(config.get("dry_run", True)),
        "auto_threshold": config.get("auto_threshold"),
        "download_dir": str(download_dir),
        "download_dir_ok": download_dir.is_dir(),
        "library_roots": roots,
        "tmdb_configured": metadata.tmdb_available(),
        "metadata_sources": metadata.source_health(),
        "prefer_anime": bool(config.get("prefer_anime", True)),
        "anilist_enabled": bool(config.get("use_anilist", True)),
        "ffprobe": mediainfo.ffprobe_available(),
        "jd": {
            "enabled": jdownloader.client.enabled,
            "connected": jdownloader.client.connected,
            "device": jdownloader.client.device_name,
            "error": jdownloader.client.last_error,
        },
        "library_index": {
            "running": library.INDEX_STATE["running"],
            "finished_at": library.INDEX_STATE["finished_at"],
            "error": library.INDEX_STATE["error"],
        },
        "scheduler": {
            "running": scheduler.running,
            "last_run": scheduler.last_run,
            "last_error": scheduler.last_error,
            "interval": config.get("scan_interval_seconds"),
        },
    }


def _fingerprint() -> str:
    from ..db import session_scope

    with session_scope() as session:
        return notify.fingerprint(session)


async def stream_generator():
    """Sendet nur bei echten Änderungen, dazwischen ein Lebenszeichen."""
    last = None
    idle = 0
    yield "retry: 5000\n\n"
    while True:
        try:
            current = await asyncio.to_thread(_fingerprint)
        except Exception:  # noqa: BLE001 - ein Aussetzer darf den Strom nicht beenden
            current = last
        if current != last:
            last = current
            idle = 0
            yield f"event: change\ndata: {json.dumps({'version': current})}\n\n"
        else:
            idle += 1
            if idle >= 15:  # hält die Verbindung durch Proxys offen
                idle = 0
                yield ": ping\n\n"
        await asyncio.sleep(1.0)


@router.get("/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events. Eine offene Verbindung, Nachricht nur bei Änderungen."""
    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/scan")
def trigger_scan() -> dict[str, Any]:
    return scheduler.run_once()


# ---------------------------------------------------------------- jobs


@router.get("/jobs")
def list_jobs(
    status: str | None = None,
    limit: int = Query(200, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(Job).order_by(desc(Job.updated_at)).limit(limit)
    if status:
        wanted = [part.strip() for part in status.split(",") if part.strip()]
        query = select(Job).where(Job.status.in_(wanted)).order_by(desc(Job.updated_at)).limit(limit)
    jobs = list(session.scalars(query))
    return {"jobs": [job_dict(job) for job in jobs]}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    events = list(
        session.scalars(select(Event).where(Event.job_id == job_id).order_by(desc(Event.ts)).limit(50))
    )
    payload = job_dict(job)
    payload["events"] = [
        {"ts": iso(event.ts), "level": event.level, "message": event.message} for event in events
    ]
    return payload


@router.post("/jobs/{job_id}/decision")
def decide(job_id: int, body: DecisionIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    try:
        pipeline.apply_decision(session, job, body.action, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.flush()
    return job_dict(job)


@router.post("/jobs/{job_id}/reanalyze")
def reanalyze(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    pipeline.analyze(session, job)
    session.flush()
    return job_dict(job)


@router.post("/jobs/bulk")
def bulk(body: DecisionIn, ids: list[int] = Query(default=[]), session: Session = Depends(get_session)):
    results = []
    for job_id in ids:
        job = session.get(Job, job_id)
        if job is None:
            continue
        try:
            pipeline.apply_decision(session, job, body.action, body.payload)
            results.append({"id": job_id, "status": job.status})
        except ValueError as exc:
            results.append({"id": job_id, "error": str(exc)})
    session.flush()
    return {"results": results}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, str]:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    session.delete(job)
    return {"status": "deleted"}


@router.post("/jobs/clear-finished")
def clear_finished(session: Session = Depends(get_session)) -> dict[str, int]:
    result = session.execute(delete(Job).where(Job.status.in_(["done", "skipped"])))
    return {"deleted": int(result.rowcount or 0)}


# ---------------------------------------------------------------- events


@router.get("/events")
def events(limit: int = Query(120, le=500), session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = list(session.scalars(select(Event).order_by(desc(Event.ts)).limit(limit)))
    return {
        "events": [
            {
                "id": row.id,
                "ts": iso(row.ts),
                "level": row.level,
                "source": row.source,
                "message": row.message,
                "job_id": row.job_id,
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------- settings


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    values = config.all_settings()
    if values.get("jd_password"):
        values = {**values, "jd_password": "********"}
    if values.get("tmdb_api_key"):
        values = {**values, "tmdb_api_key_set": True}
    return {"settings": values, "defaults": config.DEFAULTS}


@router.put("/settings")
def put_settings(body: SettingsIn) -> dict[str, Any]:
    values = dict(body.values)
    if values.get("jd_password") == "********":
        values.pop("jd_password")
    # Settings get their own transaction so the in-memory cache reflects committed data.
    updated = config.update(values)
    if any(key.startswith("jd_") for key in values):
        jdownloader.client.disconnect()
    if "tmdb_api_key" in values or "use_anilist" in values:
        metadata.clear_cache()
    return {"settings": {**updated, "jd_password": "********" if updated.get("jd_password") else ""}}


# ---------------------------------------------------------------- library


@router.get("/library")
def get_library(
    q: str | None = None,
    media_type: str | None = None,
    limit: int = Query(400, le=2000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(LibraryItem).order_by(LibraryItem.title).limit(limit)
    items = list(session.scalars(query))
    if q:
        needle = q.lower()
        items = [item for item in items if needle in item.title.lower()]
    if media_type:
        items = [item for item in items if item.media_type == media_type]
    return {
        "items": [
            {
                "id": item.id,
                "root": item.root,
                "path": item.path,
                "folder_name": item.folder_name,
                "title": item.title,
                "year": item.year,
                "media_type": item.media_type,
                "seasons": item.seasons,
                "last_added": iso(item.last_added),
                "file_count": item.file_count,
            }
            for item in items
        ]
    }


@router.post("/library/reindex")
def reindex_library(session: Session = Depends(get_session)) -> dict[str, Any]:
    stats = library.reindex(session)
    pipeline.log(session, f"Bibliothek neu eingelesen: {stats}", source="library")
    return {"roots": stats}


@router.get("/browse")
def browse(path: str | None = None) -> dict[str, Any]:
    """Folder picker for manual target selection."""
    if not path:
        entries = [root for root, _ in config.library_roots()]
        return {"path": None, "parent": None, "dirs": entries}
    target = Path(path)
    if not target.is_dir():
        raise HTTPException(400, "Kein Ordner")
    dirs = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append(entry.path)
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"path": str(target), "parent": str(target.parent), "dirs": dirs}


# ---------------------------------------------------------------- rules


@router.get("/rules")
def list_rules(session: Session = Depends(get_session)) -> dict[str, Any]:
    rules = list(session.scalars(select(Rule).order_by(desc(Rule.created_at))))
    return {
        "rules": [
            {
                "id": rule.id,
                "match_kind": rule.match_kind,
                "pattern": rule.pattern,
                "media_type": rule.media_type,
                "title": rule.title,
                "year": rule.year,
                "tmdb_id": rule.tmdb_id,
                "anilist_id": rule.anilist_id,
                "target_dir": rule.target_dir,
                "hits": rule.hits,
                "enabled": rule.enabled,
                "created_at": iso(rule.created_at),
            }
            for rule in rules
        ]
    }


@router.post("/rules")
def create_rule(body: RuleIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    rule = Rule(**body.model_dump())
    session.add(rule)
    session.flush()
    return {"id": rule.id}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, session: Session = Depends(get_session)) -> dict[str, str]:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(404, "Regel nicht gefunden")
    session.delete(rule)
    return {"status": "deleted"}


# ---------------------------------------------------------------- metadata search


@router.get("/search")
def search(q: str, media_type: str = "unknown", year: int | None = None) -> dict[str, Any]:
    candidates, notes = metadata.lookup(q, media_type, year)
    return {"candidates": [candidate.as_dict() for candidate in candidates], "notes": notes}


# ---------------------------------------------------------------- jdownloader


@router.get("/jd/packages")
def jd_packages(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = list(session.scalars(select(JDPackage).order_by(JDPackage.name)))
    return {
        "connected": jdownloader.client.connected,
        "error": jdownloader.client.last_error,
        "packages": [
            {
                "uuid": row.uuid,
                "name": row.name,
                "save_to": row.save_to,
                "state": row.state,
                "status_text": row.status_text,
                "bytes_total": row.bytes_total,
                "bytes_loaded": row.bytes_loaded,
                "progress": round(row.bytes_loaded / row.bytes_total * 100, 1) if row.bytes_total else 0.0,
                "speed": row.speed,
                "eta": row.eta,
                "finished": row.finished,
                "extracting": row.extracting,
                "failed": row.failed,
                "seen_at": iso(row.seen_at),
            }
            for row in rows
        ],
    }


@router.post("/jd/connect")
def jd_connect() -> dict[str, Any]:
    jdownloader.client.disconnect()
    ok = jdownloader.client.connect()
    return {"connected": ok, "device": jdownloader.client.device_name, "error": jdownloader.client.last_error}


@router.get("/jd/devices")
def jd_devices() -> dict[str, Any]:
    return {"devices": jdownloader.client.devices(), "error": jdownloader.client.last_error}
