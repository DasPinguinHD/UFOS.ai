#!/usr/bin/env pwsh
Write-Host "Untracking and removing backend .venv from repository and disk"
$venvPath = "FedTrader/backend/.venv"
if (Test-Path $venvPath) {
	git rm -r --cached $venvPath 2>$null
	Remove-Item -Recurse -Force $venvPath
	Write-Host ".venv removed from disk and unstaged from git index."
} else {
	Write-Host ".venv not found at $venvPath"
}
Write-Host "Please commit the change: git add . && git commit -m 'Remove backend .venv from repo and ignore'"
