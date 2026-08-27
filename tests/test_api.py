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
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["erledigt"] == 4 and daten["fehler"] == 0

    with session_scope() as session:
        for job_id in ids:
            assert session.get(Job, job_id).status == "skipped"
