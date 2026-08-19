"""Orchestration: watch, parse, match, plan, move."""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..models import Event, JDPackage, Job, Rule, utcnow
from . import files, jdownloader, library, mediainfo, metadata, mover, naming
from .parser import ParseResult, parse, title_key

MANUAL_STATES = {"review", "duplicate"}
OPEN_STATES = {"waiting", "analyzing", "review", "duplicate", "ready", "planned", "moving"}


def log(session: Session, message: str, level: str = "info", source: str = "core", job_id: int | None = None) -> None:
    session.add(Event(message=message, level=level, source=source, job_id=job_id))


# ---------------------------------------------------------------- discovery


def sync_jdownloader(session: Session) -> int:
    client = jdownloader.client
    if not client.enabled:
        return 0
    packages = client.packages()
    seen: set[str] = set()
    for package in packages:
        seen.add(package.uuid)
        row = session.get(JDPackage, package.uuid)
        values = package.as_dict()
        values.pop("source", None)
        if row is None:
            session.add(JDPackage(**values, seen_at=utcnow()))
        else:
            for key, value in values.items():
                setattr(row, key, value)
            row.seen_at = utcnow()
    if seen:
        for row in session.scalars(select(JDPackage)):
            if row.uuid not in seen:
                session.delete(row)
    return len(packages)


def _package_name_for(path: Path, download_root: Path) -> str:
    try:
        relative = path.parent.relative_to(download_root)
    except ValueError:
        return path.parent.name
    return str(relative) if str(relative) not in {"", "."} else path.parent.name


def scan_downloads(session: Session) -> int:
    """Walk the download folder and register new video files."""
    root_value = config.get("download_dir")
    root = Path(root_value)
    if not root.is_dir():
        log(session, f"download folder not found: {root_value}", level="error", source="watcher")
        return 0

    known = {row for row in session.scalars(select(Job.source_path))}
    minimum = files.min_video_bytes()
    created = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if str(path) in known or not files.is_video(path) or files.is_ignored(path):
                continue
            if files.looks_incomplete(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < minimum:
                continue
            job = Job(
                source_path=str(path),
                filename=path.name,
                package_name=_package_name_for(path, root),
                size_bytes=size,
                last_size=size,
                status="waiting",
                reason="waiting for the download and the unpacking to finish",
                dry_run=bool(config.get("dry_run", True)),
            )
            session.add(job)
            session.flush()
            log(session, f"new file discovered: {path.name}", source="watcher", job_id=job.id)
            created += 1
    return created


def check_waiting(session: Session) -> int:
    """Promote files that stopped growing and are no longer being extracted."""
    promoted = 0
    needed = int(config.get("stability_checks", 2))
    for job in session.scalars(select(Job).where(Job.status == "waiting")):
        path = Path(job.source_path)
        if not path.exists():
            job.status = "failed"
            job.error = "the source file disappeared before it could be sorted"
            log(session, job.error, level="warn", job_id=job.id)
            continue
        if files.extraction_in_progress(path.parent):
            job.reason = "JDownloader is still writing or unpacking this folder"
            job.stable_checks = 0
            continue
        stable, size = files.is_stable(path, job.last_size)
        job.last_size = size
        job.size_bytes = size
        if stable:
            job.stable_checks += 1
        else:
            job.stable_checks = 0
            job.reason = "the file is still growing"
        if job.stable_checks >= needed:
            job.status = "analyzing"
            job.reason = "ready for analysis"
            promoted += 1
    return promoted


# ---------------------------------------------------------------- analysis


def _match_rule(session: Session, job: Job, parsed: ParseResult) -> Rule | None:
    rules = list(session.scalars(select(Rule).where(Rule.enabled.is_(True))))
    for rule in rules:
        if rule.match_kind == "regex":
            try:
                if re.search(rule.pattern, job.filename, re.IGNORECASE):
                    return rule
            except re.error:
                continue
        elif rule.pattern and parsed.title_key and rule.pattern == parsed.title_key:
            return rule
    return None


def _package_video_count(session: Session, job: Job) -> int:
    parent = str(Path(job.source_path).parent)
    total = 0
    for other in session.scalars(select(Job).where(Job.status.in_(OPEN_STATES))):
        if str(Path(other.source_path).parent) == parent:
            total += 1
    return total


def _pick_media_type(parsed: ParseResult, best: metadata.Candidate | None, rule: Rule | None) -> str:
    if rule:
        return rule.media_type
    if best is None:
        return parsed.media_hint
    if best.source == "anilist":
        return "anime"
    if best.media_type == "movie" and parsed.episode is None and parsed.absolute_episode is None:
        return "movie"
    return "series" if parsed.media_hint != "anime" else "anime"


def _existing_episode(directory: Path, season: int | None, episode: int | None) -> str | None:
    """Look for the same episode already sitting in the target folder."""
    if episode is None or not directory.is_dir():
        return None
    pattern = re.compile(rf"(?i)s0*{season if season is not None else 1}e0*{episode}(?!\d)")
    for entry in os.scandir(directory):
        if entry.is_file() and pattern.search(entry.name):
            return entry.path
    return None


def analyze(session: Session, job: Job) -> Job:
    path = Path(job.source_path)
    if not path.exists():
        job.status = "failed"
        job.error = "the source file disappeared"
        return job

    parsed = parse(job.filename, folder_name=path.parent.name, path_hint=str(path.parent))
    job.parse_debug = parsed.as_dict()
    job.parsed_title = parsed.title
    job.season = parsed.season
    job.episode = parsed.episode
    job.episode_end = parsed.episode_end
    job.absolute_episode = parsed.absolute_episode
    job.special_kind = parsed.special_kind
    job.dry_run = bool(config.get("dry_run", True))

    blockers: list[str] = []
    notes: list[str] = []

    rule = _match_rule(session, job, parsed)
    candidates: list[metadata.Candidate] = []
    best: metadata.Candidate | None = None

    if rule:
        rule.hits += 1
        notes.append(f"rule #{rule.id} applied ({rule.title})")
        job.title = rule.title
        job.year = rule.year or parsed.year
        job.tmdb_id = rule.tmdb_id
        job.anilist_id = rule.anilist_id
        media_type = rule.media_type
    else:
        query = parsed.title
        if not query:
            blockers.append("no title could be read from the filename")
            media_type = parsed.media_hint
        else:
            candidates, lookup_notes = metadata.lookup(query, parsed.media_hint, parsed.year)
            notes.extend(lookup_notes)
            job.candidates = [candidate.as_dict() for candidate in candidates]
            if candidates:
                best = candidates[0]
                runner_up = candidates[1] if len(candidates) > 1 else None
                if (
                    runner_up
                    and best.score >= 0.6
                    and runner_up.score >= 0.6
                    and abs(best.score - runner_up.score) < 0.05
                    and title_key(best.title) != title_key(runner_up.title)
                ):
                    blockers.append(
                        f"two titles match equally well: {best.title} ({best.year}) and "
                        f"{runner_up.title} ({runner_up.year})"
                    )
                if best.score < 0.55:
                    blockers.append(f"the best metadata match is weak: {best.title} at {int(best.score * 100)}%")
            else:
                blockers.append("no metadata was found for this title")
        media_type = _pick_media_type(parsed, best, None)
        job.title = (best.english_title or best.title) if best else parsed.title
        job.year = (best.year if best and best.year else parsed.year)
        job.tmdb_id = best.external_id if best and best.source == "tmdb" else None
        job.anilist_id = best.external_id if best and best.source == "anilist" else None

    job.media_type = media_type

    if media_type == "unknown":
        blockers.append("the media type is unclear")
    if parsed.absolute_episode is not None and parsed.season is None:
        blockers.append(f"absolute episode number {parsed.absolute_episode} needs a season")
    if media_type in {"series", "anime"} and parsed.episode is None and parsed.absolute_episode is None:
        blockers.append("no episode number in the filename (looks like a season pack)")
    if parsed.special_kind:
        blockers.append(f"special content ({parsed.special_kind}) needs a manual target")
    siblings = _package_video_count(session, job)
    if siblings > 1:
        notes.append(f"{siblings} video files are queued from this package")

    if job.tmdb_id and media_type in {"series", "anime"} and parsed.season is not None:
        plausible, message = metadata.tmdb_season_plausible(job.tmdb_id, parsed.season, parsed.episode)
        if not plausible and message:
            blockers.append(message)
        elif message:
            notes.append(message)

    # Existing folders win over the configured default path.
    titles = [t for t in [job.title, parsed.title] if t]
    if best and best.alt_titles:
        titles.extend(best.alt_titles)
    folder_match = library.find_folder(session, titles, media_type, job.year) if job.title else None
    existing_folder = None
    if rule and rule.target_dir:
        existing_folder = rule.target_dir
        notes.append(f"rule target: {rule.target_dir}")
    elif folder_match:
        existing_folder = folder_match.item.path
        notes.append(f"{folder_match.reason}: {folder_match.item.path}")
    job.existing_folder = existing_folder

    base_dir = library.default_root(media_type) or config.get("series_path")
    plan = naming.build_plan(
        media_type=media_type,
        title=job.title or parsed.title or Path(job.filename).stem,
        year=job.year,
        season=parsed.season,
        episode=parsed.episode if parsed.episode is not None else parsed.absolute_episode,
        episode_end=parsed.episode_end,
        extension=path.suffix.lower(),
        base_dir=base_dir,
        existing_folder=existing_folder,
        special_kind=parsed.special_kind,
    )
    notes.extend(plan.notes)
    job.target_root = existing_folder or base_dir
    job.target_dir = plan.directory
    job.target_path = plan.path
    job.companions = files.find_companions(path)

    duplicate = None
    if Path(plan.path).exists():
        duplicate = plan.path
    else:
        duplicate = _existing_episode(Path(plan.directory), parsed.season, parsed.episode)
    if duplicate:
        job.duplicate_of = duplicate
        job.duplicate_info = {
            "existing": mediainfo.probe(Path(duplicate)),
            "incoming": mediainfo.probe(path),
        }
        job.status = "duplicate"
        job.reason = "this episode already exists in the library"
        job.confidence = _confidence(parsed, best, folder_match, blockers)
        log(session, f"duplicate found for {job.filename}", level="warn", job_id=job.id)
        return job

    job.confidence = _confidence(parsed, best, folder_match, blockers)
    threshold = float(config.get("auto_threshold", 85))

    if blockers:
        job.status = "review"
        job.reason = "; ".join(blockers)
    elif job.confidence < threshold:
        job.status = "review"
        job.reason = f"confidence {job.confidence:.0f}% is below the threshold of {threshold:.0f}%"
    else:
        job.status = "ready"
        job.reason = "; ".join(notes) or "ready to move"
    if notes and job.status != "ready":
        job.reason = f"{job.reason} | {'; '.join(notes)}"
    return job


def _confidence(
    parsed: ParseResult,
    best: metadata.Candidate | None,
    folder_match: library.FolderMatch | None,
    blockers: list[str],
) -> float:
    """Parse quality and metadata quality carry the score, the folder index tops it up."""
    parse_part = 0.45 * parsed.score
    metadata_part = 0.55 * (best.score if best else 0.0)
    score = (parse_part + metadata_part) * 100
    if folder_match:
        score += 8 if folder_match.score >= 0.999 else folder_match.score * 5
    score -= 22 * len(blockers)
    return round(max(0.0, min(100.0, score)), 1)


# ---------------------------------------------------------------- execution


def execute(session: Session, job: Job, *, replace_existing: bool = False) -> Job:
    source = Path(job.source_path)
    target = Path(job.target_path or "")
    if not job.target_path:
        job.status = "review"
        job.reason = "no target path has been planned yet"
        return job

    if bool(config.get("dry_run", True)):
        job.status = "planned"
        job.dry_run = True
        job.reason = "dry run: nothing was moved"
        log(session, f"dry run plan: {source.name} -> {job.target_path}", job_id=job.id)
        return job

    job.status = "moving"
    job.attempts += 1
    session.flush()

    try:
        if replace_existing and target.exists():
            backup = target.with_name(target.name + ".replaced")
            os.replace(target, backup)
            try:
                result = mover.move(source, target)
            except Exception:
                os.replace(backup, target)
                raise
            backup.unlink(missing_ok=True)
        else:
            result = mover.move(source, target)
    except (mover.MoveError, OSError) as exc:
        job.status = "failed"
        job.error = str(exc)
        job.next_attempt_at = utcnow() + dt.timedelta(minutes=10)
        log(session, f"move failed for {job.filename}: {exc}", level="error", job_id=job.id)
        return job

    moved_companions: list[dict[str, str]] = []
    for companion in job.companions or []:
        companion_path = Path(companion["path"])
        if not companion_path.exists():
            continue
        new_name = naming.companion_name(target.name, companion.get("lang", ""), companion_path.suffix.lower())
        companion_target = target.parent / new_name
        try:
            if companion_target.exists():
                moved_companions.append({"path": str(companion_path), "skipped": "target exists"})
                continue
            outcome = mover.move(companion_path, companion_target)
            moved_companions.append({"path": str(companion_path), "target": outcome.target})
        except (mover.MoveError, OSError) as exc:
            moved_companions.append({"path": str(companion_path), "error": str(exc)})

    job.companions = moved_companions
    job.status = "done"
    job.error = None
    job.finished_at = utcnow()
    job.reason = f"moved via {result.method}, verified by {result.verified}"
    files.remove_empty_dirs(source.parent, Path(config.get("download_dir")))
    log(session, f"moved {source.name} -> {result.target}", job_id=job.id)
    return job


# ---------------------------------------------------------------- tick


def process_open_jobs(session: Session) -> dict[str, int]:
    stats = {"analyzed": 0, "moved": 0}
    for job in session.scalars(select(Job).where(Job.status == "analyzing")):
        analyze(session, job)
        stats["analyzed"] += 1
        session.flush()
    dry_run = bool(config.get("dry_run", True))
    for job in session.scalars(select(Job).where(Job.status.in_(["ready", "planned"]))):
        if job.status == "planned" and dry_run:
            continue
        execute(session, job)
        stats["moved"] += 1
        session.flush()
    return stats


def retry_failed(session: Session) -> int:
    now = utcnow()
    count = 0
    for job in session.scalars(select(Job).where(Job.status == "failed")):
        if job.attempts >= 3 or (job.next_attempt_at and job.next_attempt_at > now):
            continue
        if not Path(job.source_path).exists():
            continue
        job.status = "analyzing"
        job.error = None
        count += 1
    return count


def tick(session: Session) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result["jd_packages"] = sync_jdownloader(session)
    result["discovered"] = scan_downloads(session)
    result["promoted"] = check_waiting(session)
    result.update(process_open_jobs(session))
    result["retried"] = retry_failed(session)
    return result


def counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    return {status: count for status, count in rows}


# ---------------------------------------------------------------- decisions


def plan_target(session: Session, job: Job, *, force_dir: str | None = None, force_root: str | None = None) -> Job:
    """Rebuild the target path from the values currently stored on the job."""
    path = Path(job.source_path)
    media_type = job.media_type or "series"
    titles = [t for t in [job.title, job.parsed_title] if t]
    existing_folder = force_dir
    if existing_folder is None and not force_root:
        match = library.find_folder(session, titles, media_type, job.year) if titles else None
        existing_folder = match.item.path if match else None
    base_dir = force_root or library.default_root(media_type) or config.get("series_path")
    plan = naming.build_plan(
        media_type=media_type,
        title=job.title or job.parsed_title or path.stem,
        year=job.year,
        season=job.season,
        episode=job.episode if job.episode is not None else job.absolute_episode,
        episode_end=job.episode_end,
        extension=path.suffix.lower(),
        base_dir=base_dir,
        existing_folder=existing_folder,
        special_kind=job.special_kind,
    )
    job.existing_folder = existing_folder
    job.target_root = existing_folder or base_dir
    job.target_dir = plan.directory
    job.target_path = plan.path
    return job


def _save_rule(session: Session, job: Job) -> Rule | None:
    if not job.title or not job.media_type:
        return None
    pattern = title_key(job.parsed_title or job.title)
    if not pattern:
        return None
    existing = session.scalars(select(Rule).where(Rule.pattern == pattern)).first()
    if existing:
        existing.media_type = job.media_type
        existing.title = job.title
        existing.year = job.year
        existing.tmdb_id = job.tmdb_id
        existing.anilist_id = job.anilist_id
        existing.target_dir = job.existing_folder
        return existing
    rule = Rule(
        match_kind="title",
        pattern=pattern,
        media_type=job.media_type,
        title=job.title,
        year=job.year,
        tmdb_id=job.tmdb_id,
        anilist_id=job.anilist_id,
        target_dir=job.existing_folder,
    )
    session.add(rule)
    return rule


def apply_decision(session: Session, job: Job, action: str, payload: dict[str, Any] | None = None) -> Job:
    """Handle one dashboard decision. Every branch ends in a defined job state."""
    payload = payload or {}

    if action == "skip":
        job.status = "skipped"
        job.reason = payload.get("reason") or "discarded in the dashboard"
        log(session, f"skipped {job.filename}", job_id=job.id)
        return job

    if action == "retry":
        job.status = "analyzing"
        job.error = None
        job.attempts = 0
        job.next_attempt_at = None
        return job

    if action == "defer":
        job.status = "review"
        job.reason = "postponed in the dashboard"
        return job

    if action == "select_candidate":
        wanted_source = payload.get("source")
        wanted_id = payload.get("external_id")
        chosen = None
        for candidate in job.candidates or []:
            if candidate.get("source") == wanted_source and int(candidate.get("external_id", -1)) == int(wanted_id):
                chosen = candidate
                break
        if chosen is None:
            raise ValueError("candidate is not part of this job")
        job.title = chosen.get("english_title") or chosen.get("title")
        job.year = chosen.get("year") or job.year
        job.media_type = "anime" if chosen.get("source") == "anilist" else chosen.get("media_type")
        job.tmdb_id = chosen["external_id"] if chosen.get("source") == "tmdb" else None
        job.anilist_id = chosen["external_id"] if chosen.get("source") == "anilist" else None
        job.confidence = max(job.confidence, 90.0)

    if action in {"override", "select_candidate", "approve", "set_target"}:
        for field in ("media_type", "title", "season", "episode", "episode_end"):
            if payload.get(field) not in (None, ""):
                setattr(job, field, payload[field])
        if payload.get("year") not in (None, ""):
            job.year = int(payload["year"])
        if payload.get("special_kind") is not None:
            job.special_kind = payload["special_kind"] or None

        force_dir = payload.get("target_dir")
        force_root = payload.get("target_root")
        plan_target(session, job, force_dir=force_dir, force_root=force_root)
        if payload.get("filename"):
            job.target_path = str(Path(job.target_dir) / naming.sanitize(payload["filename"]))

        if payload.get("save_rule"):
            rule = _save_rule(session, job)
            if rule is not None:
                log(session, f"rule saved for {job.title}", job_id=job.id)

        if action == "approve":
            if Path(job.target_path).exists() and not payload.get("allow_overwrite"):
                job.status = "duplicate"
                job.duplicate_of = job.target_path
                job.duplicate_info = {
                    "existing": mediainfo.probe(Path(job.target_path)),
                    "incoming": mediainfo.probe(Path(job.source_path)),
                }
                job.reason = "the planned target already exists"
                return job
            job.status = "ready"
            job.reason = "approved in the dashboard"
            job.error = None
            return execute(session, job)
        job.status = "review"
        job.reason = "target updated, waiting for approval"
        return job

    if action == "duplicate_replace":
        job.status = "ready"
        job.reason = "the existing file is being replaced"
        return execute(session, job, replace_existing=True)

    if action == "duplicate_keep_both":
        target = Path(job.target_path)
        suffix = payload.get("suffix") or "alt"
        candidate = target.with_name(f"{target.stem} [{naming.sanitize(suffix)}]{target.suffix}")
        index = 2
        while candidate.exists():
            candidate = target.with_name(f"{target.stem} [{naming.sanitize(suffix)} {index}]{target.suffix}")
            index += 1
        job.target_path = str(candidate)
        job.duplicate_of = None
        job.status = "ready"
        job.reason = "kept next to the existing file"
        return execute(session, job)

    if action == "duplicate_discard":
        job.status = "skipped"
        job.reason = "new file discarded, the library copy stays"
        return job

    raise ValueError(f"unknown action: {action}")
