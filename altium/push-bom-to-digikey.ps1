<#
.SYNOPSIS
  Push an Altium-emitted BOM (CSV or review-pack bom.json) to a DigiKey list.

.DESCRIPTION
  Thin wrapper around digikey_push.py. Resolves a Python interpreter, ensures
  the `requests` dependency is available (via a local .venv if needed), then
  invokes the script with the supplied arguments.

  Run from the repo root; the script auto-locates digikey_push.py relative to
  itself.

.PARAMETER Bom
  Path to a bom.csv (Altium OutJob export) or bom.json (review-pack).

.PARAMETER ListName
  DigiKey list name. Defaults to the BOM filename + a timestamp.

.PARAMETER Tags
  Comma-separated tags to attach to the list on DigiKey.

.PARAMETER Scale
  Multiply every row quantity by N (e.g. 10 for a 10-board build).

.PARAMETER Prefer
  Which part number to send: 'mpn' (default) or 'dkpn'.

.PARAMETER Out
  On success, write the returned short URL to this file.

.PARAMETER DryRun
  Parse the BOM and print the payload without POSTing.

.PARAMETER Open
  On success, open the returned short URL in the default browser.

.PARAMETER Verbose
  DEBUG-level logging.

.EXAMPLE
  .\push-bom-to-digikey.ps1 -Bom .\bom.csv

.EXAMPLE
  .\push-bom-to-digikey.ps1 -Bom .\review-pack\bom.json -Scale 10 -Open
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Bom,

    [string]$ListName,
    [string]$Tags,
    [int]$Scale = 1,
    [ValidateSet('mpn','dkpn')]
    [string]$Prefer = 'mpn',
    [string]$Out,
    [switch]$DryRun,
    [switch]$Open,
    [switch]$NoAggregate,
    [switch]$NoWarnShareable,
    [int]$Timeout = 30
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Bom)) {
    Write-Error "BOM file not found: $Bom"
    exit 2
}

$repoRoot   = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $repoRoot 'digikey_push.py'
if (-not (Test-Path -LiteralPath $scriptPath)) {
    Write-Error "Could not locate digikey_push.py at $scriptPath"
    exit 2
}

function Resolve-Python {
    $candidates = @('py -3', 'python', 'python3')
    foreach ($c in $candidates) {
        $parts = $c -split ' '
        $exe = $parts[0]
        $rest = $parts[1..($parts.Length - 1)]
        $found = Get-Command $exe -ErrorAction SilentlyContinue
        if ($found) {
            return @{ Exe = $found.Source; Args = $rest }
        }
    }
    return $null
}

$py = Resolve-Python
if (-not $py) {
    Write-Error "No Python interpreter found on PATH (tried: py -3, python, python3)."
    exit 2
}

# Verify `requests` is importable; if not, set up a local .venv to install into.
$venvDir = Join-Path $repoRoot '.venv'
$venvPython = if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    Join-Path $venvDir 'Scripts\python.exe'
} else {
    Join-Path $venvDir 'bin/python'
}

function Test-RequestsAvailable($pyInfo) {
    & $pyInfo.Exe @($pyInfo.Args + @('-c', 'import requests')) 2>$null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-RequestsAvailable $py)) {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "requests not available on system Python; creating venv at $venvDir ..." -ForegroundColor Cyan
        & $py.Exe @($py.Args + @('-m', 'venv', $venvDir))
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to create venv at $venvDir"
            exit 3
        }
    }
    Write-Host "Installing requests into venv ..." -ForegroundColor Cyan
    & $venvPython -m pip install --quiet --disable-pip-version-check requests
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to pip install requests in venv"
        exit 3
    }
    $py = @{ Exe = $venvPython; Args = @() }
}

# Build the argument vector for digikey_push.py
$pyArgs = @($py.Args + @($scriptPath, $Bom))
if ($ListName)        { $pyArgs += @('--list-name', $ListName) }
if ($Tags)            { $pyArgs += @('--tags', $Tags) }
if ($Scale -ne 1)     { $pyArgs += @('--scale', $Scale) }
if ($Prefer)          { $pyArgs += @('--prefer', $Prefer) }
if ($Out)             { $pyArgs += @('--out', $Out) }
if ($DryRun)          { $pyArgs += @('--dry-run') }
if ($Open)            { $pyArgs += @('--open') }
if ($NoAggregate)     { $pyArgs += @('--no-aggregate') }
if ($NoWarnShareable) { $pyArgs += @('--no-warn-shareable') }
if ($Timeout -ne 30)  { $pyArgs += @('--timeout', $Timeout) }
if ($VerbosePreference -eq 'Continue') { $pyArgs += @('--verbose') }

& $py.Exe $pyArgs
exit $LASTEXITCODE
