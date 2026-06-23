$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path "$Root\api\.venv\Scripts\python.exe")) {
  python -m venv "$Root\api\.venv"
  & "$Root\api\.venv\Scripts\python.exe" -m pip install -r "$Root\api\requirements.txt"
}
if (-not (Test-Path "$Root\web\node_modules")) {
  $Npm = (Get-Command npm.cmd).Source
  & $Npm install --prefix "$Root\web"
}
$Npm = (Get-Command npm.cmd).Source
Start-Process -FilePath "$Root\api\.venv\Scripts\python.exe" -ArgumentList "-m", "uvicorn", "app.main:app", "--reload", "--port", "8000" -WorkingDirectory "$Root\api" -WindowStyle Hidden
Start-Process -FilePath $Npm -ArgumentList "run", "dev", "--prefix", "$Root\web" -WorkingDirectory "$Root\web" -WindowStyle Hidden
Write-Host "Atlas Web: http://localhost:5173"
Write-Host "Atlas API: http://localhost:8000/docs"
