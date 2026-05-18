"""Tests for digikey_auth_cli — the credential-lifecycle CLI.

Covers `setup`, `login`, `logout`, `status`, `refresh` subcommands by mocking
the digikey_oauth surface. Verifies that each subcommand drives the right
oauth-module calls and returns the right exit codes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import digikey_auth_cli as auth_cli  # noqa: E402
import digikey_oauth as oauth  # noqa: E402

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_writes_credentials(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    rc = auth_cli.main(
        [
            "setup",
            "--client-id",
            "CID",
            "--client-secret",
            "SEC",
            "--redirect-uri",
            "https://localhost",
            "--environment",
            "sandbox",
        ]
    )
    assert rc == 0
    loaded = oauth.load_config(config_dir=tmp_path)
    assert loaded.client_id == "CID"
    assert loaded.environment == "sandbox"
    out = capsys.readouterr().out
    assert "Wrote credentials" in out
    assert "SEC" not in out


def test_setup_requires_client_id_via_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    with pytest.raises(SystemExit, match="client_id is required"):
        auth_cli.main(["setup", "--client-secret", "SEC"])


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_runs_interactive_and_saves_tokens(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_config(
        oauth.OAuthConfig(client_id="CID", client_secret="SEC"),
        config_dir=tmp_path,
    )
    fake_tokens = oauth.TokenSet(
        access_token="AT",
        refresh_token="RT",
        expires_at=time.time() + 1800,
        refresh_token_expires_at=time.time() + 7776000,
    )
    with mock.patch.object(oauth, "interactive_login", return_value=fake_tokens) as il:
        rc = auth_cli.main(["login", "--no-open"])
    assert rc == 0
    il.assert_called_once()
    persisted = oauth.load_tokens(config_dir=tmp_path)
    assert persisted.access_token == "AT"


def test_login_no_config_exits_with_error(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    # Ensure env vars are not set (would otherwise satisfy load_config).
    for k in (
        "DIGIKEY_CLIENT_ID",
        "DIGIKEY_CLIENT_SECRET",
        "DIGIKEY_REDIRECT_URI",
        "DIGIKEY_ENVIRONMENT",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit, match="client_id"):
        auth_cli.main(["login"])


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


def test_logout_removes_tokens(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_tokens(
        oauth.TokenSet(access_token="AT", refresh_token="RT", expires_at=time.time() + 100),
        config_dir=tmp_path,
    )
    rc = auth_cli.main(["logout"])
    assert rc == 0
    assert oauth.load_tokens(config_dir=tmp_path) is None
    assert "Cached tokens removed" in capsys.readouterr().out


def test_logout_no_tokens_is_a_no_op(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    rc = auth_cli.main(["logout"])
    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_no_credentials_no_tokens(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    for k in (
        "DIGIKEY_CLIENT_ID",
        "DIGIKEY_CLIENT_SECRET",
        "DIGIKEY_REDIRECT_URI",
        "DIGIKEY_ENVIRONMENT",
    ):
        monkeypatch.delenv(k, raising=False)
    rc = auth_cli.main(["status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT CONFIGURED" in out
    assert "NOT LOGGED IN" in out


def test_status_configured_logged_in(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_config(
        oauth.OAuthConfig(client_id="CID123XYZ", client_secret="SEC"),
        config_dir=tmp_path,
    )
    oauth.save_tokens(
        oauth.TokenSet(
            access_token="AT",
            refresh_token="RT",
            expires_at=time.time() + 1800,
            refresh_token_expires_at=time.time() + 7776000,
        ),
        config_dir=tmp_path,
    )
    rc = auth_cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "client credentials: OK" in out
    assert "LOGGED IN" in out
    assert "SEC" not in out  # secret never printed


def test_status_token_expired_returns_logged_in_anyway(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_config(
        oauth.OAuthConfig(client_id="CID", client_secret="SEC"),
        config_dir=tmp_path,
    )
    oauth.save_tokens(
        oauth.TokenSet(
            access_token="AT",
            refresh_token="RT",
            expires_at=time.time() - 60,  # already expired
            refresh_token_expires_at=time.time() + 7776000,
        ),
        config_dir=tmp_path,
    )
    rc = auth_cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto-refresh" in out


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refresh_drives_refresh_and_persists(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_config(
        oauth.OAuthConfig(client_id="CID", client_secret="SEC"),
        config_dir=tmp_path,
    )
    oauth.save_tokens(
        oauth.TokenSet(
            access_token="AT-1",
            refresh_token="RT-1",
            expires_at=time.time() - 60,
            refresh_token_expires_at=time.time() + 7776000,
        ),
        config_dir=tmp_path,
    )
    new_tokens = oauth.TokenSet(
        access_token="AT-2",
        refresh_token="RT-2",
        expires_at=time.time() + 1800,
    )
    with mock.patch.object(oauth, "refresh_tokens", return_value=new_tokens) as rt:
        rc = auth_cli.main(["refresh"])
    assert rc == 0
    rt.assert_called_once()
    persisted = oauth.load_tokens(config_dir=tmp_path)
    assert persisted.access_token == "AT-2"


def test_refresh_no_tokens_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "default_config_dir", lambda: tmp_path)
    oauth.save_config(
        oauth.OAuthConfig(client_id="CID", client_secret="SEC"),
        config_dir=tmp_path,
    )
    with pytest.raises(SystemExit, match="no tokens cached"):
        auth_cli.main(["refresh"])
