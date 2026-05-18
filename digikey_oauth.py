"""OAuth2 3-legged authorization-code client for DigiKey's MyLists API.

This module is the auth half of `altium-push-to-digikey --auth`. It handles:

    register-once     -> the user creates an app on developer.digikey.com,
                         subscribes it to MyLists, and supplies client_id +
                         client_secret via env vars or a config file.
    login-interactive -> one browser round-trip per machine to mint a
                         (access_token, refresh_token) pair, stored on disk.
    refresh           -> the access_token expires after ~30 min; refresh
                         transparently using the (long-lived, rotating)
                         refresh_token. The user does not see this.

Two CLI entry points consume it:

    `altium-push-to-digikey --auth <bom>`  - normal push, uses cached tokens.
    `altium-digikey-auth login|logout|...`  - manage the credential lifecycle.

Endpoints and flow are documented at:
    https://developer.digikey.com/tutorials-and-resources/oauth-20-3-legged-flow
    https://developer.digikey.com/faq/oauth-authentication-and-authorization

Redirect-URI strategy
---------------------
DigiKey requires the redirect_uri to be TLS, and explicitly accepts
`https://localhost` for clients that have no public infrastructure (FAQ:
"If you do not have the infrastructure setup to handle responses from
DigiKey, you can use the initial value of `https://localhost`").

We do NOT start a local TLS server (would need a self-signed cert and a
non-stdlib dep). Instead, after the user authorizes the app, DigiKey
redirects the browser to `https://localhost/?code=...`. The browser will
fail to connect (nothing's listening there) but the address bar still
shows the full URL including the code. The user copy-pastes that URL back
to the CLI. Robust, dependency-free, well-known OAuth-for-CLI pattern.

This module adds no dependencies beyond `requests`, which is already required
by the rest of the package.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover - same diagnostic as digikey_push
    sys.exit("ERROR: the `requests` library is required.\n" "Install with: pip install requests")


__all__ = [
    "DigiKeyOAuthError",
    "OAuthConfig",
    "TokenSet",
    "default_config_dir",
    "load_config",
    "save_config",
    "load_tokens",
    "save_tokens",
    "clear_tokens",
    "build_authorize_url",
    "parse_redirect_url",
    "exchange_code_for_tokens",
    "refresh_tokens",
    "interactive_login",
    "get_valid_access_token",
]


log = logging.getLogger("altium_push_to_digikey.oauth")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


PROD_HOST = "https://api.digikey.com"
SANDBOX_HOST = "https://sandbox-api.digikey.com"

_AUTHORIZE_PATH = "/v1/oauth2/authorize"
_TOKEN_PATH = "/v1/oauth2/token"

DEFAULT_REDIRECT_URI = "https://localhost"
ENVIRONMENTS = ("production", "sandbox")

# Safety margin: refresh the access_token if it has fewer than this many
# seconds of life left at the time of use. DigiKey access tokens live for
# ~1800 s; 60 s is a generous cushion for clock skew + HTTP round-trip.
DEFAULT_REFRESH_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DigiKeyOAuthError(RuntimeError):
    """Raised on any OAuth-flow failure that should surface to the CLI."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class OAuthConfig:
    """App registration data — what you get from developer.digikey.com after
    registering an application and subscribing it to MyLists."""

    client_id: str
    client_secret: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    environment: str = "production"  # or "sandbox"

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {ENVIRONMENTS!r}, got {self.environment!r}"
            )

    @property
    def host(self) -> str:
        return SANDBOX_HOST if self.environment == "sandbox" else PROD_HOST

    @property
    def authorize_url(self) -> str:
        return self.host + _AUTHORIZE_PATH

    @property
    def token_url(self) -> str:
        return self.host + _TOKEN_PATH

    def redacted_dict(self) -> dict:
        """For status / log output — never prints the secret."""
        return {
            "client_id": self.client_id[:6] + "..." if self.client_id else "",
            "client_secret": "***" if self.client_secret else "",
            "redirect_uri": self.redirect_uri,
            "environment": self.environment,
        }


@dataclass
class TokenSet:
    """One issued (access, refresh) pair, plus when the access token expires.

    `expires_at` is an absolute epoch second (time.time() basis), NOT a delta.
    `refresh_token_expires_at` is the same shape for the refresh token; DigiKey
    refresh tokens currently live ~90 days and are rotated on each use.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    refresh_token_expires_at: Optional[float] = None
    token_type: str = "Bearer"
    issued_at: float = field(default_factory=time.time)

    def access_expires_in(self, now: Optional[float] = None) -> float:
        return self.expires_at - (now if now is not None else time.time())

    def needs_refresh(
        self, window: float = DEFAULT_REFRESH_WINDOW_SECONDS, now: Optional[float] = None
    ) -> bool:
        return self.access_expires_in(now) <= window

    def refresh_token_expired(self, now: Optional[float] = None) -> bool:
        if self.refresh_token_expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.refresh_token_expires_at


# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------


def default_config_dir() -> Path:
    """Per-user directory for client credentials + token cache.

    Windows: %APPDATA%\\altium-push-to-digikey
    POSIX:   $XDG_CONFIG_HOME/altium-push-to-digikey  (or ~/.config/...)
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "altium-push-to-digikey"


def _config_path(d: Optional[Path] = None) -> Path:
    return (d or default_config_dir()) / "credentials.json"


def _tokens_path(d: Optional[Path] = None) -> Path:
    return (d or default_config_dir()) / "tokens.json"


# ---------------------------------------------------------------------------
# Config (client_id / client_secret / redirect / env)
# ---------------------------------------------------------------------------


def load_config(*, config_dir: Optional[Path] = None) -> OAuthConfig:
    """Load app credentials from env first, then config file.

    Env vars (preferred for CI):
        DIGIKEY_CLIENT_ID
        DIGIKEY_CLIENT_SECRET
        DIGIKEY_REDIRECT_URI (optional; default https://localhost)
        DIGIKEY_ENVIRONMENT  (optional; "production" | "sandbox")

    File: <config_dir>/credentials.json with the same keys (lower-case dict).

    Raises DigiKeyOAuthError if neither source supplies client_id+secret.
    """
    cid = os.environ.get("DIGIKEY_CLIENT_ID") or ""
    csec = os.environ.get("DIGIKEY_CLIENT_SECRET") or ""
    ruri = os.environ.get("DIGIKEY_REDIRECT_URI") or ""
    env = os.environ.get("DIGIKEY_ENVIRONMENT") or ""

    path = _config_path(config_dir)
    file_data: dict = {}
    if path.is_file():
        try:
            file_data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise DigiKeyOAuthError(f"could not read {path}: {e}") from e

    cid = cid or file_data.get("client_id") or ""
    csec = csec or file_data.get("client_secret") or ""
    ruri = ruri or file_data.get("redirect_uri") or DEFAULT_REDIRECT_URI
    env = env or file_data.get("environment") or "production"

    if not cid or not csec:
        raise DigiKeyOAuthError(
            "No DigiKey client_id / client_secret found.\n"
            "Set DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET env vars, or run\n"
            "    altium-digikey-auth setup\n"
            f"to write {path}.\n\n"
            "App registration: https://developer.digikey.com (create an app,\n"
            "subscribe it to the MyLists product, set redirect URI to\n"
            f"`{DEFAULT_REDIRECT_URI}`)."
        )

    return OAuthConfig(client_id=cid, client_secret=csec, redirect_uri=ruri, environment=env)


def save_config(config: OAuthConfig, *, config_dir: Optional[Path] = None) -> Path:
    """Persist client credentials to credentials.json. Returns the path written."""
    d = config_dir or default_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _config_path(d)
    payload = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": config.redirect_uri,
        "environment": config.environment,
    }
    _atomic_write_json(path, payload, mode=0o600)
    return path


# ---------------------------------------------------------------------------
# Tokens (access + refresh)
# ---------------------------------------------------------------------------


def load_tokens(*, config_dir: Optional[Path] = None) -> Optional[TokenSet]:
    """Read the cached TokenSet, or None if no cache file exists.

    Raises DigiKeyOAuthError on a malformed cache file (corruption / partial
    write); callers can recover by calling clear_tokens() and re-running
    interactive_login().
    """
    path = _tokens_path(config_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise DigiKeyOAuthError(f"could not read {path}: {e}") from e
    try:
        return TokenSet(**data)
    except TypeError as e:
        raise DigiKeyOAuthError(
            f"{path} is malformed (likely from an older version); "
            f"run `altium-digikey-auth logout` and log back in. ({e})"
        ) from e


def save_tokens(tokens: TokenSet, *, config_dir: Optional[Path] = None) -> Path:
    d = config_dir or default_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _tokens_path(d)
    _atomic_write_json(path, asdict(tokens), mode=0o600)
    return path


def clear_tokens(*, config_dir: Optional[Path] = None) -> bool:
    path = _tokens_path(config_dir)
    if path.is_file():
        path.unlink()
        return True
    return False


def _atomic_write_json(path: Path, data: dict, *, mode: int = 0o600) -> None:
    """Write `data` to `path` atomically: write to .tmp, then os.replace().

    `mode` is applied on POSIX (chmod after write); on Windows it's a no-op
    since file ACLs already inherit from the per-user APPDATA tree.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(tmp, mode)
        except OSError:  # pragma: no cover - tmpfs / odd FS
            pass
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Authorize URL + redirect parsing
# ---------------------------------------------------------------------------


def build_authorize_url(config: OAuthConfig, state: str) -> str:
    """Build the URL the user opens in their browser to authorize the app."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "state": state,
    }
    return config.authorize_url + "?" + urllib.parse.urlencode(params)


def parse_redirect_url(url_or_query: str, expected_state: str) -> str:
    """Pull the auth `code` from a redirect URL (or bare query string).

    Accepts forms like:
        https://localhost/?code=ABC&state=XYZ
        http://localhost/?code=ABC&state=XYZ
        ?code=ABC&state=XYZ
        code=ABC&state=XYZ

    Validates the state matches; raises DigiKeyOAuthError on mismatch or on
    explicit `error=` query params from DigiKey.
    """
    s = (url_or_query or "").strip()
    if not s:
        raise DigiKeyOAuthError("empty redirect URL — nothing to parse")

    # Strip a leading scheme://host[:port]/path so urllib.parse.parse_qs is happy.
    parsed = urllib.parse.urlparse(s)
    if parsed.scheme:
        query = parsed.query
    else:
        # Either bare "?code=..." or "code=..." — drop a leading '?'.
        query = s.lstrip("?")

    params = urllib.parse.parse_qs(query, keep_blank_values=False)

    if "error" in params:
        desc = params.get("error_description", ["(no description)"])[0]
        raise DigiKeyOAuthError(
            f"DigiKey returned an authorization error: {params['error'][0]} — {desc}"
        )

    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]

    if not code:
        raise DigiKeyOAuthError(f"no `code` parameter in redirect URL: {url_or_query!r}")
    if state != expected_state:
        raise DigiKeyOAuthError(
            "state mismatch: the URL you pasted does not match the auth request "
            "this session started. This can indicate a CSRF attempt, a stale "
            "browser tab, or an accidental cross-paste. Retry `login`."
        )

    return code


# ---------------------------------------------------------------------------
# Token endpoint calls
# ---------------------------------------------------------------------------


def _post_token(
    config: OAuthConfig,
    body: dict,
    *,
    timeout: int,
    session: Optional[requests.Session] = None,
) -> dict:
    """POST x-www-form-urlencoded body to the token endpoint; return parsed JSON."""
    poster = session.post if session is not None else requests.post
    log.debug("POST %s body-keys=%s", config.token_url, sorted(body.keys()))
    try:
        resp = poster(
            config.token_url,
            data=body,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise DigiKeyOAuthError(f"network error contacting token endpoint: {e}") from e

    if resp.status_code != 200:
        raise DigiKeyOAuthError(
            f"token endpoint returned HTTP {resp.status_code}: {resp.text[:400]}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise DigiKeyOAuthError(f"token endpoint returned non-JSON: {resp.text[:400]}") from e


def _tokens_from_response(payload: dict) -> TokenSet:
    """Build a TokenSet from a DigiKey token-endpoint response.

    Response shape (per DigiKey docs):
        {
          "access_token": "...",
          "refresh_token": "...",
          "expires_in": 1799,
          "refresh_token_expires_in": 7775999,
          "token_type": "BearerToken"
        }
    """
    try:
        access = payload["access_token"]
        refresh = payload["refresh_token"]
        expires_in = float(payload["expires_in"])
    except KeyError as e:
        raise DigiKeyOAuthError(f"token response missing required field {e!s}: {payload!r}") from e

    now = time.time()
    rte = payload.get("refresh_token_expires_in")
    refresh_at = None
    if rte is not None:
        try:
            refresh_at = now + float(rte)
        except (TypeError, ValueError):
            refresh_at = None

    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=now + expires_in,
        refresh_token_expires_at=refresh_at,
        token_type=str(payload.get("token_type") or "Bearer"),
        issued_at=now,
    )


def exchange_code_for_tokens(
    config: OAuthConfig,
    code: str,
    *,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> TokenSet:
    """Exchange an authorization code for an (access, refresh) pair."""
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "redirect_uri": config.redirect_uri,
    }
    payload = _post_token(config, body, timeout=timeout, session=session)
    return _tokens_from_response(payload)


def refresh_tokens(
    config: OAuthConfig,
    tokens: TokenSet,
    *,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> TokenSet:
    """Mint a fresh (access, refresh) pair using the current refresh_token.

    DigiKey rotates the refresh_token on every successful refresh — the old
    one is invalidated. Persist the returned TokenSet immediately to avoid
    losing access on a crash mid-rotation.
    """
    if tokens.refresh_token_expired():
        raise DigiKeyOAuthError(
            "refresh_token has expired — run `altium-digikey-auth login` to reauthorize."
        )
    body = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }
    payload = _post_token(config, body, timeout=timeout, session=session)
    return _tokens_from_response(payload)


# ---------------------------------------------------------------------------
# Interactive login (manual-paste callback)
# ---------------------------------------------------------------------------


def interactive_login(
    config: OAuthConfig,
    *,
    open_browser: bool = True,
    input_fn=None,
    print_fn=None,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> TokenSet:
    """Run the 3-legged auth-code flow with manual paste of the redirect URL.

    Steps:
      1. Build the authorize URL with a fresh `state`.
      2. Open it in the user's default browser.
      3. The user authorizes the app on digikey.com.
      4. DigiKey redirects browser to `<redirect_uri>?code=...&state=...`.
      5. Browser fails to load (no listener on https://localhost) but the
         address bar still shows the URL. User copies it back.
      6. We extract the code, validate the state, exchange for tokens.
    """
    inp = input_fn or input
    out = print_fn or print

    state = secrets.token_urlsafe(16)
    auth_url = build_authorize_url(config, state)

    out("\nDigiKey OAuth2 — interactive login")
    out("=" * 40)
    out(f"Environment: {config.environment}")
    out(f"Redirect URI: {config.redirect_uri}")
    out("")
    out("Open this URL in your browser and authorize the app:")
    out("")
    out(f"  {auth_url}")
    out("")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception as e:  # pragma: no cover - browser plumbing varies
            log.debug("webbrowser.open failed: %s", e)

    out(
        "After you click Allow, your browser will try to load "
        f"{config.redirect_uri} and will show 'site can't be reached' or a "
        "TLS warning. That is expected — nothing is listening there. "
        "Copy the FULL URL from the browser's address bar (it will include "
        "`?code=...&state=...`) and paste it below."
    )
    out("")

    redirect = inp("Paste redirect URL (or just the `?code=...` query): ").strip()
    code = parse_redirect_url(redirect, expected_state=state)

    out("\nExchanging code for tokens...")
    tokens = exchange_code_for_tokens(config, code, timeout=timeout, session=session)
    out(
        "Got tokens. access_token expires in "
        f"{int(tokens.access_expires_in())}s; "
        "refresh_token is long-lived (rotated on every refresh)."
    )
    return tokens


# ---------------------------------------------------------------------------
# High-level convenience
# ---------------------------------------------------------------------------


def get_valid_access_token(
    *,
    config: Optional[OAuthConfig] = None,
    tokens: Optional[TokenSet] = None,
    config_dir: Optional[Path] = None,
    refresh_window: float = DEFAULT_REFRESH_WINDOW_SECONDS,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
    persist: bool = True,
) -> tuple:
    """Return (access_token, OAuthConfig, TokenSet) ready to use against api.digikey.com.

    Loads config + tokens from disk if not provided. Refreshes the access
    token if it's expired or near-expiring. Persists the refreshed token set
    back to disk (unless persist=False — useful in tests).

    Raises DigiKeyOAuthError if no tokens are cached (user must run `login`
    first) or if refresh fails.
    """
    if config is None:
        config = load_config(config_dir=config_dir)
    if tokens is None:
        tokens = load_tokens(config_dir=config_dir)
    if tokens is None:
        raise DigiKeyOAuthError("no cached DigiKey tokens — run `altium-digikey-auth login` first.")

    if tokens.needs_refresh(window=refresh_window):
        log.debug("access_token within refresh window — refreshing")
        tokens = refresh_tokens(config, tokens, timeout=timeout, session=session)
        if persist:
            save_tokens(tokens, config_dir=config_dir)

    return tokens.access_token, config, tokens
