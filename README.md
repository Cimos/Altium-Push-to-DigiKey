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

### Scale quantities (multi-board build)

Multiply every row's quantity by `N` — useful for ordering enough parts for an N-board prototype run:

```powershell
python digikey_push.py path\to\bom.csv --scale 10
```

### Aggregate duplicate-MPN rows (default: on)

If the BOM has the same MPN on multiple rows (typical when designators aren't pre-merged), the script collapses them into one row per unique MPN: quantities are summed and designators are concatenated. Disable with `--no-aggregate` if you specifically want one DigiKey list row per BOM row.

```powershell
python digikey_push.py path\to\bom.csv --no-aggregate
```

### Send DigiKey-PN instead of MPN

By default the script sends the MPN as the search key (matches the KiCad-Push-to-DigiKey reference). To prefer the DigiKey-PN (with automatic fallback to MPN when no DKPN is available for a row):

```powershell
python digikey_push.py path\to\bom.csv --prefer dkpn
```

For raw CSVs, the DigiKey-PN is taken from a `DigiKey Part Number` / `DKPN` column when present, or from a `Supplier Part Number 1` column when the matching `Supplier 1` column names DigiKey. For review-pack JSON, set `"dkpn"` on the row.

### Write the resulting URL to a file (CI workflows)

```powershell
python digikey_push.py path\to\bom.csv --out url.txt
```

### Suppress the link-shareable warning

By default the script prints a warning after a successful push, reminding you that the returned short URL is link-shareable until you claim it. To suppress for CI / automation:

```powershell
python digikey_push.py path\to\bom.csv --no-warn-shareable
```

### Verbose / debug output

```powershell
python digikey_push.py path\to\bom.csv --verbose
```

### From the Altium-side PowerShell wrapper

If you'd rather invoke this from an Altium project root and have the wrapper handle Python interpreter discovery + virtualenv setup, [altium/push-bom-to-digikey.ps1](altium/push-bom-to-digikey.ps1) does that:

```powershell
.\altium\push-bom-to-digikey.ps1 -Bom .\bom.csv -Scale 10 -Open
```

See [altium/README.md](altium/README.md) for OutJob column setup.

### Authenticated direct-to-account mode (`--auth`) — `[unverified-on-target]`

Anonymous mode (default) returns a link-shareable short URL. `--auth` mode uses DigiKey's full Developer API to land the list **directly** in your account — no public URL, no browser-claim step.

> ⚠️ **`[unverified-on-target]`** — the OAuth2 token flow (authorize / exchange / refresh) is built against DigiKey's publicly documented endpoint shape and is fully unit-tested with mocks, but the authenticated CreateList + AddPartsToListId endpoint shape is reconstructed from a community Python reference rather than the gated developer-portal Swagger. **Treat the first successful `--auth` push against your real DigiKey account as a verification step.** If you hit a 4xx, check the response body, then file an issue with the redacted request/response and the request will be patched. The anonymous endpoint (no `--auth` flag) is the well-trodden path and remains the default.

One-time setup (per machine + account):

1. Register an app at https://developer.digikey.com.
2. Subscribe the app to the MyLists API product.
3. Set the OAuth redirect URI to `https://localhost` (no listener needed — see below).
4. Configure local credentials. **Don't pass the secret on the command line** — it would land in shell history and the OS process listing. Run setup interactively so the secret is read with hidden input via `getpass`:
    ```powershell
    altium-digikey-auth setup --client-id <CID>
    # ... prompts for client_secret with hidden input
    ```
    For CI / automation, prefer the env-var form (set in the runner's secret store, not in scripts):
    ```powershell
    $env:DIGIKEY_CLIENT_ID = "<CID>"
    $env:DIGIKEY_CLIENT_SECRET = "<SEC>"
    ```
5. Authorize the app:
    ```powershell
    altium-digikey-auth login
    ```
    Your browser opens the DigiKey authorize URL. After you click **Allow**, the browser is redirected to `https://localhost/?code=...&state=...` and shows "site can't be reached" — that is expected (no local server is listening). Copy the full URL from the address bar and paste it back at the CLI prompt.

Then push:

```powershell
altium-push-to-digikey path\to\bom.csv --auth --list-name "CubeRacer-Rev-C"
```

Other auth subcommands: `altium-digikey-auth status`, `altium-digikey-auth refresh`, `altium-digikey-auth logout`.

Token rotation is automatic — the access token expires every ~30 min and is refreshed transparently using the long-lived (rotating) refresh token. DigiKey rotates the refresh token on every refresh; the new value is persisted atomically.

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

For review-pack `bom.json`, the schema is fixed — see the file's docstring for the expected fields. A worked example is in [examples/example-bom.json](examples/example-bom.json).

## Development

```powershell
git clone https://github.com/Cimos/Altium-Push-to-DigiKey.git
cd Altium-Push-to-DigiKey
pip install -e .[dev]
pytest -v
ruff check .
ruff format --check .
```

The test suite is hermetic — every test that exercises the push path mocks the `requests` layer; no live DigiKey calls happen in CI or local runs.

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
- **`--auth` mode is `[unverified-on-target]`** — see the [Authenticated direct-to-account mode](#authenticated-direct-to-account-mode---auth----unverified-on-target) section. The OAuth2 token flow is exercised in tests against the publicly documented endpoint shapes; the authenticated CreateList + AddPartsToListId calls are reconstructed from a community reference and have not yet been confirmed against a live DigiKey account. Report bench results via GitHub Issues so the wire shape can be locked down.

## Roadmap / future work

- **Verify `--auth` end-to-end against a live DigiKey developer-portal account.** Once the gated Swagger / OpenAPI spec has been retrieved, lock down the CreateList response shape and remove the `[unverified-on-target]` tag.
- **NDAA / ITAR compliance filter.** Today the tool is a dumb pipe; an opt-in compliance pre-filter that flags export-controlled parts before the list is created would be a natural next layer.
- **Stock pre-validation.** A `--check-stock` flag could pre-query DigiKey for stock + lead time and warn before pushing rows with zero availability.

## Credits

- API endpoint, payload schema, and short-URL response convention reverse-engineered from [Digi-Key/KiCad-Push-to-DigiKey](https://github.com/Digi-Key/KiCad-Push-to-DigiKey) (MIT licensed). Thanks to Digi-Key for keeping the API open and the source freely available.

## License

MIT — see [LICENSE](LICENSE).
