param([switch]$Restart)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ModuleRoot = Join-Path $Root 'modules\agentgateway'
$BackendRoot = Join-Path $ModuleRoot 'backend'
$FrontendRoot = Join-Path $ModuleRoot 'frontend'
$Python = Join-Path $BackendRoot '.venv\Scripts\python.exe'
$Npm = (Get-Command npm.cmd).Source

function Get-ListeningPid([int]$Port) {
  $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($connection) {
    return $connection.OwningProcess
  }

  return $null
}

function Stop-GatewayPort([int]$Port) {
  $ownerPid = Get-ListeningPid $Port
  if ($ownerPid) { Stop-Process -Id $ownerPid -Force }
}

if (-not (Test-Path $Python)) {
  python -m venv (Join-Path $BackendRoot '.venv')
}

& $Python -c 'import fastapi' 2>$null
if ($LASTEXITCODE -ne 0) {
  & $Python -m pip install -e "$BackendRoot[dev]"
}

if (-not (Test-Path (Join-Path $FrontendRoot 'node_modules'))) {
  & $Npm ci --prefix $FrontendRoot
}

if ($Restart) {
  Stop-GatewayPort 8100
  Stop-GatewayPort 3100
}

if (-not (Get-ListeningPid 8100)) {
  Start-Process -FilePath $Python -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8100' -WorkingDirectory $BackendRoot -WindowStyle Hidden
}

if (-not (Get-ListeningPid 3100)) {
  Start-Process -FilePath $Npm -ArgumentList 'run', 'dev', '--', '--hostname', '127.0.0.1', '--port', '3100' -WorkingDirectory $FrontendRoot -WindowStyle Hidden
}

Write-Host 'AgentGateway workbench: http://127.0.0.1:3100/workbench'
