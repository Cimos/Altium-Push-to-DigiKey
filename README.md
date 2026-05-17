# Altium-Push-to-DigiKey

Push an Altium-emitted BOM straight into a DigiKey myLists / cart from the command line.

Sister project to Digi-Key's [KiCad-Push-to-DigiKey](https://github.com/Digi-Key/KiCad-Push-to-DigiKey) — same destination (DigiKey myLists), different CAD source. CLI-first, scriptable, no GUI, no Altium plugin install required.

## What it does

1. Reads a BOM file emitted by Altium (either the raw CSV from a BOM Output Job, or the normalised JSON from a review-pack pipeline).
2. Tolerantly parses MPN, Quantity, and Designator columns (skips `DNP` rows; skips rows with empty MPN or zero quantity).
3. POSTs the BOM to DigiKey's anonymous `mylists/api/thirdparty` endpoint.
4. Receives back a `https://www.digikey.com/short/<code>` URL.
5. Prints the URL (and optionally opens it). The first time you visit it in a browser, the list lands under your DigiKey account, where you can convert it to a cart with one click.

No DigiKey API credentials, no OAuth, no API key. The endpoint is the same one Digi-Key's official KiCad plugin uses — it's anonymous on the way in, account-tied when you open the returned URL.

## Install

Requires Python 3.8+.

```powershell
git clone https://github.com/Cimos/Altium-Push-to-DigiKey.git
cd Altium-Push-to-DigiKey
pip install -r requirements.txt
```

Or install as a CLI tool directly from GitHub:

```powershell
pip install git+https://github.com/Cimos/Altium-Push-to-DigiKey.git
```

After install, the command `altium-push-to-digikey` is on your PATH.

## Usage

### From a review-pack `bom.json`

```powershell
python digikey_push.py path\to\review-pack\bom.json
```

### From a raw Altium `bom.csv` (BOM Output Job export)

```powershell
python digikey_push.py path\to\bom.csv --list-name "CubeRacer-Rev-B-trial"
```

### Dry-run (parse + show payload, no POST)

```powershell
python digikey_push.py path\to\bom.csv --dry-run
```

### Push and open the resulting URL in your browser

```powershell
python digikey_push.py path\to\bom.csv --open
```

### Add tags to the DigiKey list

```powershell
python digikey_push.py path\to\bom.csv --tags "cuberacer,prototype,batch-1"
```

### Suppress the link-shareable warning (default: on)

By default the script prints a warning after a successful push, reminding you that the returned short URL is link-shareable until you claim it. To suppress for CI / automation:

```powershell
python digikey_push.py path\to\bom.csv --no-warn-shareable
```

## BOM column conventions

For raw Altium CSV, the script auto-detects the column headers (case-insensitive) from these candidates:

| Field | Header (any of) |
|---|---|
| MPN | `Manufacturer Part Number 1`, `Manufacturer Part Number`, `MPN`, `Part Number`, `PartNumber`, `ManufacturerPartNumber` |
| Quantity | `Quantity`, `Qty`, `QTY` |
| Designator (optional, becomes "customer reference" on the list) | `Designator`, `Designators`, `Reference`, `References`, `RefDes`, `Ref` |

If your Output Job uses different headers, you have two options:

1. Edit the Output Job in Altium to use one of the supported headers (cleanest).
2. Edit `DEFAULT_MPN_COLS` / `DEFAULT_QTY_COLS` / `DEFAULT_REF_COLS` near the top of `digikey_push.py`.

For review-pack `bom.json`, the schema is fixed — see the file's docstring for the expected fields.

## Output

```
Loaded 247 part rows (1842 total units) from bom.json
List name: bom-20260515-1422

POSTing to https://www.digikey.com/mylists/api/thirdparty ...

Success. List URL:
  https://www.digikey.com/short/0p5g3hx

Open the URL in a browser to land the list in your DigiKey myLists,
then click 'Add to Cart' on the DigiKey site to convert.
```

## Limitations & gotchas

- **Anonymous endpoint** — there is no per-user authentication on the POST. The returned short URL is bound to whoever opens it (in their browser session). Don't paste that URL into anywhere public if you'd rather not share the parts list.
- **No NDAA / compliance filter** — this tool is a dumb pipe. Compliance is your problem upstream of running it.
- **DigiKey-side errors are pass-through** — if DigiKey doesn't recognise an MPN, the list still lands but that row shows as "no match" on DigiKey's side. The script doesn't pre-validate against DigiKey's product catalogue.
- **No price / stock check** — same reason; the script just builds the list, DigiKey does the rest.
- **Endpoint stability** — `mylists/api/thirdparty` is the endpoint Digi-Key's own KiCad plugin uses, but it's not formally documented as a public API. If Digi-Key changes it, this script breaks. Watch for HTTP 404 / unexpected-response errors as a leading indicator.

## Roadmap / future work

### Authenticated direct-to-account mode (OAuth2)

The current anonymous endpoint is convenient — zero setup, works on any machine — but the trade-off is that the returned short URL is link-shareable. Some users (particularly those handling commercially sensitive BOMs, or working under NDA / ITAR / export-controlled programmes) will reasonably prefer that their BOM never sits on a shareable URL, even briefly.

A second mode is feasible via DigiKey's full Developer API:

- **Endpoints**: `https://api.digikey.com/v1/oauth2/authorize` + `https://api.digikey.com/v1/oauth2/token` (production), or `sandbox-api.digikey.com` for testing.
- **Flow**: 3-legged authorization code. One-time browser interaction to authorize the script against the user's DigiKey account; refresh token cached locally for indefinite headless reuse (DigiKey refresh tokens don't expire and are rotated on every refresh).
- **Result**: list lands **directly** in the user's DigiKey account — no public short URL, no browser-claim step.

Prerequisites (user-side, one-off per machine / account):

1. Register an application at https://developer.digikey.com.
2. Subscribe the app to the MyLists API product.
3. Set a redirect URI (convention: `http://localhost:8139/digikey_callback` — matches the `digikey-api` PyPI package so other CubePilot tooling stays consistent).
4. Note `client_id` + `client_secret`; pass to the script via env vars or local config file.

Implementation sketch:

- `--auth` flag (or auto-detect if credentials are present) selects authenticated mode; the existing anonymous endpoint remains the default for zero-setup use.
- Credentials cached in `%APPDATA%\altium-push-to-digikey\credentials.json` (Windows) or `~/.config/altium-push-to-digikey/credentials.json` (POSIX), with restrictive file permissions where the OS supports them.
- The authenticated CreateList endpoint shape on `api.digikey.com` is not in DigiKey's public docs — the Swagger / OpenAPI spec is gated behind login on the developer portal. Implementing this needs that spec or a verified example POST.

Status: deferred. The current anonymous flow covers the zero-setup case well; the authenticated mode is queued for when (a) a customer or use-case demands it, and (b) the DigiKey developer-portal Swagger has been retrieved.

## Credits

- API endpoint, payload schema, and short-URL response convention reverse-engineered from [Digi-Key/KiCad-Push-to-DigiKey](https://github.com/Digi-Key/KiCad-Push-to-DigiKey) (MIT licensed). Thanks to Digi-Key for keeping the API open and the source freely available.

## License

MIT — see [LICENSE](LICENSE).
