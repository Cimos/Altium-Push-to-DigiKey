#!/usr/bin/env python3
"""Push an Altium-emitted BOM into a DigiKey myLists / cart.

Usage:
    altium-push-to-digikey <bom.json|bom.csv> [--list-name NAME] [--tags TAGS]
                                              [--scale N] [--prefer dkpn|mpn]
                                              [--aggregate / --no-aggregate]
                                              [--out PATH] [--dry-run]
                                              [--open] [--verbose]
                                              [--timeout SECONDS]

Endpoint used: https://www.digikey.com/mylists/api/thirdparty (POST, anonymous).
The endpoint returns a short URL of the form https://www.digikey.com/short/<code>
which the user opens in a browser to land the list under their DigiKey account.
From there the list can be converted to a cart with one click.

API surface discovered from:
    https://github.com/Digi-Key/KiCad-Push-to-DigiKey (MIT licensed)

Input formats:
    1. review-pack `bom.json` -- preferred; normalised form emitted by
       Altium-emit-review-pack scripts and CubePilot production-agents pipeline.
       Schema: { "rows": [ { "mpn": str, "quantity": int, "ref_des": [str, ...],
                              "dnp": bool, "dkpn": str (optional), ... }, ... ] }
    2. Altium raw `bom.csv` -- direct CSV export from Altium's BOM Output Job.
       Auto-detects MPN, Quantity, Designator, and (optionally) DigiKey-PN
       columns by header name (tolerant of common variants -- see DEFAULT_*_COLS).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import webbrowser
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: the `requests` library is required.\n"
        "Install with: pip install requests\n"
        "or: pip install -r requirements.txt"
    )


__version__ = "0.2.0"

API_URL = "https://www.digikey.com/mylists/api/thirdparty"
SHORT_URL_RE = re.compile(r"^https?://(www\.)?digikey\.com/short/[0-9a-z]+", re.IGNORECASE)

DEFAULT_MPN_COLS: List[str] = [
    "Manufacturer Part Number 1",
    "Manufacturer Part Number",
    "MPN",
    "Part Number",
    "PartNumber",
    "ManufacturerPartNumber",
]
DEFAULT_QTY_COLS: List[str] = ["Quantity", "Qty", "QTY"]
DEFAULT_REF_COLS: List[str] = [
    "Designator",
    "Designators",
    "Reference",
    "References",
    "RefDes",
    "Ref",
]
# DigiKey supplier-part-number columns. Recognised even when present alongside
# generic "Supplier Part Number" columns from other distributors -- we still
# require the matching "Supplier" column (or "Supplier 1") to name DigiKey.
DEFAULT_DKPN_COLS: List[str] = [
    "DigiKey Part Number",
    "DigiKey PN",
    "DKPN",
    "DK Part Number",
    "Supplier Part Number 1",
    "Supplier Part Number",
]
DEFAULT_SUPPLIER_COLS: List[str] = ["Supplier 1", "Supplier"]

DIGIKEY_SUPPLIER_NAMES = {"digikey", "digi-key", "digi key", "dk"}

log = logging.getLogger("altium_push_to_digikey")


# ---------------------------------------------------------------------------
# BOM loading
# ---------------------------------------------------------------------------


def _find_col(cols: Sequence[Optional[str]], candidates: Sequence[str]) -> Optional[str]:
    """Return the actual header in `cols` that matches any candidate (case-insensitive)."""
    lookup = {c.lower(): c for c in cols if c is not None}
    for cand in candidates:
        if cand.lower() in lookup:
            return lookup[cand.lower()]
    return None


def _coerce_qty(value) -> int:
    """Best-effort parse of a quantity field. Returns 0 on failure (caller skips)."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    s = str(value).strip()
    if not s:
        return 0
    # Tolerate "3 pcs", "3.0", "  3 ".
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)", s)
    if not m:
        return 0
    try:
        return int(float(m.group(1)))
    except (TypeError, ValueError):
        return 0


def load_bom_json(path: str) -> List[Dict]:
    """Load review-pack bom.json. Returns list of {mpn, qty, refs, dkpn}."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    items: List[Dict] = []
    for row in data.get("rows", []):
        if row.get("dnp"):
            continue
        mpn = (row.get("mpn") or "").strip()
        if not mpn:
            continue
        qty = _coerce_qty(row.get("quantity"))
        if qty <= 0:
            continue
        refs = ", ".join(row.get("ref_des") or [])
        dkpn = (row.get("dkpn") or row.get("digikey_pn") or "").strip()
        items.append({"mpn": mpn, "qty": qty, "refs": refs, "dkpn": dkpn})
    return items


def load_bom_csv(path: str) -> List[Dict]:
    """Load an Altium-style BOM CSV. Auto-detects MPN / Quantity / Designator / DKPN columns."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = reader.fieldnames or []

    if not rows:
        return []

    mpn_col = _find_col(cols, DEFAULT_MPN_COLS)
    qty_col = _find_col(cols, DEFAULT_QTY_COLS)
    ref_col = _find_col(cols, DEFAULT_REF_COLS)
    dkpn_col = _find_col(cols, DEFAULT_DKPN_COLS)
    supplier_col = _find_col(cols, DEFAULT_SUPPLIER_COLS)

    if not mpn_col or not qty_col:
        raise SystemExit(
            f"ERROR: could not locate MPN and/or Quantity columns in BOM CSV.\n"
            f"  Available columns: {cols}\n"
            f"  Expected MPN column from: {DEFAULT_MPN_COLS}\n"
            f"  Expected Quantity column from: {DEFAULT_QTY_COLS}"
        )

    log.debug(
        "CSV columns: mpn=%r qty=%r refs=%r dkpn=%r supplier=%r",
        mpn_col,
        qty_col,
        ref_col,
        dkpn_col,
        supplier_col,
    )

    items: List[Dict] = []
    for r in rows:
        mpn = (r.get(mpn_col) or "").strip()
        if not mpn:
            continue
        qty = _coerce_qty(r.get(qty_col))
        if qty <= 0:
            continue
        refs = (r.get(ref_col) or "").strip() if ref_col else ""

        dkpn = ""
        if dkpn_col:
            candidate = (r.get(dkpn_col) or "").strip()
            # Only accept the DKPN if the supplier column (when present) names
            # DigiKey; otherwise a "Supplier Part Number 1" might be a Mouser/
            # Element14/etc. part number that DigiKey will reject.
            if candidate:
                if supplier_col:
                    supplier = (r.get(supplier_col) or "").strip().lower()
                    if supplier in DIGIKEY_SUPPLIER_NAMES:
                        dkpn = candidate
                else:
                    # No supplier column -- if the dkpn column header itself
                    # named DigiKey (e.g. "DigiKey Part Number"), trust it.
                    if "digikey" in dkpn_col.lower() or "dkpn" in dkpn_col.lower():
                        dkpn = candidate

        items.append({"mpn": mpn, "qty": qty, "refs": refs, "dkpn": dkpn})
    return items


def load_bom(path: str) -> List[Dict]:
    """Dispatch to load_bom_json / load_bom_csv based on file extension."""
    p = path.lower()
    if p.endswith(".json"):
        return load_bom_json(path)
    if p.endswith(".csv"):
        return load_bom_csv(path)
    raise SystemExit("ERROR: BOM file must end in .json (review-pack) or .csv (Altium export).")


# ---------------------------------------------------------------------------
# Aggregation and transformation
# ---------------------------------------------------------------------------


def aggregate_by_mpn(items: Iterable[Dict]) -> List[Dict]:
    """Merge rows that share the same MPN. Sums quantities, concatenates unique refs,
    preserves the first non-empty DKPN seen."""
    merged: Dict[str, Dict] = {}
    order: List[str] = []
    for it in items:
        key = (it.get("mpn") or "").strip().upper()
        if not key:
            continue
        if key not in merged:
            merged[key] = {
                "mpn": it["mpn"],
                "qty": int(it.get("qty") or 0),
                "refs": it.get("refs") or "",
                "dkpn": it.get("dkpn") or "",
            }
            order.append(key)
            continue
        existing = merged[key]
        existing["qty"] += int(it.get("qty") or 0)
        new_refs = it.get("refs") or ""
        if new_refs:
            seen = {r.strip() for r in existing["refs"].split(",") if r.strip()}
            for r in new_refs.split(","):
                r = r.strip()
                if r and r not in seen:
                    seen.add(r)
                    existing["refs"] = existing["refs"] + ", " + r if existing["refs"] else r
        if not existing["dkpn"] and it.get("dkpn"):
            existing["dkpn"] = it["dkpn"]
    return [merged[k] for k in order]


def scale_quantities(items: Iterable[Dict], factor: int) -> List[Dict]:
    """Multiply every row's qty by factor (rounded up to next int, min 1)."""
    if factor <= 0:
        raise ValueError("scale factor must be a positive integer")
    out: List[Dict] = []
    for it in items:
        new_qty = max(1, int(it.get("qty") or 0) * factor)
        out.append({**it, "qty": new_qty})
    return out


def pick_part_number(item: Dict, prefer: str) -> str:
    """Return the part-number string to send to DigiKey.

    prefer = 'mpn' -> always send MPN (default; matches KiCad-Push-to-DigiKey).
    prefer = 'dkpn' -> send DKPN if present, else fall back to MPN.
    """
    if prefer == "dkpn":
        dkpn = (item.get("dkpn") or "").strip()
        if dkpn:
            return dkpn
    return (item.get("mpn") or "").strip()


def build_payload(items: Iterable[Dict], prefer: str = "mpn") -> List[Dict]:
    """Build the JSON body for POST /mylists/api/thirdparty.

    Schema (from DigiKey KiCad reference):
        [ { "requestedPartNumber": <MPN or DKPN>,
            "quantities": [ {"quantity": <int>} ],
            "customerReference": <designators or empty>,
            "notes": <string or empty> }, ... ]
    """
    return [
        {
            "requestedPartNumber": pick_part_number(it, prefer),
            "quantities": [{"quantity": int(it.get("qty") or 0)}],
            "customerReference": it.get("refs") or "",
            "notes": "",
        }
        for it in items
    ]


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push(
    items: Iterable[Dict],
    list_name: str,
    tags: str = "",
    timeout: int = 30,
    prefer: str = "mpn",
    session: Optional[requests.Session] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """POST the BOM to DigiKey's thirdparty endpoint. Returns (short_url, error)."""
    payload = build_payload(items, prefer=prefer)
    params: Dict[str, str] = {"listName": list_name}
    if tags:
        params["tags"] = tags

    poster = session.post if session is not None else requests.post
    log.debug("POST %s params=%r payload-rows=%d", API_URL, params, len(payload))

    try:
        resp = poster(API_URL, json=payload, params=params, verify=True, timeout=timeout)
    except requests.RequestException as e:
        return None, f"network error: {e}"

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:400]}"

    try:
        result = json.loads(resp.text)
    except ValueError:
        return None, f"non-JSON response: {resp.text[:400]}"

    # DigiKey returns the short URL as a bare JSON string.
    if isinstance(result, str) and SHORT_URL_RE.match(result):
        return result, None
    return None, f"unexpected response shape: {result!r}"


# ---------------------------------------------------------------------------
# Authenticated push (OAuth2 / MyLists v1)
# ---------------------------------------------------------------------------

# Authenticated endpoint base. The DigiKey developer-portal-side MyLists v1
# API is a different surface from the anonymous /mylists/api/thirdparty
# endpoint used by push(): it requires a 3-legged-OAuth bearer token, splits
# create-list and add-parts into two POSTs, and lands the result directly
# in the authenticated user's myLists with no public short URL.
#
# Endpoint shape cross-referenced (NOT yet bench-verified) against a community
# Python implementation at
# https://github.com/shun0211/zenn-articles/blob/main/articles/digikey-api-python.md
# (referenced 2026-05-18). [unverified-on-target] — see README and CHANGELOG.
# The first successful `--auth` push against a live DigiKey account is the
# verification step. If response shape differs from what _extract_list_id
# tolerates, patch here and remove this caveat.
AUTH_API_BASE_PROD = "https://api.digikey.com"
AUTH_API_BASE_SANDBOX = "https://sandbox-api.digikey.com"
AUTH_LISTS_PATH = "/mylists/v1/lists"


def _auth_api_base(environment: str) -> str:
    return AUTH_API_BASE_SANDBOX if environment == "sandbox" else AUTH_API_BASE_PROD


def _auth_headers(client_id: str, access_token: str) -> Dict[str, str]:
    """Required header set for authenticated MyLists calls."""
    return {
        "Authorization": f"Bearer {access_token}",
        "X-DIGIKEY-Client-Id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_auth_part_payload(items: Iterable[Dict], prefer: str = "mpn") -> List[Dict]:
    """Build the array body for POST /mylists/v1/lists/{listId}/parts.

    Schema (per the zenn-articles reference; see module docstring):
        [ { "RequestedPartNumber": <MPN or DKPN>,
            "OriginalPartNumber":  <same as RequestedPartNumber by default>,
            "Quantities": [ {"Quantity": <int>} ],
            "CustomerReference": <designators or empty>,
            "Notes": <string or empty> }, ... ]
    """
    payload: List[Dict] = []
    for it in items:
        pn = pick_part_number(it, prefer)
        payload.append(
            {
                "RequestedPartNumber": pn,
                "OriginalPartNumber": pn,
                "Quantities": [{"Quantity": int(it.get("qty") or 0)}],
                "CustomerReference": it.get("refs") or "",
                "Notes": "",
            }
        )
    return payload


def _extract_list_id(create_response_text: str) -> Optional[str]:
    """Best-effort extract of the list id from CreateList's response body.

    DigiKey's response shape is sparsely documented; the field has been
    observed in community references as a bare JSON string, as
    {"ListId": "..."}, or as {"Id": "..."}. We accept all three plus
    lowercase variants and integer ids (coerced to str). Callers tag the
    result `[unverified-on-target]` until exercised against live credentials.
    """
    try:
        result = json.loads(create_response_text)
    except ValueError:
        return None
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, dict):
        for key in ("ListId", "Id", "listId", "id"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, int):
                return str(v)
    return None


def push_authenticated(
    items: Iterable[Dict],
    list_name: str,
    *,
    access_token: str,
    client_id: str,
    environment: str = "production",
    tags: Sequence[str] = (),
    prefer: str = "mpn",
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """POST a BOM to the authenticated MyLists v1 API.

    Two-step flow:
        1. POST /mylists/v1/lists                  -> create empty list
        2. POST /mylists/v1/lists/{listId}/parts   -> attach parts array

    Returns (list_id, list_url, error). On success error is None; on failure
    list_id / list_url may still be set if step 1 succeeded but step 2 didn't,
    which lets the user salvage the empty list.

    Note: `list_url` is a heuristic — DigiKey doesn't return a canonical web
    URL from these endpoints. We synthesise the conventional
    https://www.digikey.com/en/mylists/list/{list_id} form when we have an id.
    """
    items_list = list(items)
    base = _auth_api_base(environment)
    headers = _auth_headers(client_id, access_token)
    poster = session.post if session is not None else requests.post

    # Step 1: CreateList. Body uses PascalCase per the DigiKey forum example
    # and the zenn-articles working impl.
    create_body: Dict = {"ListName": list_name, "Source": "b2b"}
    if tags:
        create_body["Tags"] = list(tags)

    create_url = base + AUTH_LISTS_PATH
    log.debug("POST %s body=%r", create_url, create_body)
    try:
        resp = poster(create_url, json=create_body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return None, None, f"network error on CreateList: {e}"
    if resp.status_code not in (200, 201):
        return None, None, (f"CreateList HTTP {resp.status_code}: {resp.text[:400]}")

    list_id = _extract_list_id(resp.text)
    if not list_id:
        return (
            None,
            None,
            (
                f"CreateList succeeded ({resp.status_code}) but no list id was found "
                f"in the response: {resp.text[:400]}"
            ),
        )

    list_url = f"https://www.digikey.com/en/mylists/list/{list_id}"

    # Step 2: AddPartsToListId.
    parts_body = build_auth_part_payload(items_list, prefer=prefer)
    parts_url = f"{base}{AUTH_LISTS_PATH}/{list_id}/parts"
    log.debug("POST %s rows=%d", parts_url, len(parts_body))
    try:
        resp2 = poster(
            parts_url,
            json=parts_body,
            headers=headers,
            params={"index": "0"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        return list_id, list_url, f"network error on AddPartsToListId: {e}"
    if resp2.status_code not in (200, 201, 204):
        return list_id, list_url, (f"AddPartsToListId HTTP {resp2.status_code}: {resp2.text[:400]}")

    return list_id, list_url, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def derive_list_name(path: str) -> str:
    """Default list name when --list-name is not provided."""
    base = os.path.basename(path).rsplit(".", 1)[0]
    return f"{base}-{datetime.now().strftime('%Y%m%d-%H%M')}"


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="altium-push-to-digikey",
        description="Push an Altium-emitted BOM to a DigiKey myLists / cart.",
        epilog="API endpoint: " + API_URL + " (anonymous).",
    )
    ap.add_argument("bom", help="Path to bom.json (review-pack) or bom.csv (raw Altium export).")
    ap.add_argument(
        "--list-name",
        default=None,
        help="DigiKey list name (default: derived from BOM filename + timestamp).",
    )
    ap.add_argument(
        "--tags", default="", help="Comma-separated tags to attach to the list on DigiKey."
    )
    ap.add_argument(
        "--scale",
        type=int,
        default=1,
        metavar="N",
        help="Multiply every row quantity by N (e.g. --scale 10 for 10-board build). Default 1.",
    )
    ap.add_argument(
        "--prefer",
        choices=["mpn", "dkpn"],
        default="mpn",
        help="When the BOM has both MPN and DigiKey-PN, which to send as the search key. "
        "Default 'mpn' (matches the KiCad-Push-to-DigiKey reference); 'dkpn' uses "
        "the DigiKey-PN when present and falls back to MPN otherwise.",
    )
    ap.add_argument(
        "--aggregate",
        dest="aggregate",
        action="store_true",
        default=True,
        help="Merge rows that share an MPN (sum qty, concat designators). Default: on.",
    )
    ap.add_argument(
        "--no-aggregate",
        dest="aggregate",
        action="store_false",
        help="Disable MPN aggregation; send rows as-is from the BOM.",
    )
    ap.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="On success, write the returned short URL (plain text) to PATH.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the BOM and print the payload without POSTing.",
    )
    ap.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="On success, open the returned short URL in the default browser.",
    )
    ap.add_argument(
        "--no-warn-shareable",
        dest="warn_shareable",
        action="store_false",
        default=True,
        help="Suppress the default warning that the returned URL is link-shareable.",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="HTTP timeout in seconds for the POST. Default 30.",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose logging (DEBUG level to stderr)."
    )
    ap.add_argument(
        "--auth",
        action="store_true",
        help="Authenticated direct-to-account mode: push to your DigiKey myLists via "
        "OAuth2 instead of the anonymous link-shareable endpoint. Requires a one-time "
        "`altium-digikey-auth login`. The list lands directly under your account, no "
        "public URL is generated.",
    )
    ap.add_argument(
        "--auth-environment",
        choices=["production", "sandbox"],
        default=None,
        help="Override the OAuth environment for this run (default: from saved "
        "credentials, or `production`). Only meaningful with --auth.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _build_arg_parser()
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    items = load_bom(args.bom)

    if args.aggregate:
        before = len(items)
        items = aggregate_by_mpn(items)
        if len(items) != before:
            log.debug("Aggregated %d rows -> %d unique MPNs", before, len(items))

    if args.scale != 1:
        items = scale_quantities(items, args.scale)

    if not items:
        sys.exit(
            "ERROR: no parseable rows found in BOM.\n"
            "  Check that the file has an MPN column, a positive Quantity, and "
            "rows are not all marked DNP."
        )

    list_name = args.list_name or derive_list_name(args.bom)
    total_qty = sum(int(it["qty"]) for it in items)

    print(f"Loaded {len(items)} part rows ({total_qty} total units) from {args.bom}")
    print(f"List name: {list_name}")
    if args.tags:
        print(f"Tags: {args.tags}")
    if args.scale != 1:
        print(f"Scale: x{args.scale}")
    if args.prefer == "dkpn":
        n_dkpn = sum(1 for it in items if (it.get("dkpn") or "").strip())
        print(f"Prefer: DKPN ({n_dkpn}/{len(items)} rows have a DKPN; rest fall back to MPN)")

    if args.dry_run:
        print("\n--- DRY RUN (no HTTP POST) ---")
        if args.auth:
            print(f"# Endpoint: {AUTH_API_BASE_PROD}{AUTH_LISTS_PATH} (CreateList)")
            print("# CreateList body:")
            create_body = {"ListName": list_name, "Source": "b2b"}
            if args.tags:
                create_body["Tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
            print(json.dumps(create_body, indent=2))
            print("\n# AddPartsToListId body (POST /lists/{listId}/parts?index=0):")
            print(json.dumps(build_auth_part_payload(items, prefer=args.prefer), indent=2))
        else:
            print(json.dumps(build_payload(items, prefer=args.prefer), indent=2))
        return 0

    if args.auth:
        return _run_auth_mode(args, items, list_name)

    print(f"\nPOSTing to {API_URL} ...")
    short_url, err = push(items, list_name, args.tags, timeout=args.timeout, prefer=args.prefer)
    if err:
        sys.exit(f"ERROR: {err}")

    print(f"\nSuccess. List URL:\n  {short_url}\n")
    print("Open the URL in a browser to land the list in your DigiKey myLists,")
    print("then click 'Add to Cart' on the DigiKey site to convert.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(short_url + "\n")
        print(f"\nWrote URL to {args.out}")

    if args.warn_shareable:
        print(
            "\n"
            "  --- LINK-SHAREABLE WARNING ---\n"
            "  The endpoint used is anonymous: anyone with the URL above can view\n"
            "  (and import) this parts list until you claim it to your account by\n"
            "  opening it in a logged-in browser session. Do NOT paste it into\n"
            "  Slack, email, or any public channel if the BOM is sensitive.\n"
            "  Pass --no-warn-shareable to suppress this notice. For private push\n"
            "  directly to your DigiKey account, use --auth.\n"
        )

    if args.open_browser:
        webbrowser.open(short_url)

    return 0


def _run_auth_mode(args, items: List[Dict], list_name: str) -> int:
    """Authenticated direct-to-account mode (OAuth2 / MyLists v1)."""
    try:
        import digikey_oauth as oauth
    except ImportError as e:
        sys.exit(f"ERROR: --auth requires digikey_oauth.py alongside digikey_push.py ({e}).")

    try:
        # Honor a per-call environment override; otherwise load the saved env.
        if args.auth_environment:
            cfg = oauth.load_config()
            cfg = oauth.OAuthConfig(
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
                redirect_uri=cfg.redirect_uri,
                environment=args.auth_environment,
            )
            access_token, cfg, _tokens = oauth.get_valid_access_token(
                config=cfg, timeout=args.timeout
            )
        else:
            access_token, cfg, _tokens = oauth.get_valid_access_token(timeout=args.timeout)
    except oauth.DigiKeyOAuthError as e:
        sys.exit(f"ERROR: {e}")

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    print(
        f"\nPOSTing to {_auth_api_base(cfg.environment)}{AUTH_LISTS_PATH} (authenticated, {cfg.environment}) ..."
    )
    list_id, list_url, err = push_authenticated(
        items,
        list_name,
        access_token=access_token,
        client_id=cfg.client_id,
        environment=cfg.environment,
        tags=tags,
        prefer=args.prefer,
        timeout=args.timeout,
    )
    if err:
        if list_id:
            print(f"\nPartial success: empty list '{list_id}' created but parts step failed.")
            print(f"  List URL (heuristic): {list_url}")
        sys.exit(f"ERROR: {err}")

    print("\nSuccess. List landed directly in your DigiKey account.")
    print(f"  ListId: {list_id}")
    print(f"  URL (heuristic): {list_url}")
    print(
        "\n  This list is NOT link-shareable — it's bound to your authenticated\n"
        "  account. Convert to a cart from your DigiKey myLists page."
    )

    if args.out and list_url:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(list_url + "\n")
        print(f"\nWrote URL to {args.out}")

    if args.open_browser and list_url:
        webbrowser.open(list_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
