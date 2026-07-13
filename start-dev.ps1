param(
  [switch]$Restart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ApiPython = "$Root\api\.venv\Scripts\python.exe"
$Npm = (Get-Command npm.cmd).Source

function Get-ListeningPid([int]$Port) {
  $line = netstat -ano | Select-String "127\.0\.0\.1:$Port\s+.*LISTENING|0\.0\.0\.0:$Port\s+.*LISTENING" | Select-Object -First 1
  if (-not $line) { return $null }
  $parts = ($line.ToString() -split '\s+') | Where-Object { $_ }
  return [int]$parts[-1]
}

function Stop-AtlasPort([int]$Port, [string[]]$AllowedProcessNames) {
  $ownerPid = Get-ListeningPid $Port
  if (-not $ownerPid) { return }
  $process = Get-Process -Id $ownerPid -ErrorAction Stop
  if ($process.ProcessName -notin $AllowedProcessNames) {
    throw "Port $Port is owned by non-Atlas process $($process.ProcessName); it was not stopped."
  }
  Stop-Process -Id $ownerPid -Force
  Write-Host "Stopped $($process.ProcessName) on port $Port"
}

if (-not (Test-Path $ApiPython)) {
  python -m venv "$Root\api\.venv"
  & $ApiPython -m pip install -r "$Root\api\requirements.txt"
}

if (-not (Test-Path "$Root\web\node_modules")) {
  & $Npm install --prefix "$Root\web"
}

if ($Restart) {
  Stop-AtlasPort 8000 @('python')
  Stop-AtlasPort 5173 @('node')
  Start-Sleep -Milliseconds 500
}

if (-not (Get-ListeningPid 8000)) {
  Start-Process -FilePath $ApiPython -ArgumentList '-m', 'uvicorn', 'app.main:app', '--reload', '--host', '127.0.0.1', '--port', '8000' -WorkingDirectory "$Root\api" -WindowStyle Hidden
}

if (-not (Get-ListeningPid 5173)) {
  Start-Process -FilePath $Npm -ArgumentList 'run', 'dev', '--prefix', "$Root\web" -WorkingDirectory "$Root\web" -WindowStyle Hidden
}

Write-Host "Atlas Web: http://localhost:5173"
Write-Host "Atlas API: http://localhost:8000/docs"
Write-Host "Use .\start-dev.cmd -Restart after backend configuration changes."
