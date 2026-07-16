@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-agentgateway.ps1" %*
