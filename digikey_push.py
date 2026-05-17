#!/usr/bin/env python3
"""Push an Altium-emitted BOM into a DigiKey myLists / cart.

Usage:
    altium-push-to-digikey <bom.json|bom.csv> [--list-name NAME] [--tags TAGS]
                                              [--dry-run] [--open]

Endpoint used: https://www.digikey.com/mylists/api/thirdparty (POST, anonymous).
The endpoint returns a short URL of the form https://www.digikey.com/short/<code>
which the user opens in a browser to land the list under their DigiKey account.
From there the list can be converted to a cart with one click.

API surface discovered from:
    https://github.com/Digi-Key/KiCad-Push-to-DigiKey (MIT licensed)

Input formats:
    1. review-pack `bom.json` — preferred; canonical normalised form emitted by
       Altium-emit-review-pack scripts and CubePilot production-agents pipeline.
       Schema: { "rows": [ { "mpn": str, "quantity": int, "ref_des": [str, ...],
                              "dnp": bool, ... }, ... ] }
    2. Altium raw `bom.csv` — direct CSV export from Altium's BOM Output Job.
       Auto-detects MPN, Quantity, and Designator columns by header name
       (tolerant of common variants — see DEFAULT_*_COLS below).
"""

import argparse
import csv
import json
import os
import re
import sys
import webbrowser
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit(
        "ERROR: the `requests` library is required.\n"
        "Install with: pip install requests\n"
        "or: pip install -r requirements.txt"
    )


API_URL = "https://www.digikey.com/mylists/api/thirdparty"
SHORT_URL_RE = re.compile(
    r"^https?://(www\.)?digikey\.com/short/[0-9a-z]+", re.IGNORECASE
)

DEFAULT_MPN_COLS = [
    "Manufacturer Part Number 1",
    "Manufacturer Part Number",
    "MPN",
    "Part Number",
    "PartNumber",
    "ManufacturerPartNumber",
]
DEFAULT_QTY_COLS = ["Quantity", "Qty", "QTY"]
DEFAULT_REF_COLS = [
    "Designator",
    "Designators",
    "Reference",
    "References",
    "RefDes",
    "Ref",
]


def _find_col(cols, candidates):
    """Return the actual header in `cols` that matches any candidate (case-insensitive)."""
    lookup = {c.lower(): c for c in cols if c is not None}
    for cand in candidates:
        if cand.lower() in lookup:
            return lookup[cand.lower()]
    return None


def load_bom_json(path):
    """Load review-pack bom.json. Returns list of {mpn, qty, refs}."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    items = []
    for row in data.get("rows", []):
        if row.get("dnp"):
            continue
        mpn = (row.get("mpn") or "").strip()
        if not mpn:
            continue
        try:
            qty = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        refs = ", ".join(row.get("ref_des") or [])
        items.append({"mpn": mpn, "qty": qty, "refs": refs})
    return items


def load_bom_csv(path):
    """Load an Altium-style BOM CSV. Auto-detects MPN / Quantity / Designator columns."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = reader.fieldnames or []

    if not rows:
        return []

    mpn_col = _find_col(cols, DEFAULT_MPN_COLS)
    qty_col = _find_col(cols, DEFAULT_QTY_COLS)
    ref_col = _find_col(cols, DEFAULT_REF_COLS)

    if not mpn_col or not qty_col:
        raise SystemExit(
            f"ERROR: could not locate MPN and/or Quantity columns in BOM CSV.\n"
            f"  Available columns: {cols}\n"
            f"  Expected MPN column from: {DEFAULT_MPN_COLS}\n"
            f"  Expected Quantity column from: {DEFAULT_QTY_COLS}"
        )

    items = []
    for r in rows:
        mpn = (r.get(mpn_col) or "").strip()
        if not mpn:
            continue
        try:
            qty = int(r.get(qty_col) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        refs = (r.get(ref_col) or "").strip() if ref_col else ""
        items.append({"mpn": mpn, "qty": qty, "refs": refs})
    return items


def build_payload(items):
    """Build the JSON body for POST /mylists/api/thirdparty.

    Schema (from DigiKey KiCad reference):
        [ { "requestedPartNumber": <MPN>,
            "quantities": [ {"quantity": <int>} ],
            "customerReference": <designators or empty>,
            "notes": <string or empty> }, ... ]
    """
    return [
        {
            "requestedPartNumber": it["mpn"],
            "quantities": [{"quantity": it["qty"]}],
            "customerReference": it["refs"],
            "notes": "",
        }
        for it in items
    ]


def push(items, list_name, tags="", timeout=30):
    """POST the BOM to DigiKey's thirdparty endpoint. Returns (short_url, error)."""
    payload = build_payload(items)
    params = {"listName": list_name}
    if tags:
        params["tags"] = tags
    try:
        resp = requests.post(
            API_URL, json=payload, params=params, verify=True, timeout=timeout
        )
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


def derive_list_name(path):
    """Default list name when --list-name is not provided."""
    base = os.path.basename(path).rsplit(".", 1)[0]
    return f"{base}-{datetime.now().strftime('%Y%m%d-%H%M')}"


def main():
    ap = argparse.ArgumentParser(
        description="Push an Altium-emitted BOM to a DigiKey myLists / cart.",
        epilog="API endpoint: " + API_URL + " (anonymous).",
    )
    ap.add_argument(
        "bom",
        help="Path to bom.json (review-pack normalised form) or bom.csv (raw Altium export).",
    )
    ap.add_argument(
        "--list-name",
        default=None,
        help="DigiKey list name (default: derived from BOM filename + timestamp).",
    )
    ap.add_argument(
        "--tags",
        default="",
        help="Comma-separated tags to attach to the list on DigiKey.",
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
    args = ap.parse_args()

    path_lower = args.bom.lower()
    if path_lower.endswith(".json"):
        items = load_bom_json(args.bom)
    elif path_lower.endswith(".csv"):
        items = load_bom_csv(args.bom)
    else:
        sys.exit("ERROR: BOM file must end in .json (review-pack) or .csv (Altium export).")

    if not items:
        sys.exit(
            "ERROR: no parseable rows found in BOM.\n"
            "  Check that the file has an MPN column, a positive Quantity, and "
            "rows are not all marked DNP."
        )

    list_name = args.list_name or derive_list_name(args.bom)
    total_qty = sum(it["qty"] for it in items)

    print(f"Loaded {len(items)} part rows ({total_qty} total units) from {args.bom}")
    print(f"List name: {list_name}")
    if args.tags:
        print(f"Tags: {args.tags}")

    if args.dry_run:
        print("\n--- DRY RUN (no HTTP POST) ---")
        print(json.dumps(build_payload(items), indent=2))
        return 0

    print(f"\nPOSTing to {API_URL} ...")
    short_url, err = push(items, list_name, args.tags)
    if err:
        sys.exit(f"ERROR: {err}")

    print(f"\nSuccess. List URL:\n  {short_url}\n")
    print("Open the URL in a browser to land the list in your DigiKey myLists,")
    print("then click 'Add to Cart' on the DigiKey site to convert.")

    if args.open_browser:
        webbrowser.open(short_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
