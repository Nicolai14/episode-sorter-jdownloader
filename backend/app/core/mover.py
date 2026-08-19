"""Safe file moves: verify first, copy across datasets, delete the source last."""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import files

CHUNK = 4 * 1024 * 1024


class MoveError(RuntimeError):
    pass


@dataclass
class MoveResult:
    source: str
    target: str
    method: str
    bytes_moved: int
    verified: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def preflight(source: Path, target: Path) -> list[str]:
    """Everything that has to be true before a single byte is written."""
    problems: list[str] = []
    if not source.is_file():
        return [f"Quelldatei fehlt: {source}"]
    try:
        size = source.stat().st_size
    except OSError as exc:
        return [f"Quelldatei nicht lesbar: {exc}"]
    if size <= 0:
        problems.append("Quelldatei ist leer")
    if target.exists():
        problems.append(f"Zieldatei existiert bereits: {target}")

    parent = target.parent
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        problems.append(f"Zielordner existiert nicht: {parent}")
    elif not os.access(probe, os.W_OK):
        problems.append(f"Ziel ist nicht beschreibbar: {probe}")

    margin = int(config.get("free_space_margin_mb", 2048))
    free = files.free_space_mb(parent)
    needed = size / (1024 * 1024) + margin
    if free and free < needed:
        problems.append(f"Zu wenig Speicher frei: {free} MB verfügbar, {int(needed)} MB nötig")
    return problems


def move(source: Path, target: Path, *, dry_run: bool = False) -> MoveResult:
    """Move one file. Cross dataset moves copy to a temp name and verify before deleting."""
    problems = preflight(source, target)
    if problems:
        raise MoveError("; ".join(problems))

    size = source.stat().st_size
    if dry_run:
        return MoveResult(str(source), str(target), "Dry Run", size, "übersprungen")

    target.parent.mkdir(parents=True, exist_ok=True)

    if files.same_filesystem(source, target.parent):
        os.replace(source, target)
        return MoveResult(str(source), str(target), "Umbenennen", size, "gleiches Dateisystem")

    temp = target.with_name(target.name + ".es-part")
    if temp.exists():
        temp.unlink()
    try:
        with source.open("rb") as src, temp.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, temp, follow_symlinks=True)

        verify_mode = str(config.get("verify_mode", "size"))
        if temp.stat().st_size != size:
            raise MoveError(f"Größe stimmt nach dem Kopieren nicht ({temp.stat().st_size} statt {size})")
        verified = "Dateigröße"
        if verify_mode == "sha256":
            if _sha256(temp) != _sha256(source):
                raise MoveError("Prüfsumme stimmt nach dem Kopieren nicht")
            verified = "SHA-256"

        if target.exists():
            raise MoveError(f"Zieldatei ist während des Kopierens aufgetaucht: {target}")
        os.replace(temp, target)
    except Exception:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
        raise

    source.unlink()
    return MoveResult(str(source), str(target), "Kopieren und Prüfen", size, verified)
