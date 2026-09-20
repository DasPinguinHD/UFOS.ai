param(
	[string]$UiExe = '.\UFOS.ai\bin\Debug\net10.0-windows\UFOS.ai.exe',
	[string]$BackendScript = '.\UFOS.ai\LiveStreamAgent\backend\run_backend.ps1',
	[string]$SmokeScript = '.\UFOS.ai\LiveStreamAgent\tools\ws_smoke.ps1'
)

Write-Host "Stopping existing UFOS.ai processes..."
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -eq 'UFOS.ai' } | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

Write-Host "Building UI..."
dotnet build .\UFOS.ai\UFOS.ai.csproj -c Debug | Out-Null

Write-Host "Starting backend..."
Start-Process -FilePath powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File', $BackendScript -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Host "Running smoke test against ws://127.0.0.1:8765"
& $SmokeScript -Uri 'ws://127.0.0.1:8765' -Timeout 5
$rc = $LASTEXITCODE
Write-Host "Smoke exit code: $rc"

if ($rc -ne 0) { Write-Host "Smoke test failed; aborting UI start"; exit $rc }

Write-Host "Starting UI exe..."
Start-Process -FilePath $UiExe -WorkingDirectory (Split-Path $UiExe) -WindowStyle Normal
Start-Sleep -Milliseconds 500

Write-Host "Tailing ws debug log in temp (last 200 lines):"
$log = Join-Path $env:TEMP 'ufos_ai_ws_debug.log'
if (Test-Path $log) { Get-Content -Path $log -Tail 200 } else { Write-Host 'NO LOG FILE' }

Write-Host "Done."
