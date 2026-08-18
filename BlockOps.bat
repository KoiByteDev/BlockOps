@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  call "%~dp0setup.bat"
  exit /b %errorlevel%
)
start "BlockOps" "%PYTHON%" "%~dp0dashboard.py"
