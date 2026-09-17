#!/usr/bin/env pwsh
<#
  run_backend.ps1
  - creates virtualenv in backend/.venv (if missing)
  - installs requirements
  - starts the backend (main.py)
#>
param(
	[string]$Python = "python",
	[switch]$RecreateVenv
)

Set-Location -Path $PSScriptRoot

if ($RecreateVenv -and (Test-Path .venv)) {
	Write-Host "Removing existing .venv..."
	Remove-Item -Recurse -Force .venv
}

if (-not (Test-Path .venv)) {
	Write-Host "Creating virtual environment .venv..."
	& $Python -m venv .venv
}

Write-Host "Activating virtual environment and installing requirements..."
. .\.venv\Scripts\Activate.ps1
if (Test-Path requirements.txt) {
	pip install -r requirements.txt
}

Write-Host "Starting backend (main.py)..."
python main.py
