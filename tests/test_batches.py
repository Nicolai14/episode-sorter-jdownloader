"""Der Hintergrundlauf für Sammelentscheidungen."""
import threading
import time

import pytest

from app.core import batches, pipeline
from app.db import session_scope
from app.models import Job


@pytest.fixture(autouse=True)
def _sauber():
    batches.reset()
    yield
    batches.reset()


def _jobs(anzahl, praefix):
    ids = []
    with session_scope() as session:
        for nummer in range(anzahl):
            job = Job(source_path=f"/tmp/{praefix}-{nummer}.mkv", filename=f"{praefix}-{nummer}.mkv",
                      status="duplicate", duplicate_of="/tmp/alt.mkv")
            session.add(job)
            session.flush()
            ids.append(job.id)
    return ids


def _warte(batch_id, timeout=10.0):
    ende = time.time() + timeout
    while time.time() < ende:
        state = batches.snapshot(batch_id)
        if state and state["finished_at"]:
            return state
        time.sleep(0.02)
    raise AssertionError(f"Stapel wurde nicht fertig: {batches.snapshot(batch_id)}")


def test_a_batch_works_through_every_job():
    ids = _jobs(3, "stapel")
    state = batches.start("duplicate_discard", {}, ids, threading.Lock())
    assert state["total"] == 3

    fertig = _warte(state["id"])
    assert fertig["done"] == 3
    assert fertig["failed"] == 0
    assert fertig["current"] is None
    with session_scope() as session:
        assert all(session.get(Job, job_id).status == "skipped" for job_id in ids)


def test_one_broken_job_does_not_stop_the_rest(monkeypatch):
    ids = _jobs(3, "kaputt")
    echt = pipeline.apply_decision

    def _zweiter_faellt_um(session, job, action, payload):
        if job.id == ids[1]:
            raise RuntimeError("Platte weg")
        return echt(session, job, action, payload)

    monkeypatch.setattr(batches.pipeline, "apply_decision", _zweiter_faellt_um)
    state = batches.start("duplicate_discard", {}, ids, threading.Lock())
    fertig = _warte(state["id"])

    assert fertig["done"] == 2
    assert fertig["failed"] == 1
    assert "Platte weg" in fertig["errors"][0]
    with session_scope() as session:
        assert session.get(Job, ids[2]).status == "skipped", "die Datei danach wurde trotzdem bearbeitet"


def test_cancel_stops_after_the_running_file(monkeypatch):
    """Abbrechen lässt nie eine halb kopierte Datei zurück."""
    ids = _jobs(4, "abbruch")
    erste_laeuft = threading.Event()
    weiter = threading.Event()
    bearbeitet = []
    echt = pipeline.apply_decision

    def _langsam(session, job, action, payload):
        bearbeitet.append(job.id)
        if len(bearbeitet) == 1:
            erste_laeuft.set()
            weiter.wait(5)
        return echt(session, job, action, payload)

    monkeypatch.setattr(batches.pipeline, "apply_decision", _langsam)
    state = batches.start("duplicate_discard", {}, ids, threading.Lock())
    assert erste_laeuft.wait(5)

    batches.cancel(state["id"])
    weiter.set()
    fertig = _warte(state["id"])

    assert fertig["cancelled"] is True
    assert bearbeitet == [ids[0]], "nach dem Abbruch darf keine weitere Datei drankommen"
    assert fertig["done"] == 1
    with session_scope() as session:
        assert session.get(Job, ids[1]).status == "duplicate"


def test_the_batch_holds_the_scheduler_lock():
    """Solange eine Datei bearbeitet wird, läuft kein Scheduler-Durchlauf."""
    ids = _jobs(1, "sperre")
    lock = threading.Lock()
    lock.acquire()

    state = batches.start("duplicate_discard", {}, ids, lock)
    time.sleep(0.2)
    assert batches.snapshot(state["id"])["done"] == 0, "ohne Sperre darf nichts passieren"

    lock.release()
    fertig = _warte(state["id"])
    assert fertig["done"] == 1


def test_the_fingerprint_changes_with_the_progress():
    ids = _jobs(2, "abdruck")
    leer = batches.fingerprint()
    state = batches.start("duplicate_discard", {}, ids, threading.Lock())
    _warte(state["id"])
    assert batches.fingerprint() != leer


def test_finished_batches_are_forgotten_after_a_while():
    ids = _jobs(1, "gnade")
    state = batches.start("duplicate_discard", {}, ids, threading.Lock())
    fertig = _warte(state["id"])
    assert batches.latest() is not None
    # Kurz nach dem Ende ist der Stapel noch aktuell, später nicht mehr.
    assert batches.latest(grace=0.0) is None
    assert batches.snapshot(fertig["id"]) is not None, "abrufbar bleibt er trotzdem"
