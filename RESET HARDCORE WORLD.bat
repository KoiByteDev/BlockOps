@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0profiles\fabric-26-2-hardcore\reset-hardcore-world.ps1"
pause
