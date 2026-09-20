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
	pip install -r requirements.txt --upgrade
}

# If another backend is already listening on the websocket port, exit after the environment
# has been prepared so dependency updates are still applied.
try {
	# prefer Test-NetConnection when available
	$tnc = Get-Command Test-NetConnection -ErrorAction SilentlyContinue
	if ($tnc) {
		$res = Test-NetConnection -ComputerName 127.0.0.1 -Port 8765 -WarningAction SilentlyContinue
		if ($res -and $res.TcpTestSucceeded) {
			Write-Host "Backend listener already present on 127.0.0.1:8765; environment prepared, exiting.";
			exit 0
		}
	}
	else {
		# fallback to .NET TcpClient quick connect attempt
		$tcp = New-Object System.Net.Sockets.TcpClient
		$async = $tcp.BeginConnect('127.0.0.1', 8765, $null, $null)
		$ok = $async.AsyncWaitHandle.WaitOne(200)
		if ($ok) {
			try { $tcp.EndConnect($async) } catch { }
			Write-Host "Backend listener already present on 127.0.0.1:8765; environment prepared, exiting.";
			exit 0
		}
	}
} catch { }

Write-Host "Starting backend (main.py)..."
python main.py
