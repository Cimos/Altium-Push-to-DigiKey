# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - unreleased

### Added
- `--scale N` flag — multiply every row's quantity by `N`, useful for pushing
  enough parts for an `N`-board build.
- `--prefer mpn|dkpn` flag — select whether to send the MPN (default, matches
  KiCad-Push-to-DigiKey reference) or the DigiKey-PN as the search key. DKPN
  mode falls back to MPN when no DKPN is available.
- `--aggregate / --no-aggregate` flags — merge BOM rows that share an MPN
  (sum quantities, concatenate designators). Default on.
- `--out PATH` flag — write the returned short URL to a file, for CI workflows.
- `--timeout SECONDS` flag — configurable HTTP timeout.
- `--verbose` / `-v` flag — DEBUG-level logging to stderr.
- `--version` flag.
- Detection of `DigiKey Part Number` / `DKPN` / `Supplier Part Number 1` CSV
  columns. DKPN from a `Supplier Part Number 1` column is only trusted when the
  matching `Supplier 1` column names DigiKey.
- `dkpn` field passed through from review-pack `bom.json` rows.
- `digikey_push.aggregate_by_mpn()`, `digikey_push.scale_quantities()`,
  `digikey_push.pick_part_number()`, `digikey_push.load_bom()` as importable
  helpers.
- pytest test suite under [tests/](tests/) covering CSV/JSON parsing,
  aggregation, payload shape, and a fully-mocked push path. CI hits no live
  endpoints.
- GitHub Actions CI: lint + tests across Python 3.8 / 3.10 / 3.12 on
  ubuntu-latest and windows-latest.
- `altium/push-bom-to-digikey.ps1` — PowerShell wrapper that resolves a Python
  interpreter, sets up a local `.venv` if `requests` is missing, and invokes
  `digikey_push.py` with normalised flags.
- `altium/README.md` — BOM Output Job column setup, supported header variants,
  and the rationale for not shipping a DelphiScript wrapper today.
- `examples/example-bom.json` — review-pack-shaped sample input.

### Changed
- Quantity parsing is now tolerant of `"3 pcs"`, `"3.0"`, leading/trailing
  whitespace, and float-like strings (was: integer-only).
- Verbose logging is wired through `logging` to stderr; default `print()` output
  on stdout is unchanged.
- Output of run-summary now reports DKPN coverage when `--prefer dkpn` is set.

### Notes
- The wire format and endpoint (`mylists/api/thirdparty`, anonymous POST) are
  unchanged — existing scripts and pipelines that import `push()` /
  `build_payload()` continue to work; new keyword args have safe defaults.

## [0.1.1] - 2026

### Added
- Warn on the link-shareable nature of the returned short URL by default;
  suppress with `--no-warn-shareable`.
- OAuth2 direct-to-account mode sketched in the README under Roadmap.

## [0.1.0] - 2026

### Added
- Initial release: parse Altium BOM (CSV or review-pack JSON), POST to
  DigiKey's anonymous `mylists/api/thirdparty` endpoint, return the short URL.
