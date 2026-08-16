from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time

import pytest

from eve_market import auth


def make_jwt(character_id: int = 12345678, name: str = "Test Pilot", scopes=None) -> str:
    """Build an unsigned JWT shaped like CCP's access tokens."""
    payload = {"sub": f"CHARACTER:EVE:{character_id}", "name": name}
    if scopes is not None:
        payload["scp"] = scopes

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return f"{b64(b'{}')}.{b64(json.dumps(payload).encode())}.{b64(b'sig')}"


def test_pkce_challenge_is_the_sha256_of_the_verifier():
    verifier, challenge = auth._pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    # RFC 7636 requires 43-128 characters.
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge


def test_pkce_pairs_are_never_reused():
    assert auth._pkce_pair()[0] != auth._pkce_pair()[0]


def test_character_is_read_from_the_subject_claim():
    claims = auth._decode_jwt_claims(make_jwt(99, "Someone"))
    assert auth._character_from_claims(claims) == (99, "Someone")


def test_malformed_token_is_rejected():
    with pytest.raises(auth.AuthError):
        auth._decode_jwt_claims("not-a-jwt")


def test_unexpected_subject_is_rejected():
    with pytest.raises(auth.AuthError, match="unexpected subject"):
        auth._character_from_claims({"sub": "CORPORATION:EVE:oops:extra"})


def test_tokens_report_expiry_with_margin():
    fresh = auth.Tokens("a", "r", time.time() + 3600, 1, "P")
    assert not fresh.expired
    # Inside the refresh margin, a token counts as expired even though it
    # technically still has seconds left.
    edge = auth.Tokens("a", "r", time.time() + 10, 1, "P")
    assert edge.expired


def test_tokens_from_response_reads_scopes_and_character():
    payload = {
        "access_token": make_jwt(42, "Pilot", ["esi-markets.read_character_orders.v1"]),
        "refresh_token": "refresh-me",
        "expires_in": 1199,
    }
    tokens = auth._tokens_from_response(payload, [])
    assert tokens.character_id == 42
    assert tokens.character_name == "Pilot"
    assert tokens.scopes == ["esi-markets.read_character_orders.v1"]
    assert tokens.refresh_token == "refresh-me"


def test_a_single_scope_string_is_normalised_to_a_list():
    payload = {"access_token": make_jwt(scopes="esi-ui.open_window.v1"), "expires_in": 1200}
    assert auth._tokens_from_response(payload, []).scopes == ["esi-ui.open_window.v1"]


def test_tokens_round_trip_through_disk(tmp_path):
    path = tmp_path / "tokens.json"
    original = auth.Tokens("access", "refresh", time.time() + 100, 7, "Pilot", ["a"])
    auth.save(original, path)

    loaded = auth.load(path)
    assert loaded is not None
    assert loaded.refresh_token == "refresh"
    assert loaded.character_id == 7
    assert loaded.scopes == ["a"]


def test_token_file_is_not_readable_by_others(tmp_path):
    """The refresh token is a long-lived credential."""
    path = tmp_path / "tokens.json"
    auth.save(auth.Tokens("a", "r", time.time(), 1, "P"), path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_loading_a_missing_file_returns_none(tmp_path):
    assert auth.load(tmp_path / "absent.json") is None


def test_loading_a_corrupt_file_returns_none_instead_of_raising(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{ not json")
    assert auth.load(path) is None


def test_ensure_fresh_returns_valid_tokens_without_refreshing(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    auth.save(auth.Tokens("access", "refresh", time.time() + 3600, 1, "P"), path)

    def explode(*args, **kwargs):
        raise AssertionError("should not have refreshed a valid token")

    monkeypatch.setattr(auth, "refresh", explode)
    tokens = auth.ensure_fresh("client-id", path)
    assert tokens is not None
    assert tokens.access_token == "access"


def test_ensure_fresh_refreshes_and_persists_expired_tokens(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    auth.save(auth.Tokens("old", "refresh-token", time.time() - 10, 1, "P"), path)

    monkeypatch.setattr(
        auth,
        "refresh",
        lambda cid, t: auth.Tokens("new", "rotated", time.time() + 1200, 1, "P"),
    )
    tokens = auth.ensure_fresh("client-id", path)
    assert tokens is not None
    assert tokens.access_token == "new"
    # The refreshed token must be written back, not just returned.
    assert auth.load(path).access_token == "new"


def test_refresh_token_is_carried_forward_when_ccp_does_not_rotate_it(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    auth.save(auth.Tokens("old", "original-refresh", time.time() - 10, 1, "P"), path)

    monkeypatch.setattr(
        auth,
        "refresh",
        lambda cid, t: auth.Tokens("new", "", time.time() + 1200, 1, "P"),
    )
    tokens = auth.ensure_fresh("client-id", path)
    assert tokens is not None
    # Losing the refresh token here would silently force a re-login later.
    assert tokens.refresh_token == "original-refresh"
    assert auth.load(path).refresh_token == "original-refresh"


def test_missing_scopes_lists_what_was_not_granted():
    tokens = auth.Tokens("a", "r", time.time(), 1, "P", ["esi-ui.open_window.v1"])
    missing = auth.missing_scopes(
        tokens, ["esi-ui.open_window.v1", "esi-wallet.read_character_wallet.v1"]
    )
    assert missing == ["esi-wallet.read_character_wallet.v1"]


def test_login_without_a_client_id_is_refused():
    with pytest.raises(auth.AuthError, match="no client id"):
        auth.login("", "http://localhost:8000/callback")
