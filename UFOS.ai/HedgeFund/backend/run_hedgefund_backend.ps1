param(
	[string]$Python = "python",
	[switch]$RecreateVenv
)

Set-Location -Path $PSScriptRoot

if ($RecreateVenv -and (Test-Path .venv)) {
	Remove-Item -Recurse -Force .venv
}

if (-not (Test-Path .venv)) {
	& $Python -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt --upgrade

python main.py
