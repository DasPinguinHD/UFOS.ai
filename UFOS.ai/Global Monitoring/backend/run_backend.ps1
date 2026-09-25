#!/usr/bin/env pwsh
<#
  run_backend.ps1
  - creates virtualenv in backend/.venv (if missing)
  - installs requirements
  - starts the backend (main.py)

  Mirrors LiveStreamAgent/backend/run_backend.ps1 (same venv-setup-and-run
  shape), including the "already running" early exit — MainWindow.xaml.cs
  starts this automatically when the Global Monitoring window opens, so if
  a backend from a previous run (or a manual `python main.py`) is still up,
  this just prepares the environment and exits instead of double-starting it.
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

# If a backend is already listening on the health-check port, exit after the
# environment has been prepared so dependency updates are still applied.
try {
	$tnc = Get-Command Test-NetConnection -ErrorAction SilentlyContinue
	if ($tnc) {
		$res = Test-NetConnection -ComputerName 127.0.0.1 -Port 8768 -WarningAction SilentlyContinue
		if ($res -and $res.TcpTestSucceeded) {
			Write-Host "Backend already listening on 127.0.0.1:8768; environment prepared, exiting.";
			exit 0
		}
	}
	else {
		$tcp = New-Object System.Net.Sockets.TcpClient
		$async = $tcp.BeginConnect('127.0.0.1', 8768, $null, $null)
		$ok = $async.AsyncWaitHandle.WaitOne(200)
		if ($ok) {
			try { $tcp.EndConnect($async) } catch { }
			Write-Host "Backend already listening on 127.0.0.1:8768; environment prepared, exiting.";
			exit 0
		}
	}
} catch { }

Write-Host "Starting backend (main.py)..."
python main.py
