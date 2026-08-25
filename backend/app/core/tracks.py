"""Lossless track pruning: drop audio and subtitle streams nobody here watches.

Keeps German and Japanese audio, German subtitles, and anything whose language is
not tagged. Video is never touched, the file is remuxed with a stream copy, so
there is no quality loss at all.
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

GERMAN = {"deu", "ger", "de", "de-de", "german", "deutsch", "gerdub", "gersub"}
JAPANESE = {"jpn", "ja", "jap", "japanese", "japanisch", "jp"}
UNTAGGED = {"", "und", "unknown", "none", "mis", "zxx"}

# Titles carry the language when the tag does not.
GERMAN_HINTS = ("german", "deutsch", "ger dub", "gerdub", "ger sub", "gersub", "ger.", "de forced")
JAPANESE_HINTS = ("japanese", "japanisch", "japan", "jap", "jpn", "original")

# Rough sizes when the container does not state a bitrate, in kbit/s.
CODEC_GUESS = {
    "aac": 160, "ac3": 384, "eac3": 320, "dts": 1509, "truehd": 3000, "flac": 900,
    "opus": 128, "mp3": 192, "vorbis": 160, "pcm_s16le": 1536, "pcm_s24le": 2304,
}


@dataclass
class Stream:
    index: int
    kind: str  # audio | subtitle | video | other
    codec: str
    language: str
    title: str
    channels: int | None
    default: bool
    forced: bool
    bitrate_kbps: float
    bitrate_measured: bool
    duration: float = 0.0

    @property
    def is_german(self) -> bool:
        if self.language in GERMAN:
            return True
        return any(hint in self.title.lower() for hint in GERMAN_HINTS)

    @property
    def is_japanese(self) -> bool:
        if self.language in JAPANESE:
            return True
        return any(hint in self.title.lower() for hint in JAPANESE_HINTS)

    @property
    def is_untagged(self) -> bool:
        return self.language in UNTAGGED and not self.title.strip()


@dataclass
class Plan:
    path: str
    size: int
    keep: list[int] = field(default_factory=list)
    drop: list[int] = field(default_factory=list)
    streams: list[Stream] = field(default_factory=list)
    saving_bytes: int = 0
    saving_estimated: bool = False
    skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def worth_doing(self) -> bool:
        return not self.skip_reason and bool(self.drop)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["streams"] = [asdict(stream) for stream in self.streams]
        return data


def _tag(stream: dict[str, Any], name: str) -> str:
    return str((stream.get("tags") or {}).get(name) or "").strip()


def _stream_duration(stream: dict[str, Any]) -> float:
    """Laufzeit einer einzelnen Spur.

    Das Matroska-Tag zuerst: bei MKV liefert das Feld duration je Spur oft nur die
    Containerlaufzeit, das Tag dagegen den echten Wert der Spur.
    """
    roh = _tag(stream, "DURATION") or _tag(stream, "DURATION-eng")
    if roh and ":" in roh:
        try:
            stunden, minuten, sekunden = roh.split(":")
            return int(stunden) * 3600 + int(minuten) * 60 + float(sekunden)
        except ValueError:
            pass
    direct = stream.get("duration")
    if direct:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    return 0.0


def _bitrate(stream: dict[str, Any], duration: float) -> tuple[float, bool]:
    """Bits per second for one stream. Matroska usually carries BPS in the tags."""
    direct = stream.get("bit_rate")
    if direct:
        return float(direct) / 1000, True
    for key in ("BPS", "BPS-eng", "BPS-en"):
        value = _tag(stream, key)
        if value.isdigit():
            return float(value) / 1000, True
    size = _tag(stream, "NUMBER_OF_BYTES") or _tag(stream, "NUMBER_OF_BYTES-eng")
    if size.isdigit() and duration > 0:
        return float(size) * 8 / duration / 1000, True
    codec = str(stream.get("codec_name") or "").lower()
    guess = CODEC_GUESS.get(codec, 128 if stream.get("codec_type") == "audio" else 10)
    channels = stream.get("channels") or 2
    if stream.get("codec_type") == "audio" and channels > 2 and codec in {"aac", "eac3", "opus"}:
        guess = guess * channels / 2
    return float(guess), False


def probe(path: Path) -> tuple[list[Stream], float, dict[str, Any]]:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, timeout=120,
    )
    payload = json.loads(result.stdout or b"{}")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    streams: list[Stream] = []
    for raw in payload.get("streams", []):
        kind = str(raw.get("codec_type") or "other")
        rate, measured = _bitrate(raw, duration)
        disposition = raw.get("disposition") or {}
        streams.append(Stream(
            index=int(raw["index"]),
            kind=kind,
            codec=str(raw.get("codec_name") or "?"),
            language=_tag(raw, "language").lower(),
            title=_tag(raw, "title"),
            channels=raw.get("channels"),
            default=bool(disposition.get("default")),
            forced=bool(disposition.get("forced")),
            bitrate_kbps=round(rate, 1),
            bitrate_measured=measured,
            duration=_stream_duration(raw),
        ))
    return streams, duration, payload.get("format") or {}


def decide(streams: list[Stream], duration: float, size: int, path: str = "") -> Plan:
    """Decide what to keep. Conservative by design: when in doubt, keep the stream.

    Pure function, no file access, so the rules can be tested on their own.
    """
    plan = Plan(path=path, size=size, streams=streams)
    if not streams:
        plan.skip_reason = "ffprobe liefert keine Spuren"
        return plan
    if duration <= 0:
        plan.skip_reason = "keine Laufzeit erkennbar"
        return plan

    audio = [s for s in streams if s.kind == "audio"]
    subtitles = [s for s in streams if s.kind == "subtitle"]
    others = [s for s in streams if s.kind not in {"audio", "subtitle"}]

    if not audio:
        plan.skip_reason = "keine Tonspur"
        return plan

    keep_audio = [s for s in audio if s.is_german or s.is_japanese or s.is_untagged]
    if not keep_audio:
        # Nothing matched, so the tagging is unusable. Hands off.
        plan.skip_reason = "keine deutsche oder japanische Tonspur erkennbar"
        return plan
    if len(keep_audio) == len(audio) and len(subtitles) == 0:
        plan.skip_reason = "nichts zu entfernen"
        return plan

    german_audio = [s for s in keep_audio if s.is_german]
    keep_subs = [s for s in subtitles if s.is_german or s.is_untagged]
    if subtitles and not keep_subs and not german_audio:
        # Japanese audio without a German subtitle would leave nothing to read.
        keep_subs = list(subtitles)
        plan.notes.append("Untertitel behalten, weil keine deutsche Tonspur vorhanden ist")

    keep = others + keep_audio + keep_subs
    drop = [s for s in streams if s not in keep]
    if not drop:
        plan.skip_reason = "nichts zu entfernen"
        return plan

    plan.keep = sorted(s.index for s in keep)
    plan.drop = sorted(s.index for s in drop)
    saving_kbps = sum(s.bitrate_kbps for s in drop)
    plan.saving_bytes = int(saving_kbps * 1000 * duration / 8)
    plan.saving_estimated = any(not s.bitrate_measured for s in drop)
    return plan


def build_plan(path: Path) -> Plan:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return Plan(path=str(path), size=0, skip_reason=f"nicht lesbar: {exc}")
    streams, duration, _fmt = probe(path)
    return decide(streams, duration, size, str(path))


def describe(plan: Plan) -> str:
    def label(stream: Stream) -> str:
        parts = [stream.kind[:3], stream.codec, stream.language or "ohne Sprache"]
        if stream.title:
            parts.append(f'"{stream.title[:24]}"')
        if stream.forced:
            parts.append("forced")
        return " ".join(parts)

    lines = [f"{Path(plan.path).name}  {plan.size / 1024**3:.2f} GB"]
    if plan.skip_reason:
        lines.append(f"   übersprungen: {plan.skip_reason}")
        return "\n".join(lines)
    for stream in plan.streams:
        if stream.kind == "video":
            continue
        mark = "behalten" if stream.index in plan.keep else "ENTFERNEN"
        lines.append(f"   {mark:9} {label(stream)}  {stream.bitrate_kbps:.0f} kbit/s"
                     f"{'' if stream.bitrate_measured else ' (geschätzt)'}")
    lines.append(f"   Ersparnis: {plan.saving_bytes / 1024**2:.0f} MB"
                 f"{' (geschätzt)' if plan.saving_estimated else ''}")
    for note in plan.notes:
        lines.append(f"   Hinweis: {note}")
    return "\n".join(lines)


REMUXABLE = {".mkv", ".mp4", ".m4v"}


@dataclass
class RemuxResult:
    path: str
    ok: bool
    before: int
    after: int
    message: str = ""

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after) if self.ok else 0


def _verify(source_streams: list[Stream], expected_keep: list[int], temp: Path,
            duration: float) -> str | None:
    """Everything that has to hold before the original is replaced."""
    streams, new_duration, _fmt = probe(temp)
    if not streams:
        return "neue Datei liefert keine Spuren"
    # Die Containerlaufzeit richtet sich nach der laengsten Spur. Faellt die
    # laengste weg, etwa ein englischer Untertitel der eine Minute ueber das Video
    # hinauslief, wird die Datei rechnerisch kuerzer, ohne dass Inhalt fehlt.
    # Verglichen wird deshalb gegen die laengste Spur, die bleiben soll.
    # Nur echte Zeitspuren zaehlen. Angehaengte Schriftarten haben keine Laufzeit,
    # ffprobe schreibt ihnen aber die Containerlaufzeit zu.
    behalten = [s for s in source_streams
                if s.index in expected_keep and s.duration > 0 and s.kind in {"video", "audio", "subtitle"}]
    mindestens = max((s.duration for s in behalten), default=0.0) or duration
    toleranz = max(2.0, duration * 0.0025)
    if not (mindestens - toleranz <= new_duration <= duration + toleranz):
        return (f"Laufzeit weicht ab ({new_duration:.1f} Sekunden, erwartet zwischen "
                f"{mindestens - toleranz:.1f} und {duration + toleranz:.1f})")
    # Datenspuren zaehlen nicht mit. MP4 traegt oft eine bin_data- oder
    # Timecode-Spur, und ffmpeg legt beim Schreiben selbst eine an. Nur Bild, Ton
    # und Untertitel muessen uebereinstimmen.
    zaehlbar = {"video", "audio", "subtitle"}
    erwartet = collections.Counter(
        s.kind for s in source_streams if s.index in expected_keep and s.kind in zaehlbar)
    vorhanden = collections.Counter(s.kind for s in streams if s.kind in zaehlbar)
    if erwartet != vorhanden:
        fehlend = ", ".join(f"{art}: {erwartet[art]} statt {vorhanden[art]}"
                            for art in sorted(set(erwartet) | set(vorhanden))
                            if erwartet[art] != vorhanden[art])
        return f"Spuren stimmen nicht ({fehlend})"
    old_video = [s for s in source_streams if s.kind == "video"]
    new_video = [s for s in streams if s.kind == "video"]
    if len(old_video) != len(new_video):
        return "Videospur fehlt"
    if old_video and new_video and old_video[0].codec != new_video[0].codec:
        return f"Videocodec verändert ({new_video[0].codec} statt {old_video[0].codec})"
    return None


def remux(plan: Plan, *, dry_run: bool = True, keep_original: bool = False) -> RemuxResult:
    source = Path(plan.path)
    if plan.skip_reason or not plan.drop:
        return RemuxResult(str(source), False, plan.size, plan.size, plan.skip_reason or "nichts zu tun")
    if source.suffix.lower() not in REMUXABLE:
        return RemuxResult(str(source), False, plan.size, plan.size, f"Format {source.suffix} wird nicht angefasst")
    if dry_run:
        return RemuxResult(str(source), True, plan.size, plan.size - plan.saving_bytes, "Probelauf")

    free = shutil.disk_usage(source.parent).free
    if free < plan.size * 1.1:
        return RemuxResult(str(source), False, plan.size, plan.size, "zu wenig freier Speicher für die neue Datei")

    _streams, duration, _fmt = probe(source)
    temp = source.with_name(source.stem + ".pruning" + source.suffix)
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
    for index in plan.keep:
        command += ["-map", f"0:{index}"]
    command += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "0", str(temp)]

    try:
        result = subprocess.run(command, capture_output=True, timeout=3600)
        if result.returncode != 0:
            temp.unlink(missing_ok=True)
            return RemuxResult(str(source), False, plan.size, plan.size,
                               f"ffmpeg: {result.stderr.decode(errors='replace')[:160]}")

        problem = _verify(plan.streams, plan.keep, temp, duration)
        if problem:
            temp.unlink(missing_ok=True)
            return RemuxResult(str(source), False, plan.size, plan.size, f"Prüfung fehlgeschlagen: {problem}")

        after = temp.stat().st_size
        if after >= plan.size:
            temp.unlink(missing_ok=True)
            return RemuxResult(str(source), False, plan.size, after, "neue Datei ist nicht kleiner")

        # Keep the timestamps of file and folder, the library index reads them.
        stat = source.stat()
        folder = source.parent
        folder_stat = folder.stat()
        os.utime(temp, (stat.st_atime, stat.st_mtime))
        # Rechte nur uebernehmen, wenn das Original ueberhaupt Leserechte im Modus
        # traegt. Auf ZFS stehen viele Dateien auf Modus 000 und sind allein ueber
        # NFSv4-ACLs zugaenglich, ein chmod wuerde diese ACL neu schreiben und die
        # Datei fuer Plex und SMB unlesbar machen.
        if stat.st_mode & 0o444:
            try:
                os.chmod(temp, stat.st_mode & 0o7777)
            except OSError:
                pass

        if keep_original:
            source.replace(source.with_name(source.name + ".orig"))
        os.replace(temp, source)
        try:
            os.utime(folder, (folder_stat.st_atime, folder_stat.st_mtime))
        except OSError:
            pass
        return RemuxResult(str(source), True, plan.size, after, "fertig")
    except subprocess.TimeoutExpired:
        temp.unlink(missing_ok=True)
        return RemuxResult(str(source), False, plan.size, plan.size, "ffmpeg lief zu lange")
    except Exception as exc:  # noqa: BLE001
        temp.unlink(missing_ok=True)
        return RemuxResult(str(source), False, plan.size, plan.size, f"{type(exc).__name__}: {exc}")
