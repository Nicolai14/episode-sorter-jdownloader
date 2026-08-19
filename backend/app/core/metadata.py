"""TMDb and AniList lookups with a small in-memory cache."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any

import requests

from .. import config
from .parser import title_key

TMDB_BASE = "https://api.themoviedb.org/3"
ANILIST_URL = "https://graphql.anilist.co"
TIMEOUT = 12

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


class MetadataError(RuntimeError):
    pass


@dataclass
class Candidate:
    source: str  # tmdb | anilist
    external_id: int
    media_type: str  # anime | series | movie
    title: str
    original_title: str | None
    english_title: str | None
    year: int | None
    score: float
    episodes: int | None = None
    seasons: int | None = None
    overview: str | None = None
    poster: str | None = None
    alt_titles: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cache_get(key: str) -> Any | None:
    ttl = float(config.get("metadata_cache_hours", 72)) * 3600
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        stamp, value = entry
        if time.time() - stamp > ttl:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def similarity(left: str, right: str) -> float:
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, 0.9 - abs(len(a) - len(b)) / max(len(a), len(b), 1) * 0.2)
    return round(ratio, 3)


def _score_candidate(query: str, year: int | None, titles: list[str], candidate_year: int | None) -> float:
    best = max((similarity(query, title) for title in titles if title), default=0.0)
    score = best
    if year and candidate_year:
        delta = abs(year - candidate_year)
        if delta == 0:
            score += 0.08
        elif delta == 1:
            score += 0.02
        else:
            score -= 0.12
    return round(max(0.0, min(1.0, score)), 3)


# ---------------------------------------------------------------- TMDb


def tmdb_available() -> bool:
    return bool(config.get("tmdb_api_key"))


def _tmdb_request(path: str, params: dict[str, Any]) -> dict[str, Any]:
    api_key = config.get("tmdb_api_key")
    if not api_key:
        raise MetadataError("TMDb API key is not configured")
    params = {**params, "api_key": api_key}
    cache_key = f"tmdb:{path}:{sorted((k, v) for k, v in params.items() if k != 'api_key')}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    response = requests.get(f"{TMDB_BASE}{path}", params=params, timeout=TIMEOUT)
    if response.status_code == 401:
        raise MetadataError("TMDb rejected the API key")
    response.raise_for_status()
    payload = response.json()
    _cache_put(cache_key, payload)
    return payload


def _tmdb_english_title(item_id: int, media_type: str) -> str | None:
    """TMDb returns the localized name; fetch the English one for folder naming."""
    try:
        path = f"/{'movie' if media_type == 'movie' else 'tv'}/{item_id}"
        payload = _tmdb_request(path, {"language": "en-US"})
    except Exception:
        return None
    return payload.get("title") or payload.get("name")


def tmdb_search(query: str, media_type: str, year: int | None = None) -> list[Candidate]:
    if not query:
        return []
    path = "/search/movie" if media_type == "movie" else "/search/tv"
    params: dict[str, Any] = {
        "query": query,
        "language": config.get("tmdb_language", "de-DE"),
        "include_adult": "false",
    }
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    payload = _tmdb_request(path, params)
    candidates: list[Candidate] = []
    for item in payload.get("results", [])[:6]:
        if media_type == "movie":
            name = item.get("title") or ""
            original = item.get("original_title")
            date = item.get("release_date") or ""
        else:
            name = item.get("name") or ""
            original = item.get("original_name")
            date = item.get("first_air_date") or ""
        candidate_year = int(date[:4]) if date[:4].isdigit() else None
        english = _tmdb_english_title(int(item["id"]), media_type)
        titles = [name, original or "", english or ""]
        candidates.append(
            Candidate(
                source="tmdb",
                external_id=int(item["id"]),
                media_type="movie" if media_type == "movie" else "series",
                title=english or name,
                original_title=original,
                english_title=english,
                year=candidate_year,
                score=_score_candidate(query, year, titles, candidate_year),
                overview=(item.get("overview") or "")[:400] or None,
                poster=item.get("poster_path"),
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def tmdb_details(tmdb_id: int, media_type: str) -> dict[str, Any]:
    path = f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
    return _tmdb_request(path, {"language": "en-US"})


def tmdb_season_plausible(tmdb_id: int, season: int, episode: int | None) -> tuple[bool, str | None]:
    """Check the parsed season and episode against TMDb."""
    try:
        details = tmdb_details(tmdb_id, "series")
    except Exception as exc:  # network or key issues must not block the pipeline
        return True, f"TMDb check skipped: {exc}"
    seasons = details.get("seasons") or []
    numbers = {int(entry.get("season_number", -1)) for entry in seasons}
    if season not in numbers:
        return False, f"season {season} is unknown on TMDb (known: {sorted(numbers)})"
    if episode is None:
        return True, None
    for entry in seasons:
        if int(entry.get("season_number", -1)) == season:
            count = int(entry.get("episode_count") or 0)
            if count and episode > count:
                return False, f"episode {episode} is above the {count} episodes TMDb lists for season {season}"
    return True, None


# ---------------------------------------------------------------- AniList

_ANILIST_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 8) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      episodes
      format
      seasonYear
      startDate { year }
      synonyms
      description(asHtml: false)
      title { romaji english native }
      coverImage { medium }
    }
  }
}
"""


def anilist_search(query: str, year: int | None = None) -> list[Candidate]:
    if not query or not config.get("use_anilist", True):
        return []
    cache_key = f"anilist:{query.lower()}"
    payload = _cache_get(cache_key)
    if payload is None:
        response = requests.post(
            ANILIST_URL,
            json={"query": _ANILIST_QUERY, "variables": {"search": query}},
            timeout=TIMEOUT,
        )
        if response.status_code == 429:
            raise MetadataError("AniList rate limit reached")
        response.raise_for_status()
        payload = response.json()
        _cache_put(cache_key, payload)
    media = (payload.get("data") or {}).get("Page", {}).get("media", []) or []
    candidates: list[Candidate] = []
    for item in media:
        titles = item.get("title") or {}
        synonyms = item.get("synonyms") or []
        candidate_year = item.get("seasonYear") or (item.get("startDate") or {}).get("year")
        names = [titles.get("english"), titles.get("romaji"), titles.get("native"), *synonyms]
        candidates.append(
            Candidate(
                source="anilist",
                external_id=int(item["id"]),
                media_type="anime",
                title=titles.get("english") or titles.get("romaji") or "",
                original_title=titles.get("native"),
                english_title=titles.get("english"),
                year=candidate_year,
                score=_score_candidate(query, year, [n for n in names if n], candidate_year),
                episodes=item.get("episodes"),
                overview=(item.get("description") or "")[:400] or None,
                poster=(item.get("coverImage") or {}).get("medium"),
                alt_titles=[n for n in names if n],
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def lookup(title: str, media_hint: str, year: int | None = None) -> tuple[list[Candidate], list[str]]:
    """Query the sources that fit the hint and return merged, sorted candidates."""
    notes: list[str] = []
    results: list[Candidate] = []

    def _try(fn, *args) -> list[Candidate]:
        try:
            return fn(*args)
        except MetadataError as exc:
            notes.append(str(exc))
        except requests.RequestException as exc:
            notes.append(f"{fn.__name__} failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - metadata lookups must never kill the pipeline
            notes.append(f"{fn.__name__} error: {exc}")
        return []

    if media_hint in {"anime", "unknown", "series"}:
        results += _try(anilist_search, title, year)
    if tmdb_available():
        if media_hint in {"movie", "unknown"}:
            results += _try(tmdb_search, title, "movie", year)
        if media_hint in {"series", "anime", "unknown"}:
            results += _try(tmdb_search, title, "tv", year)
    elif media_hint in {"movie", "series"}:
        notes.append("TMDb API key missing")

    results.sort(key=lambda c: c.score, reverse=True)
    return results[:10], notes
