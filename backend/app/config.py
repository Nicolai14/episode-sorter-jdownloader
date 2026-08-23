"""Settings: defaults, environment seeding, database-backed overrides."""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Setting

DEFAULTS: dict[str, Any] = {
    # paths
    "download_dir": "/downloads",
    "anime_path_1": "/mnt/poolToshiba/Anime",
    "anime_path_2": "/mnt/SmallPool/dataGrepDataset/Anime",
    "series_path": "/mnt/poolToshiba/Serien",
    "series_path_2": "/mnt/SmallPool/dataGrepDataset/Serien",
    "movies_path": "/mnt/poolToshiba/Filme",
    "movies_path_2": "",
    "default_anime_path": "/mnt/SmallPool/dataGrepDataset/Anime",
    "default_series_path": "/mnt/SmallPool/dataGrepDataset/Serien",
    "default_movie_path": "/mnt/poolToshiba/Filme",
    # metadata
    "tmdb_api_key": "",
    "tmdb_language": "de-DE",
    "use_anilist": True,
    "use_jikan": True,
    # This library is mostly anime, so ambiguous cases lean that way.
    "prefer_anime": True,
    "metadata_cache_hours": 72,
    # behaviour
    "dry_run": True,
    "auto_threshold": 85,
    "scan_interval_seconds": 60,
    "stability_checks": 2,
    "min_video_size_mb": 80,
    "video_extensions": [".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"],
    "subtitle_extensions": [".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"],
    "ignored_terms": ["sample", "trailer", "proof", "screens", "rarbg", "etrg"],
    "ignored_extensions": [".exe", ".bat", ".cmd", ".lnk", ".url", ".txt", ".nfo", ".sfv", ".jpg", ".png"],
    "verify_mode": "size",  # size | sha256
    "event_retention": 5000,
    "job_retention_days": 60,
    "free_space_margin_mb": 2048,
    "delete_empty_source_dirs": True,
    "move_subtitles": True,
    # naming
    "episode_template": "{title} ({year}) - S{season:02d}E{episode:02d}",
    "episode_range_template": "{title} ({year}) - S{season:02d}E{episode:02d}-E{episode_end:02d}",
    "movie_template": "{title} ({year})",
    # The library uses S1, S2, S3 in 234 of its folders, so new folders match that.
    "season_folder_template": "S{season}",
    "specials_folder": "Specials",
    # jdownloader
    "jd_email": "",
    "jd_password": "",
    "jd_device": "",
    "jd_enabled": False,
    # JDownloader reports the path inside its own container. Everything below this
    # prefix is rewritten to the download folder Episode Sorter watches.
    "jd_path_prefix": "/output",
    "watch_folder_fallback": True,
}

ENV_MAP = {
    "download_dir": "ES_DOWNLOAD_DIR",
    "anime_path_1": "ES_ANIME_PATH_1",
    "anime_path_2": "ES_ANIME_PATH_2",
    "series_path": "ES_SERIES_PATH",
    "series_path_2": "ES_SERIES_PATH_2",
    "movies_path": "ES_MOVIES_PATH",
    "movies_path_2": "ES_MOVIES_PATH_2",
    "default_anime_path": "ES_DEFAULT_ANIME_PATH",
    "default_series_path": "ES_DEFAULT_SERIES_PATH",
    "default_movie_path": "ES_DEFAULT_MOVIE_PATH",
    "tmdb_api_key": "TMDB_API_KEY",
    "jd_email": "JD_EMAIL",
    "jd_password": "JD_PASSWORD",
    "jd_device": "JD_DEVICE",
    "dry_run": "ES_DRY_RUN",
    "auto_threshold": "ES_AUTO_THRESHOLD",
}

_lock = threading.Lock()
_cache: dict[str, Any] = {}


def _coerce(key: str, raw: str) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, list):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def bootstrap() -> None:
    """Seed missing settings from defaults, overridden by environment variables."""
    with session_scope() as session:
        stored = {row.key for row in session.scalars(select(Setting))}
        for key, default in DEFAULTS.items():
            if key in stored:
                continue
            env_name = ENV_MAP.get(key)
            value = default
            if env_name and os.environ.get(env_name):
                value = _coerce(key, os.environ[env_name])
            session.add(Setting(key=key, value=value))
    refresh()


def refresh() -> dict[str, Any]:
    with session_scope() as session:
        values = {row.key: row.value for row in session.scalars(select(Setting))}
    merged = dict(DEFAULTS)
    merged.update(values)
    with _lock:
        _cache.clear()
        _cache.update(merged)
    return merged


def all_settings() -> dict[str, Any]:
    with _lock:
        if _cache:
            return dict(_cache)
    return refresh()


def get(key: str, fallback: Any = None) -> Any:
    values = all_settings()
    if key in values:
        return values[key]
    return DEFAULTS.get(key, fallback)


def update(values: dict[str, Any], session: Session | None = None) -> dict[str, Any]:
    def _apply(sess: Session) -> None:
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            row = sess.get(Setting, key)
            if row is None:
                sess.add(Setting(key=key, value=value))
            else:
                row.value = value

    if session is not None:
        _apply(session)
        session.flush()
    else:
        with session_scope() as sess:
            _apply(sess)
    return refresh()


ROOT_KEYS: list[tuple[str, str]] = [
    ("anime_path_1", "anime"),
    ("anime_path_2", "anime"),
    ("series_path", "series"),
    ("series_path_2", "series"),
    ("movies_path", "movie"),
    ("movies_path_2", "movie"),
]


def library_roots() -> list[tuple[str, str]]:
    """(path, media_type) for every configured library root, empty ones skipped."""
    roots: list[tuple[str, str]] = []
    for key, media_type in ROOT_KEYS:
        path = get(key)
        if path:
            roots.append((str(path), media_type))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for path, media_type in roots:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, media_type))
    return unique
