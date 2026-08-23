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
