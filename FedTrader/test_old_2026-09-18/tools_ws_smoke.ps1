# archived copy of tools/ws_smoke.ps1
param(
	[string]$Uri = "ws://127.0.0.1:8765",
	[int]$Timeout = 5
)

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Host "python not found in PATH"; exit 2 }

Write-Host "Running ws_smoke.py against $Uri"
& $python .\tools\ws_smoke.py --uri "$Uri" --timeout $Timeout
exit $LASTEXITCODE
