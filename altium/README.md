# Altium-side setup

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

## Why no `.pas` (DelphiScript) wrapper here

A DelphiScript wrapper that walks the active project's schematics and emits a
CSV directly is appealing in principle, but the Altium API surface for
component iteration (`SchServer` + `IProject` + `DM_PhysicalDocuments` +
schematic component enumeration with variant resolution) is version-sensitive
on AD26 and we won't ship one we haven't bench-verified. The OutJob path above
is the right place to emit the CSV — Altium already implements variant
resolution, supplier-link resolution, and packaging-quantity logic there. The
script consumes whatever Altium writes.

If a project ships an `altium-emit-review-pack.PrjScr`-style emitter that
produces a `bom.json` already, prefer that path.
