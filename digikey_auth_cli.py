"""Companion CLI for managing the DigiKey OAuth credential lifecycle.

Subcommands:
    setup    -- write client_id/client_secret to the user config dir.
    login    -- run the interactive 3-legged auth-code flow once.
    logout   -- delete the cached tokens (forces re-login).
    status   -- print whether tokens are cached and how long until expiry.
    refresh  -- force a refresh of the access_token now (rarely needed —
                push --auth refreshes automatically when within 60 s of expiry).

Installed as the `altium-digikey-auth` entry point alongside
`altium-push-to-digikey`. The two share `digikey_oauth.py`.

App registration prerequisites (one-off, per machine/account):
  1. https://developer.digikey.com → register an application.
  2. Subscribe the app to the MyLists product.
  3. Set OAuth redirect URI to `https://localhost`.
  4. Run `altium-digikey-auth setup` (or set env vars) with the resulting
     client_id + client_secret. Then `altium-digikey-auth login` once.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Optional, Sequence

import digikey_oauth as oauth

PROG = "altium-digikey-auth"


def _cmd_setup(args: argparse.Namespace) -> int:
    cid = args.client_id or input("DigiKey client_id: ").strip()
    if not cid:
        sys.exit("ERROR: client_id is required.")
    csec = args.client_secret or getpass.getpass("DigiKey client_secret (hidden): ").strip()
    if not csec:
        sys.exit("ERROR: client_secret is required.")
    ruri = args.redirect_uri or oauth.DEFAULT_REDIRECT_URI
    env = args.environment or "production"

    cfg = oauth.OAuthConfig(
        client_id=cid,
        client_secret=csec,
        redirect_uri=ruri,
        environment=env,
    )
    path = oauth.save_config(cfg)
    print(f"Wrote credentials to {path}")
    print(f"  client_id: {cfg.client_id[:6]}...")
    print(f"  redirect_uri: {cfg.redirect_uri}")
    print(f"  environment: {cfg.environment}")
    print()
    print("Next step: `altium-digikey-auth login`")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    try:
        cfg = oauth.load_config()
    except oauth.DigiKeyOAuthError as e:
        sys.exit(f"ERROR: {e}")
    try:
        tokens = oauth.interactive_login(
            cfg,
            open_browser=not args.no_open,
            timeout=args.timeout,
        )
    except oauth.DigiKeyOAuthError as e:
        sys.exit(f"ERROR: {e}")
    path = oauth.save_tokens(tokens)
    print(f"\nTokens cached at {path}")
    print(f"  access_token expires in {int(tokens.access_expires_in())}s")
    if tokens.refresh_token_expires_at:
        rem_days = (tokens.refresh_token_expires_at - time.time()) / 86400.0
        print(f"  refresh_token good for ~{rem_days:.0f} days (rotated on each use)")
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    removed = oauth.clear_tokens()
    if removed:
        print("Cached tokens removed. Run `login` to authorize again.")
    else:
        print("No cached tokens found — nothing to do.")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    try:
        cfg = oauth.load_config()
    except oauth.DigiKeyOAuthError as e:
        print(f"client credentials: NOT CONFIGURED ({e.args[0].splitlines()[0]})")
        cfg = None
    if cfg is not None:
        print("client credentials: OK")
        for k, v in cfg.redacted_dict().items():
            print(f"  {k}: {v}")

    try:
        tokens = oauth.load_tokens()
    except oauth.DigiKeyOAuthError as e:
        print(f"tokens: ERROR — {e}")
        return 1
    if tokens is None:
        print("tokens: NOT LOGGED IN — run `altium-digikey-auth login`")
        return 1
    rem = tokens.access_expires_in()
    if rem > 0:
        print(f"tokens: LOGGED IN — access_token valid for {int(rem)}s")
    else:
        print(
            f"tokens: LOGGED IN — access_token expired {int(-rem)}s ago "
            "(will auto-refresh on next push)"
        )
    if tokens.refresh_token_expires_at:
        rd = (tokens.refresh_token_expires_at - time.time()) / 86400.0
        if rd > 0:
            print(f"        refresh_token valid for ~{rd:.0f} more days")
        else:
            print(f"        refresh_token EXPIRED {int(-rd)} days ago — re-login required")
    return 0


def _cmd_refresh(args: argparse.Namespace) -> int:
    try:
        cfg = oauth.load_config()
        tokens = oauth.load_tokens()
    except oauth.DigiKeyOAuthError as e:
        sys.exit(f"ERROR: {e}")
    if tokens is None:
        sys.exit("ERROR: no tokens cached — run `login` first.")
    try:
        new_tokens = oauth.refresh_tokens(cfg, tokens, timeout=args.timeout)
    except oauth.DigiKeyOAuthError as e:
        sys.exit(f"ERROR: {e}")
    oauth.save_tokens(new_tokens)
    print(f"Refreshed. access_token expires in {int(new_tokens.access_expires_in())}s.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=PROG,
        description="Manage DigiKey OAuth2 credentials for altium-push-to-digikey --auth.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    setup = sub.add_parser("setup", help="Write client_id/client_secret to local config.")
    setup.add_argument("--client-id", default=None, help="(else prompted)")
    setup.add_argument("--client-secret", default=None, help="(else prompted, hidden)")
    setup.add_argument(
        "--redirect-uri",
        default=None,
        help=f"OAuth redirect URI registered on developer.digikey.com (default: {oauth.DEFAULT_REDIRECT_URI})",
    )
    setup.add_argument(
        "--environment",
        choices=["production", "sandbox"],
        default=None,
        help="Default: production.",
    )
    setup.set_defaults(func=_cmd_setup)

    login = sub.add_parser("login", help="Interactive 3-legged OAuth login.")
    login.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open the browser; just print the auth URL.",
    )
    login.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds. Default 30.")
    login.set_defaults(func=_cmd_login)

    logout = sub.add_parser("logout", help="Delete cached tokens.")
    logout.set_defaults(func=_cmd_logout)

    status = sub.add_parser("status", help="Show credentials + token status.")
    status.set_defaults(func=_cmd_status)

    refresh = sub.add_parser("refresh", help="Force a token refresh.")
    refresh.add_argument("--timeout", type=int, default=30)
    refresh.set_defaults(func=_cmd_refresh)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
