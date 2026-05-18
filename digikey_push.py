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
    1. review-pack `bom.json` -- preferred; canonical normalised form emitted by
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
        print(json.dumps(build_payload(items, prefer=args.prefer), indent=2))
        return 0

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
            "  Pass --no-warn-shareable to suppress this notice.\n"
        )

    if args.open_browser:
        webbrowser.open(short_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
