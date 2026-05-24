# Altium-side setup

## Recommended path: DelphiScript emitter (one click)

`push-bom-to-digikey.pas` extracts the BOM directly from the focused Altium
project and writes `<project-dir>\digikey-push.csv` in a single step from the
Scripts panel. No OutJob needed, no absolute-path drift, no Generate Outputs
step.

**Setup:**
1. In Altium: `File > Scripts > Open Script Project...` > browse to
   `altium\push-bom-to-digikey.PrjScr` in this repo.
2. Open the `.PrjPcb` you want to emit and make it the focused project.
3. In the Scripts panel (`View > Panels > Scripts`) right-click
   `EmitDigiKeyBOM` and click **Run**.
4. A dialog reports the output path and the exact `altium-push-to-digikey`
   command to run.

**First run:** the script calls `WorkspaceManager:Compile` automatically if
the flattened document is nil. On subsequent runs the already-compiled project
state is reused.

**Probe file:** `push-bom-to-digikey-probe.pas` is a 10-line diagnostic that
tests each API symbol before relying on it. Run it once on a new AD26 install
to confirm the symbols exist. See the probe file header for instructions.

## Manual path (fallback): OutJob + CSV

This script consumes either:

1. A normalised `bom.json` from a CubePilot review-pack pipeline, **or**
2. A raw `bom.csv` exported by an Altium BOM Output Job.

If you already have a review-pack pipeline, option 1 needs no Altium work — point
the script at the `bom.json` and you're done.

For the raw-CSV path (option 2), the script auto-detects columns by header
name; you just need to set up a BOM Output Job that includes the right columns.

## BOM Output Job setup (minimum columns)

In the Output Job's BOM editor, the following columns must be present
(case-insensitive header match; pick any of the supported header names):

| Field | Supported header (any of) |
|---|---|
| MPN (required) | `Manufacturer Part Number 1`, `Manufacturer Part Number`, `MPN`, `Part Number`, `PartNumber`, `ManufacturerPartNumber` |
| Quantity (required) | `Quantity`, `Qty`, `QTY` |
| Designator (optional — becomes "customer reference" on the DigiKey list) | `Designator`, `Designators`, `Reference`, `References`, `RefDes`, `Ref` |
| Supplier name (optional — required only if you want DKPN preference) | `Supplier 1`, `Supplier` |
| DigiKey-PN (optional) | `DigiKey Part Number`, `DigiKey PN`, `DKPN`, `Supplier Part Number 1`, `Supplier Part Number` |

When a `Supplier Part Number 1`-style column is present, the script only treats
it as a DigiKey-PN if the matching `Supplier 1` column names DigiKey
(`digikey` / `digi-key` / `dk`, case-insensitive). This prevents Mouser /
Element14 / TME part numbers from being sent as DigiKey search keys.

Output format: **CSV**. Tab-separated and XLSX also "work" if read with the
right tool, but the script reads CSV (`.csv` extension) — keep it simple.

## Recommended OutJob output container

A typical configuration:

- **Output**: BOM (BomDoc) — configure with the columns above.
- **Container**: Folder Structure, with output path
  `[Project Outputs Folder]\bom.csv` (relative path).
- **Variant**: the same variant you intend to manufacture (Released, etc.).
- **Variations**: if any parts are flagged as "Not Fitted" in the variant, the
  CSV row's Quantity will be 0 and the script will skip the row. No extra
  filtering needed on this side.

## Wrapper: `push-bom-to-digikey.ps1`

After the OutJob has emitted the CSV, run:

```powershell
.\altium\push-bom-to-digikey.ps1 -Bom .\bom.csv
```

The wrapper resolves a Python interpreter (preferring `py -3` on Windows, then
`python`), pip-installs this repo into a local `.venv` on first use, then
invokes `digikey_push.py` with the supplied arguments. Pass anything you'd
normally pass to the script after `-Bom`:

```powershell
.\altium\push-bom-to-digikey.ps1 -Bom .\bom.csv -Scale 10 -Open
```

```powershell
.\altium\push-bom-to-digikey.ps1 -Bom .\review-pack\bom.json -Tags "cuberacer,prototype"
```

## Why the `.pas` emitter is preferred over the OutJob path

The OutJob path works, but has one persistent friction point: Altium embeds
absolute output paths in every OutJob container, so in a team repo every
developer has to re-link the containers after cloning. The `.pas` emitter
derives its output path from the focused project at runtime, so it works
correctly on any machine without manual re-linking.
