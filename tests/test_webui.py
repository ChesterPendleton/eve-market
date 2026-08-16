"""The dashboard API: shapes and graceful degradation, no browser required."""

import pytest

fastapi = pytest.importorskip("fastapi", reason="ui extra not installed")
from fastapi.testclient import TestClient

from eve_market import webui


@pytest.fixture()
def client():
    # base_url and header mirror what the real dashboard sends; anything else
    # is refused by design (see the guard tests below).
    with TestClient(
        webui.app,
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        headers={"x-eve-market": "1"},
    ) as c:
        yield c


def test_foreign_host_is_refused():
    # DNS rebinding: a hostile page's domain resolving to 127.0.0.1 still
    # carries its own Host header, and that must be enough to refuse it.
    with TestClient(webui.app, base_url="http://evil.example", raise_server_exceptions=False) as c:
        assert c.get("/api/status").status_code == 403


def test_post_without_app_header_is_refused(client):
    # Cross-origin pages can send bodyless POSTs without any CORS preflight;
    # the custom header is what they cannot forge.
    r = client.post("/api/actions/sync", headers={"x-eve-market": ""})
    assert r.status_code == 403


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
