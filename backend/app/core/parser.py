"""Filename parser: turns a release name into title, season, episode and media hints."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any

RESOLUTION_TOKENS = {
    "480p", "576p", "720p", "1080p", "1440p", "2160p", "4320p", "4k", "8k", "uhd", "hd", "sd", "fhd", "qhd",
}
SOURCE_TOKENS = {
    "web", "webx", "webdl", "web-dl", "webrip", "webhd", "bluray", "blu-ray", "bdrip", "brrip", "bdremux",
    "bdrip", "hdtv", "pdtv", "dvdrip", "dvd", "dvd5", "dvd9", "remux", "hdrip", "cam", "ts", "tc", "amzn",
    "nf", "netflix", "dsnp", "atvp", "hmax", "crunchyroll", "cr", "funi", "wakanim", "itunes",
}
CODEC_TOKENS = {
    "x264", "x265", "h264", "h265", "h-264", "h-265", "hevc", "avc", "xvid", "divx", "vp9", "av1",
    "10bit", "8bit", "hi10p", "hi10", "hdr", "hdr10", "hdr10plus", "dv", "dolbyvision", "sdr",
}
AUDIO_TOKENS = {
    "aac", "aac2", "ac3", "eac3", "dd", "dd5", "ddp", "ddp5", "dts", "dtshd", "dts-hd", "truehd", "atmos",
    "flac", "mp3", "opus", "2ch", "6ch", "5", "1", "7", "commentary",
}
LANGUAGE_TOKENS = {
    "german", "germandub", "germansub", "gersub", "gerdub", "ger", "deutsch", "dl", "dual", "multi",
    "english", "eng", "en", "japanese", "jap", "jpn", "italian", "french", "spanish", "korean",
    "dubbed", "dub", "subbed", "sub", "subs", "omu", "vostfr", "synced",
}
EDITION_TOKENS = {
    "proper", "repack", "rerip", "internal", "limited", "complete", "uncut", "unrated", "extended",
    "remastered", "imax", "directors", "director", "cut", "theatrical", "final", "readnfo", "retail",
    "custom", "hybrid", "untouched", "ws", "fs", "tvshow", "serie", "series", "staffel", "season",
}
TECH_TOKENS = (
    RESOLUTION_TOKENS | SOURCE_TOKENS | CODEC_TOKENS | AUDIO_TOKENS | LANGUAGE_TOKENS | EDITION_TOKENS
)

# Known scene / fansub groups that show up without a leading dash.
GROUP_HINTS = {
    "rarbg", "sparks", "evo", "ntb", "ggez", "tigole", "qxr", "psa", "yts", "yify", "ethel", "flux",
    "horriblesubs", "subsplease", "erai-raws", "judas", "cleo", "anixnet", "commie", "nandesu",
}

ANIME_HINTS = {
    "ova", "oav", "oad", "ona", "anime", "subsplease", "horriblesubs", "erai-raws", "judas",
    "gersub", "gerdub", "omu", "raw", "bd", "vostfr",
}

SPECIAL_WORDS = {
    "ova": "ova",
    "oav": "ova",
    "oad": "ova",
    "ona": "ona",
    "special": "special",
    "specials": "special",
    "sp": "special",
    "extra": "special",
    "omake": "special",
    "recap": "special",
    "movie": "movie",
    "film": "movie",
}

_MULTI_E = re.compile(r"(?i)s(?P<s>\d{1,3})[\s._-]*e(?P<e1>\d{1,4})[\s._-]*(?:-\s*)?e(?P<e2>\d{1,4})(?!\d)")
_MULTI_DASH = re.compile(r"(?i)s(?P<s>\d{1,3})[\s._-]*e(?P<e1>\d{1,4})\s*-\s*(?P<e2>\d{1,4})(?!\d)")
_STANDARD = re.compile(r"(?i)s(?P<s>\d{1,3})[\s._-]*e(?P<e1>\d{1,4})(?!\d)")
_CROSS = re.compile(r"(?i)(?<![\d\w])(?P<s>\d{1,2})x(?P<e1>\d{1,3})(?:\s*-\s*(?P<e2>\d{1,3}))?(?!\d)")
_WORDY = re.compile(
    r"(?i)(?:season|staffel)[\s._-]*(?P<s>\d{1,3})[\s._-]*(?:episode|folge|ep|e)[\s._-]*(?P<e1>\d{1,4})(?!\d)"
)
_EPISODE_ONLY = re.compile(r"(?i)(?<![a-z0-9])(?:episode|folge|ep|e)[\s._-]*(?P<e1>\d{1,4})(?!\d)")
_SEASON_ONLY = re.compile(r"(?i)(?<![a-z0-9])(?:season|staffel|s)[\s._-]*(?P<s>\d{1,2})(?!\d)")
_ABSOLUTE_DASH = re.compile(r"(?:^|\s)-\s*(?P<e1>\d{1,4})(?:v\d)?(?=\s|$)")
_SPECIAL_NUM = re.compile(r"(?i)(?<![a-z0-9])(?:ova|oav|oad|ona|sp|special)[\s._-]*(?P<e1>\d{1,3})(?!\d)")
_YEAR_BRACKET = re.compile(r"[\(\[](?P<y>19\d{2}|20\d{2})[\)\]]")
_YEAR_BARE = re.compile(r"(?<![\d])(?P<y>19\d{2}|20\d{2})(?![\dp])")
_GROUP_LEADING = re.compile(r"^[\[\{\(](?P<g>[^\]\}\)]{1,40})[\]\}\)]\s*")
_GROUP_TRAILING = re.compile(r"-(?P<g>[A-Za-z0-9]{2,20})$")
_BRACKETS = re.compile(r"[\[\{\(][^\]\}\)]*[\]\}\)]")


@dataclass
class ParseResult:
    raw: str
    title: str = ""
    title_key: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    absolute_episode: int | None = None
    special_kind: str | None = None
    media_hint: str = "unknown"
    pattern: str = "none"
    group: str | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: list[str] = field(default_factory=list)
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def title_key(value: str) -> str:
    """Normalized key used to compare titles across sources."""
    value = _strip_accents(value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"['’`]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(the|a|an|der|die|das|le|la|les)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_separators(value: str) -> str:
    value = value.replace("_", " ").replace("+", " ")
    # Dots are separators unless they sit between single digits (5.1 audio) or in a decimal title.
    value = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" -")


def _collect_tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[\s\-\[\]\(\)]+", text) if token]


def _detect_tech(tokens: list[str]) -> tuple[str | None, str | None, str | None, list[str]]:
    resolution = source = codec = None
    languages: list[str] = []
    for token in tokens:
        low = token.lower()
        if resolution is None and low in RESOLUTION_TOKENS:
            resolution = low
        if source is None and low in SOURCE_TOKENS:
            source = low
        if codec is None and low in CODEC_TOKENS:
            codec = low
        if low in LANGUAGE_TOKENS and low not in {"dl", "sub", "subs", "dub"}:
            if low not in languages:
                languages.append(low)
    return resolution, source, codec, languages


def _clean_title_tokens(fragment: str) -> str:
    fragment = _BRACKETS.sub(" ", fragment)
    fragment = _normalize_separators(fragment)
    tokens = _collect_tokens(fragment)
    kept: list[str] = []
    for token in tokens:
        low = token.lower().strip("-.")
        if not low:
            continue
        if low in TECH_TOKENS or low in GROUP_HINTS:
            continue
        if re.fullmatch(r"(19|20)\d{2}", low):
            continue
        if re.fullmatch(r"\d{3,4}[pi]", low):
            continue
        kept.append(token.strip("-."))
    title = " ".join(part for part in kept if part)
    title = re.sub(r"\s{2,}", " ", title).strip(" -,.")
    if title and title.islower():
        title = title.title()
    return title


def _extract_year(text: str) -> tuple[int | None, str]:
    match = _YEAR_BRACKET.search(text)
    if match:
        return int(match.group("y")), text[: match.start()] + " " + text[match.end():]
    match = _YEAR_BARE.search(text)
    if match:
        return int(match.group("y")), text[: match.start()] + " " + text[match.end():]
    return None, text


def _guess_media(raw_lower: str, result: ParseResult, path_hint: str | None) -> str:
    hint = (path_hint or "").lower()
    if any(word in hint for word in ("anime", "animes")):
        return "anime"
    if any(word in raw_lower for word in ANIME_HINTS):
        return "anime"
    if result.absolute_episode is not None and result.season is None:
        return "anime"
    if result.episode is not None or result.season is not None:
        return "series"
    if result.year is not None:
        return "movie"
    return "unknown"


def parse(name: str, folder_name: str | None = None, path_hint: str | None = None) -> ParseResult:
    """Parse a release name. `folder_name` is used when the filename carries no title."""
    raw = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name.strip())
    result = ParseResult(raw=name)

    working = raw
    group_match = _GROUP_LEADING.match(working)
    if group_match:
        candidate = group_match.group("g")
        if not re.fullmatch(r"(19|20)\d{2}", candidate):
            result.group = candidate
            working = working[group_match.end():]

    normalized = _normalize_separators(working)

    trailing = _GROUP_TRAILING.search(normalized)
    if (
        trailing
        and trailing.group("g").lower() not in TECH_TOKENS
        and not re.fullmatch(r"(?i)(e\d{1,4}|\d{1,4}|\d{3,4}[pi])", trailing.group("g"))
    ):
        if result.group is None:
            result.group = trailing.group("g")
        normalized = normalized[: trailing.start()]

    tokens = _collect_tokens(normalized)
    result.resolution, result.source, result.codec, result.languages = _detect_tech(tokens)

    cut_at: int | None = None
    for pattern, name_of in (
        (_MULTI_E, "SxxExxExx"),
        (_MULTI_DASH, "SxxExx-xx"),
        (_WORDY, "Season x Episode y"),
        (_STANDARD, "SxxExx"),
        (_CROSS, "x-notation"),
    ):
        match = pattern.search(normalized)
        if match:
            result.season = int(match.group("s"))
            result.episode = int(match.group("e1"))
            if "e2" in match.groupdict() and match.group("e2"):
                result.episode_end = int(match.group("e2"))
            result.pattern = name_of
            cut_at = match.start()
            break

    if result.episode is None:
        special_match = _SPECIAL_NUM.search(normalized)
        if special_match:
            result.season = 0
            result.episode = int(special_match.group("e1"))
            result.special_kind = SPECIAL_WORDS.get(
                re.match(r"(?i)[a-z]+", special_match.group(0).strip()).group(0).lower(), "special"
            )
            result.pattern = "special+number"
            cut_at = special_match.start()

    if result.episode is None:
        episode_match = _EPISODE_ONLY.search(normalized)
        if episode_match:
            result.episode = int(episode_match.group("e1"))
            result.pattern = "episode-only"
            cut_at = episode_match.start()
            season_match = _SEASON_ONLY.search(normalized[: episode_match.start()])
            if season_match:
                result.season = int(season_match.group("s"))
                result.pattern = "season+episode"
                cut_at = min(cut_at, season_match.start())

    if result.episode is None:
        absolute_match = _ABSOLUTE_DASH.search(normalized)
        if absolute_match:
            result.absolute_episode = int(absolute_match.group("e1"))
            result.pattern = "absolute"
            cut_at = absolute_match.start()

    if result.episode is None and result.absolute_episode is None and re.fullmatch(r"\d{1,4}", normalized.strip()):
        result.episode = int(normalized.strip())
        result.pattern = "bare-number"
        cut_at = 0

    if result.episode is None and result.absolute_episode is None:
        season_match = _SEASON_ONLY.search(normalized)
        if season_match:
            result.season = int(season_match.group("s"))
            result.pattern = "season-only"
            cut_at = season_match.start()

    lowered = normalized.lower()
    if result.special_kind is None:
        for word, kind in SPECIAL_WORDS.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered) and word not in {"movie", "film"}:
                result.special_kind = kind
                if result.season is None:
                    result.season = 0
                break
    if result.season == 0 and result.special_kind is None:
        result.special_kind = "special"

    head = normalized[:cut_at] if cut_at is not None else normalized
    year, head_without_year = _extract_year(head)
    if year is None:
        year, _ = _extract_year(normalized)
    result.year = year

    title = _clean_title_tokens(head_without_year if year else head)
    if (len(title) < 3 or title.isdigit()) and folder_name:
        folder_parsed = parse(folder_name, path_hint=path_hint)
        if folder_parsed.title:
            title = folder_parsed.title
            result.year = result.year or folder_parsed.year
            result.notes.append("title taken from folder name")
            if result.season is None:
                result.season = folder_parsed.season
    result.title = title
    result.title_key = title_key(title)

    result.media_hint = _guess_media(raw.lower(), result, path_hint)
    result.score = _score(result)
    return result


def _score(result: ParseResult) -> float:
    """0..1 confidence in the parse itself (metadata scoring happens later)."""
    score = 0.0
    if result.title_key:
        score += 0.35
        if len(result.title_key) > 6:
            score += 0.05
    if result.pattern in {"SxxExx", "SxxExxExx", "SxxExx-xx", "Season x Episode y", "x-notation"}:
        score += 0.45
    elif result.pattern in {"season+episode"}:
        score += 0.35
    elif result.pattern in {"episode-only", "special+number"}:
        score += 0.2
    elif result.pattern == "absolute":
        score += 0.1
    elif result.pattern == "none" and result.year:
        score += 0.35  # looks like a movie
    if result.year:
        score += 0.1
    if result.special_kind:
        score -= 0.05
    return max(0.0, min(1.0, round(score, 3)))
