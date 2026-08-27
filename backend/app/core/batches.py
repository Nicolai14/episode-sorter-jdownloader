"""Sammelentscheidungen laufen im Hintergrund.

Eine ganze Staffel zu ersetzen kopiert schnell mehrere Gigabyte. Liefe das in
der HTTP-Anfrage, sähe das Dashboard minutenlang nichts und der Browser gäbe
irgendwann auf. Stattdessen arbeitet ein Thread die Liste ab und schreibt den
Fortschritt hierher, wo das Dashboard ihn abholen kann.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..db import session_scope
from ..models import Job
from . import pipeline

# So viele abgeschlossene Stapel bleiben abrufbar, damit das Ergebnis nach dem
# Neuladen der Seite noch sichtbar ist.
HISTORY = 8

# Wie lange ein fertiger Stapel noch als aktuell gilt und angezeigt wird.
GRACE_SECONDS = 25.0

_batches: dict[str, dict[str, Any]] = {}
_order: list[str] = []
_lock = threading.Lock()

# Partizip, damit "3 von 6 ersetzt" gelesen werden kann.
ACTION_LABEL = {
    "duplicate_replace": "ersetzt",
    "duplicate_discard": "verworfen",
    "duplicate_keep_both": "zusätzlich behalten",
}


def label_for(job: Job) -> str:
    """Kurzer Name für die Fortschrittsanzeige."""
    name = job.title or job.parsed_title or Path(job.source_path).name
    if job.season is not None and job.episode is not None:
        return f"{name} S{job.season:02d}E{job.episode:02d}"
    return name


def _trim() -> None:
    """Alte, abgeschlossene Stapel wegräumen. Laufende bleiben immer."""
    fertig = [key for key in _order if _batches[key].get("finished_at")]
    for key in fertig[: max(0, len(fertig) - HISTORY)]:
        _batches.pop(key, None)
        _order.remove(key)


def start(action: str, payload: dict[str, Any], ids: list[int], run_lock: threading.Lock) -> dict[str, Any]:
    """Startet den Stapel und kehrt sofort zurück."""
    batch_id = uuid.uuid4().hex[:8]
    state: dict[str, Any] = {
        "id": batch_id,
        "action": action,
        "label": ACTION_LABEL.get(action, action),
        "total": len(ids),
        "done": 0,
        "failed": 0,
        "current": None,
        "errors": [],
        "started_at": time.time(),
        "finished_at": None,
        "cancelled": False,
    }
    with _lock:
        _batches[batch_id] = state
        _order.append(batch_id)
        _trim()
    thread = threading.Thread(
        target=_work,
        args=(state, payload, list(ids), run_lock),
        name=f"episode-sorter-batch-{batch_id}",
        daemon=True,
    )
    thread.start()
    return snapshot(batch_id) or state


def _note_error(state: dict[str, Any], message: str) -> None:
    with _lock:
        state["failed"] += 1
        # Nur die ersten Fehler aufheben, sonst wächst der Eintrag unbegrenzt.
        if len(state["errors"]) < 10:
            state["errors"].append(message)


def _work(state: dict[str, Any], payload: dict[str, Any], ids: list[int], run_lock: threading.Lock) -> None:
    for job_id in ids:
        if state["cancelled"]:
            break
        name = f"Datei {job_id}"
        try:
            # Die Sperre pro Datei nehmen, nicht für den ganzen Stapel. So kommt
            # der Scheduler zwischendurch dran und hungert nicht minutenlang.
            with run_lock:
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if job is None:
                        _note_error(state, f"Datei {job_id} gibt es nicht mehr")
                        continue
                    name = label_for(job)
                    with _lock:
                        state["current"] = name
                    pipeline.apply_decision(session, job, state["action"], payload)
                    status = job.status
                    fehler = job.error
        except Exception as exc:  # noqa: BLE001 - ein Ausrutscher darf den Stapel nicht beenden
            _note_error(state, f"{name}: {exc}")
            continue

        if status == "failed":
            _note_error(state, f"{name}: {fehler or 'fehlgeschlagen'}")
        else:
            with _lock:
                state["done"] += 1

    with _lock:
        state["current"] = None
        state["finished_at"] = time.time()


def snapshot(batch_id: str) -> dict[str, Any] | None:
    with _lock:
        state = _batches.get(batch_id)
        return dict(state) if state else None


def latest(grace: float = GRACE_SECONDS) -> dict[str, Any] | None:
    """Der jüngste Stapel, der noch läuft oder gerade fertig geworden ist."""
    now = time.time()
    with _lock:
        for batch_id in reversed(_order):
            state = _batches.get(batch_id)
            if state is None:
                continue
            if state["finished_at"] is None or now - state["finished_at"] <= grace:
                return dict(state)
    return None


def cancel(batch_id: str) -> dict[str, Any] | None:
    """Bricht nach der laufenden Datei ab. Angefangenes wird nie halb liegen gelassen."""
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            return None
        if state["finished_at"] is None:
            state["cancelled"] = True
        return dict(state)


def fingerprint() -> str:
    """Grob genug, dass ein laufender Stapel den Ereignisstrom nicht flutet."""
    state = latest()
    if state is None:
        return ""
    fertig = 0 if state["finished_at"] is None else 1
    return f"{state['id']}:{state['done']}:{state['failed']}:{fertig}"


def reset() -> None:
    """Nur für Tests."""
    with _lock:
        _batches.clear()
        _order.clear()
