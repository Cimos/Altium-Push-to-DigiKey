"""Tests for digikey_oauth.

Covers OAuthConfig validation, TokenSet expiry math, config/tokens persistence,
authorize-URL composition, redirect-URL parsing (success + every error path),
token-endpoint calls (success + HTTP error + non-JSON + network error),
refresh rotation, get_valid_access_token, and interactive_login orchestration.

All HTTP is mocked. No live calls to DigiKey.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# These tests cross-emulate POSIX path semantics on Windows by monkeypatching
# do.os.name. That trick works for the Windows branch (test_default_config_dir_windows)
# because pathlib.Path returns a WindowsPath on Windows regardless. It does NOT
# work for the POSIX branches: pathlib refuses to instantiate PosixPath on a
# Windows host. We skip those two on Windows where they cannot be meaningfully
# exercised, and rely on CI's ubuntu-latest matrix entry for coverage.
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX-only path semantics")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import digikey_oauth as do  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload, raw_text=None):
        self.status_code = status_code
        if raw_text is not None:
            self.text = raw_text
            self._payload = None
        else:
            self.text = json.dumps(payload)
            self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("non-json")
        return self._payload


def _good_token_response(expires_in=1800, refresh_in=7775999):
    return {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": expires_in,
        "refresh_token_expires_in": refresh_in,
        "token_type": "BearerToken",
    }


# ---------------------------------------------------------------------------
# OAuthConfig
# ---------------------------------------------------------------------------


def test_oauth_config_defaults_to_production():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    assert cfg.environment == "production"
    assert cfg.host == do.PROD_HOST
    assert cfg.authorize_url == do.PROD_HOST + "/v1/oauth2/authorize"
    assert cfg.token_url == do.PROD_HOST + "/v1/oauth2/token"
    assert cfg.redirect_uri == do.DEFAULT_REDIRECT_URI


def test_oauth_config_sandbox_routes_to_sandbox_host():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC", environment="sandbox")
    assert cfg.host == do.SANDBOX_HOST
    assert cfg.token_url.startswith(do.SANDBOX_HOST)


def test_oauth_config_rejects_bad_environment():
    with pytest.raises(ValueError, match="environment must be"):
        do.OAuthConfig(client_id="CID", client_secret="SEC", environment="staging")


def test_oauth_config_redacted_dict_hides_secret():
    cfg = do.OAuthConfig(client_id="ABCDEFGHIJK", client_secret="topsecret")
    r = cfg.redacted_dict()
    assert r["client_secret"] == "***"
    assert "topsecret" not in str(r)
    assert r["client_id"].startswith("ABCDEF")


# ---------------------------------------------------------------------------
# TokenSet expiry math
# ---------------------------------------------------------------------------


def test_tokenset_needs_refresh_within_window():
    now = time.time()
    fresh = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=now + 3600)
    assert not fresh.needs_refresh()

    expiring_soon = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=now + 30)
    assert expiring_soon.needs_refresh()  # default window=60, 30 <= 60

    just_outside = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=now + 120)
    assert not just_outside.needs_refresh()


def test_tokenset_refresh_token_expired():
    now = time.time()
    rt_fresh = do.TokenSet(
        access_token="AT",
        refresh_token="RT",
        expires_at=now,
        refresh_token_expires_at=now + 1000,
    )
    assert not rt_fresh.refresh_token_expired()

    rt_dead = do.TokenSet(
        access_token="AT",
        refresh_token="RT",
        expires_at=now,
        refresh_token_expires_at=now - 1,
    )
    assert rt_dead.refresh_token_expired()

    # None means we don't know — treat as not expired.
    rt_unknown = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=now)
    assert not rt_unknown.refresh_token_expired()


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def test_save_then_load_config(tmp_path):
    cfg = do.OAuthConfig(
        client_id="CID",
        client_secret="SEC",
        environment="sandbox",
        redirect_uri="https://localhost",
    )
    p = do.save_config(cfg, config_dir=tmp_path)
    assert p.exists()
    loaded = do.load_config(config_dir=tmp_path)
    assert loaded.client_id == "CID"
    assert loaded.client_secret == "SEC"
    assert loaded.environment == "sandbox"


def test_load_config_env_overrides_file(tmp_path, monkeypatch):
    do.save_config(
        do.OAuthConfig(client_id="FILE-CID", client_secret="FILE-SEC"),
        config_dir=tmp_path,
    )
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "ENV-CID")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "ENV-SEC")
    loaded = do.load_config(config_dir=tmp_path)
    assert loaded.client_id == "ENV-CID"
    assert loaded.client_secret == "ENV-SEC"


def test_load_config_missing_credentials_raises(tmp_path, monkeypatch):
    for k in (
        "DIGIKEY_CLIENT_ID",
        "DIGIKEY_CLIENT_SECRET",
        "DIGIKEY_REDIRECT_URI",
        "DIGIKEY_ENVIRONMENT",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(do.DigiKeyOAuthError, match="client_id"):
        do.load_config(config_dir=tmp_path)


def test_load_config_corrupted_file_raises(tmp_path):
    (tmp_path / "credentials.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(do.DigiKeyOAuthError, match="could not read"):
        do.load_config(config_dir=tmp_path)


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def test_save_then_load_tokens(tmp_path):
    now = time.time()
    t = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=now + 1800)
    do.save_tokens(t, config_dir=tmp_path)
    loaded = do.load_tokens(config_dir=tmp_path)
    assert loaded.access_token == "AT"
    assert loaded.refresh_token == "RT"
    assert abs(loaded.expires_at - (now + 1800)) < 0.01


def test_load_tokens_missing_returns_none(tmp_path):
    assert do.load_tokens(config_dir=tmp_path) is None


def test_load_tokens_corrupted_raises(tmp_path):
    (tmp_path / "tokens.json").write_text("not-json{", encoding="utf-8")
    with pytest.raises(do.DigiKeyOAuthError, match="could not read"):
        do.load_tokens(config_dir=tmp_path)


def test_load_tokens_malformed_schema_raises(tmp_path):
    (tmp_path / "tokens.json").write_text(
        json.dumps({"some_old_field": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(do.DigiKeyOAuthError, match="malformed"):
        do.load_tokens(config_dir=tmp_path)


def test_clear_tokens(tmp_path):
    do.save_tokens(do.TokenSet("AT", "RT", time.time() + 60), config_dir=tmp_path)
    assert do.clear_tokens(config_dir=tmp_path) is True
    assert do.clear_tokens(config_dir=tmp_path) is False


# ---------------------------------------------------------------------------
# default_config_dir platform branches
# ---------------------------------------------------------------------------


def test_default_config_dir_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(do.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert do.default_config_dir() == tmp_path / "altium-push-to-digikey"


@posix_only
def test_default_config_dir_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(do.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert do.default_config_dir() == tmp_path / "altium-push-to-digikey"


@posix_only
def test_default_config_dir_posix_home(monkeypatch, tmp_path):
    monkeypatch.setattr(do.os, "name", "posix")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(do.Path, "home", classmethod(lambda cls: tmp_path))
    assert do.default_config_dir() == tmp_path / ".config" / "altium-push-to-digikey"


# ---------------------------------------------------------------------------
# build_authorize_url / parse_redirect_url
# ---------------------------------------------------------------------------


def test_build_authorize_url_includes_required_params():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    url = do.build_authorize_url(cfg, state="abc123")
    assert url.startswith(cfg.authorize_url + "?")
    assert "response_type=code" in url
    assert "client_id=CID" in url
    assert "state=abc123" in url
    assert "redirect_uri=https%3A%2F%2Flocalhost" in url


@pytest.mark.parametrize(
    "redirect",
    [
        "https://localhost/?code=ABC&state=XYZ",
        "http://localhost/?code=ABC&state=XYZ",
        "?code=ABC&state=XYZ",
        "code=ABC&state=XYZ",
    ],
)
def test_parse_redirect_url_accepts_each_form(redirect):
    assert do.parse_redirect_url(redirect, expected_state="XYZ") == "ABC"


def test_parse_redirect_url_state_mismatch():
    with pytest.raises(do.DigiKeyOAuthError, match="state mismatch"):
        do.parse_redirect_url("https://localhost/?code=ABC&state=ATTACKER", expected_state="LEGIT")


def test_parse_redirect_url_no_code():
    with pytest.raises(do.DigiKeyOAuthError, match="no `code`"):
        do.parse_redirect_url("https://localhost/?state=XYZ", expected_state="XYZ")


def test_parse_redirect_url_empty_input():
    with pytest.raises(do.DigiKeyOAuthError, match="empty"):
        do.parse_redirect_url("", expected_state="XYZ")


def test_parse_redirect_url_explicit_error():
    redirect = "https://localhost/?error=access_denied&error_description=user+rejected"
    with pytest.raises(do.DigiKeyOAuthError, match="access_denied"):
        do.parse_redirect_url(redirect, expected_state="XYZ")


# ---------------------------------------------------------------------------
# Token endpoint calls
# ---------------------------------------------------------------------------


def test_exchange_code_for_tokens_success():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    payload = _good_token_response()
    with mock.patch.object(do.requests, "post", return_value=FakeResponse(200, payload)) as m:
        tokens = do.exchange_code_for_tokens(cfg, "CODE")
    assert tokens.access_token == "AT"
    assert tokens.refresh_token == "RT"
    assert tokens.expires_at > time.time() + 1000
    assert tokens.refresh_token_expires_at is not None
    body = m.call_args.kwargs["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "CODE"
    assert body["client_id"] == "CID"
    assert body["client_secret"] == "SEC"
    assert body["redirect_uri"] == do.DEFAULT_REDIRECT_URI


def test_exchange_code_for_tokens_http_error_raises():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    with mock.patch.object(
        do.requests, "post", return_value=FakeResponse(401, None, raw_text="invalid client")
    ):
        with pytest.raises(do.DigiKeyOAuthError, match="HTTP 401"):
            do.exchange_code_for_tokens(cfg, "CODE")


def test_exchange_code_for_tokens_non_json_raises():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    with mock.patch.object(
        do.requests, "post", return_value=FakeResponse(200, None, raw_text="<html>")
    ):
        with pytest.raises(do.DigiKeyOAuthError, match="non-JSON"):
            do.exchange_code_for_tokens(cfg, "CODE")


def test_exchange_code_for_tokens_network_error_raises():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    with mock.patch.object(
        do.requests, "post", side_effect=do.requests.exceptions.ConnectionError("down")
    ):
        with pytest.raises(do.DigiKeyOAuthError, match="network error"):
            do.exchange_code_for_tokens(cfg, "CODE")


def test_exchange_code_for_tokens_missing_field_raises():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    bad = {"access_token": "AT"}  # no refresh_token, no expires_in
    with mock.patch.object(do.requests, "post", return_value=FakeResponse(200, bad)):
        with pytest.raises(do.DigiKeyOAuthError, match="missing required field"):
            do.exchange_code_for_tokens(cfg, "CODE")


def test_refresh_tokens_success_rotates():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    old = do.TokenSet(
        access_token="AT-1",
        refresh_token="RT-1",
        expires_at=time.time() + 1,
        refresh_token_expires_at=time.time() + 99999,
    )
    new_payload = {**_good_token_response(), "access_token": "AT-2", "refresh_token": "RT-2"}
    with mock.patch.object(do.requests, "post", return_value=FakeResponse(200, new_payload)) as m:
        new = do.refresh_tokens(cfg, old)
    assert new.access_token == "AT-2"
    assert new.refresh_token == "RT-2"
    assert m.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert m.call_args.kwargs["data"]["refresh_token"] == "RT-1"


def test_refresh_tokens_refresh_expired_raises():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    dead = do.TokenSet(
        access_token="AT",
        refresh_token="RT",
        expires_at=0,
        refresh_token_expires_at=time.time() - 1,
    )
    with pytest.raises(do.DigiKeyOAuthError, match="expired"):
        do.refresh_tokens(cfg, dead)


def test_refresh_tokens_sandbox_routes_to_sandbox():
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC", environment="sandbox")
    tokens = do.TokenSet(access_token="AT", refresh_token="RT", expires_at=0)
    with mock.patch.object(
        do.requests, "post", return_value=FakeResponse(200, _good_token_response())
    ) as m:
        do.refresh_tokens(cfg, tokens)
    assert "sandbox-api.digikey.com" in m.call_args.args[0]


# ---------------------------------------------------------------------------
# get_valid_access_token
# ---------------------------------------------------------------------------


def test_get_valid_access_token_no_tokens_cached_raises(tmp_path, monkeypatch):
    do.save_config(
        do.OAuthConfig(client_id="CID", client_secret="SEC"),
        config_dir=tmp_path,
    )
    with pytest.raises(do.DigiKeyOAuthError, match="no cached"):
        do.get_valid_access_token(config_dir=tmp_path)


def test_get_valid_access_token_fresh_returns_without_refresh(tmp_path):
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    fresh = do.TokenSet(access_token="AT-fresh", refresh_token="RT", expires_at=time.time() + 3600)
    with mock.patch.object(do.requests, "post") as m:
        access, ret_cfg, tokens = do.get_valid_access_token(
            config=cfg,
            tokens=fresh,
            config_dir=tmp_path,
        )
    assert access == "AT-fresh"
    m.assert_not_called()


def test_get_valid_access_token_refreshes_and_persists(tmp_path):
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    stale = do.TokenSet(
        access_token="AT-old",
        refresh_token="RT-1",
        expires_at=time.time() - 1,
        refresh_token_expires_at=time.time() + 99999,
    )
    new_payload = {**_good_token_response(), "access_token": "AT-new", "refresh_token": "RT-2"}
    with mock.patch.object(do.requests, "post", return_value=FakeResponse(200, new_payload)):
        access, _, tokens = do.get_valid_access_token(
            config=cfg,
            tokens=stale,
            config_dir=tmp_path,
        )
    assert access == "AT-new"
    persisted = do.load_tokens(config_dir=tmp_path)
    assert persisted.access_token == "AT-new"
    assert persisted.refresh_token == "RT-2"


# ---------------------------------------------------------------------------
# interactive_login
# ---------------------------------------------------------------------------


def test_interactive_login_full_flow(tmp_path):
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    captured = []
    fed_state = []

    def fake_input(prompt):
        # Inspect the printed lines to find the state we generated.
        printed_blob = "\n".join(captured)
        # The state is embedded in the authorize URL we printed.
        # Extract it from the printed URL via a quick search.
        import re

        m = re.search(r"state=([A-Za-z0-9_\-]+)", printed_blob)
        assert m, "test could not see authorize URL with state in output"
        st = m.group(1)
        fed_state.append(st)
        return f"https://localhost/?code=AUTHCODE&state={st}"

    def fake_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    payload = _good_token_response()
    with mock.patch.object(do.requests, "post", return_value=FakeResponse(200, payload)) as m:
        tokens = do.interactive_login(
            cfg,
            open_browser=False,
            input_fn=fake_input,
            print_fn=fake_print,
        )
    assert tokens.access_token == "AT"
    assert tokens.refresh_token == "RT"
    assert fed_state, "state was not fed back through"
    assert m.call_args.kwargs["data"]["code"] == "AUTHCODE"


def test_interactive_login_user_paste_with_error(tmp_path):
    cfg = do.OAuthConfig(client_id="CID", client_secret="SEC")
    captured = []

    def fake_input(prompt):
        # Capture state from printed URL.
        import re

        printed_blob = "\n".join(captured)
        m = re.search(r"state=([A-Za-z0-9_\-]+)", printed_blob)
        return f"https://localhost/?error=access_denied&state={m.group(1)}"

    def fake_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    with pytest.raises(do.DigiKeyOAuthError, match="access_denied"):
        do.interactive_login(cfg, open_browser=False, input_fn=fake_input, print_fn=fake_print)
