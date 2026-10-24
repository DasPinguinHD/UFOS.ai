#!/usr/bin/env pwsh
<#
  run_backend.ps1
  Prepares the Python environment of the Global Monitoring backend and starts it.

  - creates the virtualenv backend/.venv (if missing)
  - installs ONLY from requirements.lock (stage 10, pinned dependencies:
    every package "==" pinned with its sha256; made by make_lockfile.py from
    requirements.txt) with "pip install --require-hashes -r requirements.lock",
    and only when that is required:
      * .venv is missing,
      * requirements.lock differs from the version installed last time
        (SHA-256 recorded in .venv/ufos-requirements.sha256; before the first
        lock: the requirements.txt hash), or
      * the last install did not complete (.venv/ufos-install-pending).
    A missing lock, or one made from an older requirements.txt, refuses the
    install ("UFOS_ENV: failed ..." with the command to run) - never an
    unpinned install.
  - optional upgrades are deliberate: -Upgrade first regenerates the lock
    (make_lockfile.py --update: newest versions requirements.txt allows),
    then installs exactly that lock (the root UFOS.ai app's Settings ->
    "Update Python packages" calls this with -Upgrade -EnvOnly).
  - starts the backend (main.py) unless -EnvOnly is given.
  - -SkipInstall (used by the UFOS.ai app's launchers): no venv creation and
    no pip at all - the app prepares every module's environment once at
    app start (Shared/PythonEnvironment.cs, same stamp/lock protocol) and
    waits for it before starting this script. The lock is still taken, so
    the start waits while an install/update of this venv is running.
    Without -SkipInstall (a developer running the script by hand) the
    required-install behaviour above is unchanged and writes the same stamp,
    so the app does not reinstall afterwards.

  MainWindow.xaml.cs starts this automatically when the Global Monitoring
  window opens; if a backend (previous run or a manual `python main.py`) is
  already listening on 8768, this prepares the environment and exits
  instead of double-starting it (mirrors LiveStreamAgent/backend).

  Locking: every run holds backend/.venv.lock while it checks/changes the
  venv, so a backend never starts while an install/upgrade of the same venv
  is running (the start waits instead). The lock file is opened with
  DeleteOnClose, so Windows removes it even if this process is killed; a
  leftover lock (e.g. after a power cut) counts as stale when its PID is no
  longer alive or it is older than 30 minutes.

  Marker lines for the C# launchers (translated into status text):
    UFOS_ENV: waiting               - another install/update holds the lock
    UFOS_ENV: installing <reason>   - required install running
    UFOS_ENV: upgrading             - optional upgrade running (-Upgrade)
    UFOS_ENV: changed <n>           - after -Upgrade: n packages changed
    UFOS_ENV: failed <reason>       - venv creation / pip failed
    UFOS_ENV: ready                 - environment ready

  Must stay Windows PowerShell 5.1 compatible and ASCII-only (5.1 reads
  BOM-less scripts in the ANSI code page).
#>
param(
	[string]$Python = "python",
	[switch]$RecreateVenv,
	[switch]$Upgrade,
	[switch]$EnvOnly,
	[switch]$SkipInstall
)

Set-Location -Path $PSScriptRoot
# The root app (Settings -> "Update Python packages") runs -EnvOnly, sets
# PYTHONIOENCODING=utf-8 and reads UTF-8. Normal backend starts keep the console
# encoding they always had, so the module launchers' log output doesn't change.
if ($EnvOnly) {
	try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
}

$VenvDir = Join-Path $PSScriptRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $PSScriptRoot 'requirements.txt'
$HashFile = Join-Path $VenvDir 'ufos-requirements.sha256'
$ReqLock = Join-Path $PSScriptRoot 'requirements.lock'
$LockTool = Join-Path $PSScriptRoot 'make_lockfile.py'
$LockToolHint = 'make_lockfile.py'
$PendingFile = Join-Path $VenvDir 'ufos-install-pending'
$LockFile = Join-Path $PSScriptRoot '.venv.lock'
$LockStaleMinutes = 30
$script:LockStream = $null
$script:EnvOk = $false

function Write-EnvMarker([string]$Text) {
	# Well-known marker line, parsed by the C# launchers / root app. Keep the prefix stable.
	Write-Host ("UFOS_ENV: " + $Text)
}

function Get-RequirementsHash {
	# Stage 10 (pinned dependencies): the stamp is the SHA-256 of requirements.lock once it exists. Before the
	# first lock it is the old requirements.txt hash, so an installed environment keeps starting until the lock
	# is made - an install itself always needs the lock (Invoke-EnvSetup).
	if (Test-Path -LiteralPath $ReqLock) { return (Get-FileHash -LiteralPath $ReqLock -Algorithm SHA256).Hash }
	if (-not (Test-Path -LiteralPath $Requirements)) { return 'none' }
	return (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
}

# $null when the lock exists and was made from the current requirements file (its "# source: ... sha256=" line),
# otherwise the reason with the command to run.
function Get-LockProblem([string]$Req, [string]$Lock, [string]$Name) {
	if (-not (Test-Path -LiteralPath $Lock)) {
		return ($Name + ' missing - run once (needs internet): .venv\Scripts\python.exe ' + $LockToolHint)
	}
	$want = (Get-FileHash -LiteralPath $Req -Algorithm SHA256).Hash.ToLowerInvariant()
	$line = Select-String -LiteralPath $Lock -Pattern '^# source: \S+ sha256=([0-9a-f]{64})$' | Select-Object -First 1
	if ($null -eq $line) { return ($Name + ' has no source line - regenerate it: .venv\Scripts\python.exe ' + $LockToolHint) }
	if ($line.Matches[0].Groups[1].Value -ne $want) {
		return ($Name + ' is out of date (requirements changed) - run: .venv\Scripts\python.exe ' + $LockToolHint + ' --update')
	}
	return $null
}

# Returns $null when no install is needed, otherwise a short reason.
function Get-InstallReason {
	if (-not (Test-Path -LiteralPath $VenvPython)) { return 'no .venv yet' }
	if (Test-Path -LiteralPath $PendingFile) { return 'last install did not complete' }
	if (-not (Test-Path -LiteralPath $HashFile)) { return 'requirements not recorded yet' }
	$recorded = ''
	try { $recorded = ([string](Get-Content -LiteralPath $HashFile -Raw -ErrorAction Stop)).Trim() } catch { }
	if ($recorded -ne (Get-RequirementsHash)) { return 'requirements changed' }
	return $null
}

# A lock that can be read is not held open by a live process (the holder keeps it
# open with DeleteOnClose and no FileShare.Delete, so reads fail while it's held).
function Test-LockStale {
	$holder = 0
	try {
		$item = Get-Item -LiteralPath $LockFile -Force -ErrorAction Stop
		$first = Get-Content -LiteralPath $LockFile -TotalCount 1 -ErrorAction Stop
		[void][int]::TryParse(([string]$first).Trim(), [ref]$holder)
	}
	catch { return $false }  # gone (retry) or held open by a live process
	$ageMinutes = ((Get-Date).ToUniversalTime() - $item.LastWriteTimeUtc).TotalMinutes
	if ($ageMinutes -gt $LockStaleMinutes) { return $true }
	if ($holder -le 0) { return ($ageMinutes -gt 1) }
	if ($holder -eq $PID) { return $true }
	return (-not (Get-Process -Id $holder -ErrorAction SilentlyContinue))
}

function Enter-EnvLock([string]$Purpose) {
	$announced = $false
	$errors = 0
	$deadline = (Get-Date).AddMinutes($LockStaleMinutes + 5)
	while ($true) {
		try {
			$fs = New-Object System.IO.FileStream -ArgumentList @($LockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read, 4096, [System.IO.FileOptions]::DeleteOnClose)
			$text = "{0}`r`n{1}`r`n{2}`r`n" -f $PID, (Get-Date).ToUniversalTime().ToString('o'), $Purpose
			$bytes = [System.Text.Encoding]::ASCII.GetBytes($text)
			$fs.Write($bytes, 0, $bytes.Length)
			$fs.Flush()
			$script:LockStream = $fs
			return $true
		}
		catch {
			if (-not (Test-Path -LiteralPath $LockFile)) {
				$errors++
				Write-Host ("Could not create .venv.lock: " + $_.Exception.Message)
				if ($errors -ge 5) { return $false }
				Start-Sleep -Milliseconds 500
				continue
			}
		}
		if (Test-LockStale) {
			Write-Host 'Removing stale .venv.lock (its process is gone or it is older than 30 minutes).'
			try { Remove-Item -LiteralPath $LockFile -Force -ErrorAction Stop } catch { Start-Sleep -Seconds 2 }
			continue
		}
		if (-not $announced) {
			Write-EnvMarker 'waiting'
			Write-Host 'A Python package install/update for this backend is running - waiting for it to finish...'
			$announced = $true
		}
		if ((Get-Date) -gt $deadline) {
			Write-Host 'Gave up waiting for .venv.lock.'
			return $false
		}
		Start-Sleep -Seconds 2
	}
}

function Exit-EnvLock {
	if ($null -ne $script:LockStream) {
		# DeleteOnClose removes the file when the handle closes.
		try { $script:LockStream.Dispose() } catch { }
		$script:LockStream = $null
	}
}

# Sets $script:EnvOk. Native output is piped to Out-Host so it isn't swallowed.
function Invoke-EnvSetup {
	$script:EnvOk = $false
	if ($RecreateVenv -and (Test-Path -LiteralPath $VenvDir)) {
		Write-Host 'Removing existing .venv...'
		try { Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction Stop }
		catch {
			Write-EnvMarker ('failed could not remove .venv (is the backend still running?): ' + $_.Exception.Message)
			return
		}
	}

	$reason = Get-InstallReason
	if ($SkipInstall -and -not $Upgrade -and -not $RecreateVenv) {
		# The UFOS.ai app prepared the environment (Shared/PythonEnvironment.cs) before
		# starting this script - never create the venv or run pip here.
		if ($reason) {
			Write-Host ('-SkipInstall: Python environment not up to date (' + $reason + ') - it is prepared by the UFOS.ai app; starting with the installed packages.')
		}
		else {
			Write-Host 'Python packages are installed and requirements.txt is unchanged - no install needed.'
		}
		$script:EnvOk = (Test-Path -LiteralPath $VenvPython)
		if ($script:EnvOk) { Write-EnvMarker 'ready' }
		else { Write-EnvMarker 'failed no .venv (-SkipInstall) - the UFOS.ai app prepares it' }
		return
	}

	if (-not $Upgrade -and -not $reason) {
		Write-Host 'Python packages are installed and requirements.txt is unchanged - no install needed.'
		Write-EnvMarker 'ready'
		$script:EnvOk = $true
		return
	}

	if ($Upgrade) { Write-EnvMarker 'upgrading' }
	else { Write-EnvMarker ('installing ' + $reason) }

	if (-not (Test-Path -LiteralPath $VenvPython)) {
		Write-Host 'Creating virtual environment .venv...'
		& $Python -m venv $VenvDir | Out-Host
		if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
			Write-EnvMarker ("failed could not create .venv with '" + $Python + "' - is Python installed and on PATH?")
			return
		}
	}

	if (-not (Test-Path -LiteralPath $Requirements)) {
		Write-Host 'No requirements.txt - nothing to install.'
		Set-Content -LiteralPath $HashFile -Value (Get-RequirementsHash) -Encoding Ascii
		if ($Upgrade) { Write-EnvMarker 'changed 0' }
		Write-EnvMarker 'ready'
		$script:EnvOk = $true
		return
	}

	$before = @()
	if ($Upgrade) { $before = @(& $VenvPython -m pip freeze --disable-pip-version-check 2>$null) }

	if ($Upgrade) {
		# "Update Python packages": regenerate the lock deliberately, then install exactly that lock - never
		# "pip install --upgrade" in place (stage 10, pinned dependencies).
		Write-Host 'Regenerating requirements.lock (make_lockfile.py --update, needs internet)...'
		& $VenvPython $LockTool --update | Out-Host
		if ($LASTEXITCODE -ne 0) {
			Write-EnvMarker ('failed could not regenerate requirements.lock (make_lockfile.py exited with code ' + $LASTEXITCODE + ' - offline? see log)')
			return
		}
	}
	$lockProblem = Get-LockProblem $Requirements $ReqLock 'requirements.lock'
	if ($lockProblem) {
		Write-EnvMarker ('failed ' + $lockProblem)
		return
	}

	Set-Content -LiteralPath $PendingFile -Value 'install started' -Encoding Ascii
	$pipArgs = @('-m', 'pip', 'install', '--require-hashes', '-r', $ReqLock, '--disable-pip-version-check', '--no-input', '--progress-bar', 'off')
	Write-Host ('Running: python ' + ($pipArgs -join ' '))
	& $VenvPython @pipArgs | Out-Host
	$pipExit = $LASTEXITCODE
	if ($pipExit -ne 0) {
		Write-EnvMarker ('failed pip install exited with code ' + $pipExit + ' (offline? see log)')
		return
	}

	Set-Content -LiteralPath $HashFile -Value (Get-RequirementsHash) -Encoding Ascii
	Remove-Item -LiteralPath $PendingFile -Force -ErrorAction SilentlyContinue

	if ($Upgrade) {
		$after = @(& $VenvPython -m pip freeze --disable-pip-version-check 2>$null)
		$seen = @{}
		foreach ($line in $before) { $seen[[string]$line] = $true }
		$changed = 0
		foreach ($line in $after) { if (-not $seen.ContainsKey([string]$line)) { $changed++ } }
		Write-EnvMarker ('changed ' + $changed)
	}
	Write-EnvMarker 'ready'
	$script:EnvOk = $true
}

$purpose = 'start'
if ($Upgrade) { $purpose = 'upgrade' }
if (Enter-EnvLock $purpose) {
	try { Invoke-EnvSetup }
	catch {
		Write-EnvMarker ('failed ' + $_.Exception.Message)
		$script:EnvOk = $false
	}
	finally { Exit-EnvLock }
}
else {
	Write-EnvMarker 'failed could not get .venv.lock'
}

if ($EnvOnly) {
	if ($script:EnvOk) { exit 0 }
	exit 1
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
	Write-Host 'No usable .venv - the backend cannot start.'
	exit 1
}
if (-not $script:EnvOk) {
	Write-Host 'WARNING: the Python package install did not complete - starting with the packages already installed (the install is retried on the next start).'
}

# If a backend is already listening on port 8768, exit after the environment
# has been prepared instead of double-starting it.
try {
	$tnc = Get-Command Test-NetConnection -ErrorAction SilentlyContinue
	if ($tnc) {
		$res = Test-NetConnection -ComputerName 127.0.0.1 -Port 8768 -WarningAction SilentlyContinue
		if ($res -and $res.TcpTestSucceeded) {
			Write-Host "Backend already listening on 127.0.0.1:8768; environment prepared, exiting."
			exit 0
		}
	}
	else {
		$tcp = New-Object System.Net.Sockets.TcpClient
		$async = $tcp.BeginConnect('127.0.0.1', 8768, $null, $null)
		$ok = $async.AsyncWaitHandle.WaitOne(200)
		if ($ok) {
			try { $tcp.EndConnect($async) } catch { }
			Write-Host "Backend already listening on 127.0.0.1:8768; environment prepared, exiting."
			exit 0
		}
	}
} catch { }

Write-Host "Starting backend (main.py)..."
. (Join-Path $VenvDir 'Scripts\Activate.ps1')
python main.py
exit $LASTEXITCODE
