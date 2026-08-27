import pytest
from fastapi.testclient import TestClient

from app.core.scheduler import scheduler


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(scheduler, "start", lambda: None)
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_status_endpoint(client):
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert "counts" in payload
    assert "library_roots" in payload
    assert isinstance(payload["dry_run"], bool)


def test_settings_roundtrip(client):
    response = client.put("/api/settings", json={"values": {"auto_threshold": 91}})
    assert response.status_code == 200
    assert response.json()["settings"]["auto_threshold"] == 91
    assert client.get("/api/settings").json()["settings"]["auto_threshold"] == 91
    client.put("/api/settings", json={"values": {"auto_threshold": 85}})


def test_password_is_never_returned(client):
    client.put("/api/settings", json={"values": {"jd_password": "hunter2"}})
    payload = client.get("/api/settings").json()["settings"]
    assert payload["jd_password"] == "********"
    client.put("/api/settings", json={"values": {"jd_password": ""}})


def test_jobs_and_events_are_listable(client):
    assert "jobs" in client.get("/api/jobs").json()
    assert "events" in client.get("/api/events").json()
    assert client.get("/api/jobs/999999").status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_timestamps_carry_the_utc_offset(client):
    """Without an offset a browser reads a UTC timestamp as local time."""
    client.post("/api/scan")
    payload = client.get("/api/events").json()["events"]
    assert payload, "es sollte mindestens ein Ereignis geben"
    for event in payload[:5]:
        assert event["ts"].endswith("+00:00"), event["ts"]


def test_status_reports_metadata_sources(client):
    payload = client.get("/api/status").json()
    assert "metadata_sources" in payload
    assert isinstance(payload["prefer_anime"], bool)


def test_fingerprint_changes_with_the_data(client):
    """Der Ereignisstrom haengt daran, also muss er sich bei Aenderungen bewegen."""
    from app.core import notify
    from app.db import session_scope
    from app.models import Job

    with session_scope() as session:
        vorher = notify.fingerprint(session)
        session.add(Job(source_path="/tmp/fingerprint.mkv", filename="fingerprint.mkv", status="waiting"))
    with session_scope() as session:
        nachher = notify.fingerprint(session)
    assert vorher != nachher

    with session_scope() as session:
        assert notify.fingerprint(session) == nachher, "ohne Aenderung muss er gleich bleiben"


def test_stream_generator_sends_a_change(client):
    """Der Generator wird direkt geprüft, ein offener Strom im Testclient blockiert."""
    import asyncio

    from app.api import routes

    async def zwei_stuecke():
        strom = routes.stream_generator()
        try:
            erstes = await asyncio.wait_for(strom.__anext__(), timeout=10)
            zweites = await asyncio.wait_for(strom.__anext__(), timeout=10)
            return erstes, zweites
        finally:
            await strom.aclose()

    erstes, zweites = asyncio.run(zwei_stuecke())
    assert erstes.startswith("retry:")
    assert zweites.startswith("event: change")
    assert "version" in zweites


def _warte_auf_stapel(client, batch_id, timeout=10.0):
    """Der Stapel läuft in einem eigenen Thread, also kurz nachfragen bis er fertig ist."""
    import time

    ende = time.time() + timeout
    while time.time() < ende:
        antwort = client.get(f"/api/batches/{batch_id}")
        assert antwort.status_code == 200
        state = antwort.json()
        if state["finished_at"]:
            return state
        time.sleep(0.05)
    raise AssertionError(f"Stapel {batch_id} wurde nicht fertig: {state}")


def test_bulk_decision_handles_a_whole_season(client):
    """Eine ganze Staffel wird mit einem Knopf entschieden."""
    from app.db import session_scope
    from app.models import Job

    ids = []
    with session_scope() as session:
        for nummer in range(1, 5):
            job = Job(source_path=f"/tmp/bulk-{nummer}.mkv", filename=f"bulk-{nummer}.mkv",
                      status="duplicate", duplicate_of="/tmp/alt.mkv")
            session.add(job)
            session.flush()
            ids.append(job.id)

    antwort = client.post("/api/jobs/bulk", json={"action": "duplicate_discard", "ids": ids})
    assert antwort.status_code == 202
    stapel = antwort.json()
    assert stapel["total"] == 4
    assert stapel["label"] == "verworfen"

    fertig = _warte_auf_stapel(client, stapel["id"])
    assert fertig["done"] == 4, fertig["errors"]
    assert fertig["failed"] == 0

    with session_scope() as session:
        for job_id in ids:
            assert session.get(Job, job_id).status == "skipped"


def test_bulk_reports_progress_and_survives_a_missing_job(client):
    """Der Fortschritt ist abrufbar und eine gelöschte Datei kippt den Stapel nicht."""
    from app.db import session_scope
    from app.models import Job

    ids = []
    with session_scope() as session:
        for nummer in range(1, 3):
            job = Job(source_path=f"/tmp/fort-{nummer}.mkv", filename=f"fort-{nummer}.mkv",
                      status="duplicate", duplicate_of="/tmp/alt.mkv")
            session.add(job)
            session.flush()
            ids.append(job.id)

    antwort = client.post("/api/jobs/bulk", json={"action": "duplicate_discard", "ids": ids + [999999]})
    stapel = antwort.json()
    assert stapel["total"] == 2, "unbekannte Nummern fallen vorher raus"

    fertig = _warte_auf_stapel(client, stapel["id"])
    assert fertig["done"] == 2 and fertig["failed"] == 0
    assert fertig["current"] is None

    # Der Status trägt den Vorgang mit, damit die Oberfläche ihn nach dem
    # Neuladen weiter anzeigen kann.
    status = client.get("/api/status").json()
    assert status["batch"]["id"] == stapel["id"]
    assert status["transfers"] == []


def test_cancel_endpoint_answers(client):
    """Den Knopf im Dashboard gibt es, also muss der Weg dahinter stimmen."""
    from app.db import session_scope
    from app.models import Job

    with session_scope() as session:
        job = Job(source_path="/tmp/abbruch-api.mkv", filename="abbruch-api.mkv",
                  status="duplicate", duplicate_of="/tmp/alt.mkv")
        session.add(job)
        session.flush()
        job_id = job.id

    stapel = client.post("/api/jobs/bulk", json={"action": "duplicate_discard", "ids": [job_id]}).json()
    antwort = client.post(f"/api/batches/{stapel['id']}/cancel")
    assert antwort.status_code == 200
    assert antwort.json()["id"] == stapel["id"]
    _warte_auf_stapel(client, stapel["id"])

    assert client.post("/api/batches/gibtesnicht/cancel").status_code == 404


def test_bulk_rejects_an_empty_list(client):
    assert client.post("/api/jobs/bulk", json={"action": "duplicate_discard", "ids": []}).status_code == 400
    assert client.post("/api/jobs/bulk", json={"action": "duplicate_discard", "ids": [424242]}).status_code == 404
    assert client.get("/api/batches/gibtesnicht").status_code == 404
