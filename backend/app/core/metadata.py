"""TMDb and AniList lookups with a small in-memory cache."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any

import requests

from .. import config
from .parser import strict_title_key, title_key

TMDB_BASE = "https://api.themoviedb.org/3"
ANILIST_URL = "https://graphql.anilist.co"
JIKAN_URL = "https://api.jikan.moe/v4/anime"
TIMEOUT = 12
USER_AGENT = "episode-sorter/1.0 (+https://github.com/Nicolai14/episode-sorter-jdownloader)"
SOURCE_PENALTY = 0.08

# Which source answered last time, so the dashboard can say when one is down.
HEALTH: dict[str, dict[str, Any]] = {}


def _mark(source: str, ok: bool, error: str | None = None) -> None:
    HEALTH[source] = {"ok": ok, "error": error, "checked_at": time.time()}


def source_health() -> dict[str, dict[str, Any]]:
    return {name: dict(value) for name, value in HEALTH.items()}

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
    anime_signal: bool = False

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
    """1.0 only for a real match. "The Dark" and "Dark" stay distinguishable."""
    strict_left, strict_right = strict_title_key(left), strict_title_key(right)
    if not strict_left or not strict_right:
        return 0.0
    if strict_left == strict_right:
        return 1.0
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 0.93  # same title apart from a leading article
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
    api_key = str(config.get("tmdb_api_key") or "").strip()
    if not api_key:
        raise MetadataError("Kein TMDb-Schlüssel hinterlegt")
    headers: dict[str, str] = {}
    if api_key.startswith("eyJ"):
        # v4 read access token goes into the header, the v3 key into the query string
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params = {**params, "api_key": api_key}
    cache_key = f"tmdb:{path}:{sorted((k, v) for k, v in params.items() if k != 'api_key')}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    headers["User-Agent"] = USER_AGENT
    headers["Accept"] = "application/json"
    try:
        response = requests.get(f"{TMDB_BASE}{path}", params=params, headers=headers, timeout=TIMEOUT)
        if response.status_code == 401:
            raise MetadataError("TMDb hat den Schlüssel abgelehnt")
        response.raise_for_status()
    except Exception as exc:
        _mark("tmdb", False, str(exc)[:200])
        raise
    payload = response.json()
    _mark("tmdb", True)
    _cache_put(cache_key, payload)
    return payload


ANIME_GENRE = "Animation"


def _tmdb_signals(item_id: int, media_type: str) -> dict[str, Any]:
    """English title, anime signals and alternative titles for one TMDb entry.

    The alternative titles matter for the folder index: a German release of
    "Mushoku Tensei" has to find the folder "Mushoku-tensei ~Isekai ittara honki dasu~",
    and TMDb lists exactly that romaji title.
    """
    kind = "movie" if media_type == "movie" else "tv"
    signals: dict[str, Any] = {"english_title": None, "anime": False, "alt_titles": []}
    try:
        details = _tmdb_request(f"/{kind}/{item_id}", {"language": "en-US"})
    except Exception:
        return signals

    signals["english_title"] = details.get("title") or details.get("name")
    genres = {genre.get("name") for genre in details.get("genres") or []}
    japanese = details.get("original_language") == "ja" or "JP" in (details.get("origin_country") or [])
    signals["anime"] = bool(japanese and (ANIME_GENRE in genres or kind == "tv"))

    titles = [details.get("original_title") or details.get("original_name")]
    try:
        alternatives = _tmdb_request(f"/{kind}/{item_id}/alternative_titles", {})
        rows = alternatives.get("titles") or alternatives.get("results") or []
        titles.extend(row.get("title") for row in rows)
    except Exception:
        pass
    signals["alt_titles"] = [title for title in titles if title]
    return signals


def tmdb_search(query: str, media_type: str, year: int | None = None, enrich: int = 3) -> list[Candidate]:
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
        candidates.append(
            Candidate(
                source="tmdb",
                external_id=int(item["id"]),
                media_type="movie" if media_type == "movie" else "series",
                title=name,
                original_title=original,
                english_title=None,
                year=candidate_year,
                score=_score_candidate(query, year, [name, original or ""], candidate_year),
                overview=(item.get("overview") or "")[:400] or None,
                poster=item.get("poster_path"),
                alt_titles=[title for title in (name, original) if title],
            )
        )

    # Only the strongest entries are worth the extra requests.
    candidates.sort(key=lambda c: c.score, reverse=True)
    for candidate in candidates[:enrich]:
        signals = _tmdb_signals(candidate.external_id, media_type)
        candidate.english_title = signals.get("english_title")
        candidate.anime_signal = bool(signals.get("anime"))
        names = list(dict.fromkeys([
            *(candidate.alt_titles or []),
            *(signals.get("alt_titles") or []),
            signals.get("english_title") or "",
        ]))
        candidate.alt_titles = [name for name in names if name]
        if candidate.english_title:
            candidate.title = candidate.english_title
        candidate.score = _score_candidate(query, year, candidate.alt_titles, candidate.year)
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
        return True, f"TMDb-Prüfung übersprungen: {exc}"
    seasons = details.get("seasons") or []
    numbers = {int(entry.get("season_number", -1)) for entry in seasons}
    if season not in numbers:
        return False, f"Staffel {season} kennt TMDb nicht (bekannt: {sorted(numbers)})"
    if episode is None:
        return True, None
    for entry in seasons:
        if int(entry.get("season_number", -1)) == season:
            count = int(entry.get("episode_count") or 0)
            if count and episode > count:
                return False, f"Folge {episode} liegt über den {count} Folgen, die TMDb für Staffel {season} führt"
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
        try:
            response = requests.post(
                ANILIST_URL,
                json={"query": _ANILIST_QUERY, "variables": {"search": query}},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if response.status_code == 429:
                raise MetadataError("AniList-Limit erreicht, bitte später erneut")
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                raise MetadataError(f"AniList: {errors[0].get('message', 'Fehler')}")
        except Exception as exc:
            _mark("anilist", False, str(exc)[:200])
            raise
        _mark("anilist", True)
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
                anime_signal=True,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# ---------------------------------------------------------------- Jikan (MyAnimeList)


def jikan_search(query: str, year: int | None = None) -> list[Candidate]:
    """Fallback for anime while AniList is unavailable. Needs no key."""
    if not query or not config.get("use_jikan", True):
        return []
    cache_key = f"jikan:{query.lower()}"
    payload = _cache_get(cache_key)
    if payload is None:
        try:
            response = requests.get(
                JIKAN_URL,
                params={"q": query, "limit": 6},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if response.status_code == 429:
                raise MetadataError("Jikan-Limit erreicht, bitte später erneut")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            _mark("jikan", False, str(exc)[:200])
            raise
        _mark("jikan", True)
        _cache_put(cache_key, payload)

    candidates: list[Candidate] = []
    for item in payload.get("data", [])[:6]:
        names = [item.get("title_english"), item.get("title"), item.get("title_japanese")]
        names += [entry.get("title") for entry in item.get("titles") or []]
        names += item.get("title_synonyms") or []
        names = [name for name in dict.fromkeys(names) if name]
        candidate_year = item.get("year") or ((item.get("aired") or {}).get("prop", {}).get("from", {}) or {}).get("year")
        candidates.append(
            Candidate(
                source="jikan",
                external_id=int(item["mal_id"]),
                media_type="anime",
                title=item.get("title_english") or item.get("title") or "",
                original_title=item.get("title_japanese"),
                english_title=item.get("title_english"),
                year=candidate_year,
                score=_score_candidate(query, year, names, candidate_year),
                episodes=item.get("episodes"),
                overview=(item.get("synopsis") or "")[:400] or None,
                poster=((item.get("images") or {}).get("jpg") or {}).get("image_url"),
                alt_titles=names,
                anime_signal=True,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def lookup(title: str, media_hint: str, year: int | None = None) -> tuple[list[Candidate], list[str]]:
    """Query the sources that fit the hint and return merged, sorted candidates.

    This library is mostly anime, so anime sources are asked first for anything
    episodic. A clear series hint from the filename still wins over that leaning.
    """
    notes: list[str] = []
    results: list[Candidate] = []

    def _try(fn, *args) -> list[Candidate]:
        try:
            return fn(*args)
        except MetadataError as exc:
            notes.append(str(exc))
        except requests.RequestException as exc:
            notes.append(f"{fn.__name__} fehlgeschlagen: {exc}")
        except Exception as exc:  # noqa: BLE001 - metadata lookups must never kill the pipeline
            notes.append(f"Fehler in {fn.__name__}: {exc}")
        return []

    prefer_anime = bool(config.get("prefer_anime", True))
    episodic = media_hint in {"anime", "series", "unknown"}

    if episodic:
        anime_hits = _try(anilist_search, title, year)
        if not anime_hits:
            anime_hits = _try(jikan_search, title, year)
            if anime_hits:
                notes.append("Anime über MyAnimeList erkannt, AniList antwortet nicht")
        results += anime_hits

    if tmdb_available():
        if media_hint in {"movie", "unknown"}:
            results += _try(tmdb_search, title, "movie", year)
        if episodic:
            results += _try(tmdb_search, title, "tv", year)
    elif media_hint in {"movie", "series"}:
        notes.append("Kein TMDb-Schlüssel hinterlegt")

    # A source that does not fit the hint loses a little, so a generic title like
    # "Dark" does not jump from the German series to the anime of the same name.
    if media_hint != "movie":
        anime_wanted = media_hint == "anime" or (prefer_anime and media_hint == "unknown")
        for candidate in results:
            from_anime_source = candidate.source in {"anilist", "jikan"} or candidate.anime_signal
            if anime_wanted != from_anime_source and candidate.media_type != "movie":
                candidate.score = round(max(0.0, candidate.score - SOURCE_PENALTY), 3)

    results.sort(key=lambda c: c.score, reverse=True)
    return results[:10], notes
