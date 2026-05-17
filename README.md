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

## Credits

- API endpoint, payload schema, and short-URL response convention reverse-engineered from [Digi-Key/KiCad-Push-to-DigiKey](https://github.com/Digi-Key/KiCad-Push-to-DigiKey) (MIT licensed). Thanks to Digi-Key for keeping the API open and the source freely available.

## License

MIT — see [LICENSE](LICENSE).
