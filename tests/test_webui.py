"""The dashboard API: shapes and graceful degradation, no browser required."""

import pytest

fastapi = pytest.importorskip("fastapi", reason="ui extra not installed")
from fastapi.testclient import TestClient

from eve_market import webui


@pytest.fixture()
def client():
    with TestClient(webui.app, raise_server_exceptions=False) as c:
        yield c


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>eve-market</title>" in r.text


def test_status_always_answers(client):
    # Even with the database down this must not 500 — the UI's first paint
    # depends on it to say what's wrong.
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "db_ok" in body and "snapshots" in body and "dest" in body


def test_relist_without_login_is_a_state_not_an_error(client):
    r = client.get("/api/relist")
    assert r.status_code == 200
    assert r.json().get("error") in ("not_logged_in", None)


def test_unknown_region_rejected(client):
    r = client.post("/api/actions/snapshot", json={"region": "not_a_region"})
    assert r.status_code == 400


def test_job_endpoint_reports_idle(client):
    r = client.get("/api/job")
    assert r.status_code == 200
    assert r.json()["state"] in ("idle", "running", "done", "error")


def test_autorefresh_floor_enforced(client):
    # ESI's error budget is shared; sub-10-minute polling is refused.
    r = client.post("/api/actions/autorefresh", json={"minutes": 5})
    assert r.status_code == 400
    r = client.post("/api/actions/autorefresh", json={"minutes": 0})
    assert r.status_code == 200


def test_item_search_needs_two_chars(client):
    r = client.get("/api/items", params={"q": "w"})
    assert r.status_code == 200
    assert r.json()["rows"] == []
