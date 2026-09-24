# Run the ECMWF Kalman pipeline on a folder of ensemble files (Windows).
#
#   .\run_desktop.ps1 -DataDir "C:\Users\me\Desktop\ECMWF"
#   .\run_desktop.ps1 -DataDir "D:\ecmwf" -Cities nyc,chicago -Extra --param,mx2t6
#
# First run creates .venv and installs requirements.txt.
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$OutDir = (Join-Path $PSScriptRoot "desktop_output"),
    [string]$Cities = "nyc,chicago,miami,dallas,seattle,atlanta",
    [string[]]$Extra = @()
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}
& $python -m ecmwf_kf.desktop --data-dir $DataDir --out-dir $OutDir --cities $Cities @Extra
exit $LASTEXITCODE
