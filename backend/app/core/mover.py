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
        return [f"source is missing: {source}"]
    try:
        size = source.stat().st_size
    except OSError as exc:
        return [f"source is unreadable: {exc}"]
    if size <= 0:
        problems.append("source file is empty")
    if target.exists():
        problems.append(f"target already exists: {target}")

    parent = target.parent
    probe = parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        problems.append(f"target root does not exist: {parent}")
    elif not os.access(probe, os.W_OK):
        problems.append(f"target is not writable: {probe}")

    margin = int(config.get("free_space_margin_mb", 2048))
    free = files.free_space_mb(parent)
    needed = size / (1024 * 1024) + margin
    if free and free < needed:
        problems.append(f"not enough space: {free} MB free, {int(needed)} MB needed")
    return problems


def move(source: Path, target: Path, *, dry_run: bool = False) -> MoveResult:
    """Move one file. Cross dataset moves copy to a temp name and verify before deleting."""
    problems = preflight(source, target)
    if problems:
        raise MoveError("; ".join(problems))

    size = source.stat().st_size
    if dry_run:
        return MoveResult(str(source), str(target), "dry-run", size, "skipped")

    target.parent.mkdir(parents=True, exist_ok=True)

    if files.same_filesystem(source, target.parent):
        os.replace(source, target)
        return MoveResult(str(source), str(target), "rename", size, "same filesystem")

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
            raise MoveError(f"size mismatch after copy ({temp.stat().st_size} != {size})")
        verified = "size"
        if verify_mode == "sha256":
            if _sha256(temp) != _sha256(source):
                raise MoveError("checksum mismatch after copy")
            verified = "sha256"

        if target.exists():
            raise MoveError(f"target appeared during the copy: {target}")
        os.replace(temp, target)
    except Exception:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass
        raise

    source.unlink()
    return MoveResult(str(source), str(target), "copy+verify", size, verified)
