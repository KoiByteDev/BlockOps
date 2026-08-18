@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo BlockOps has not been set up yet. Starting setup...
  call "%~dp0setup.bat" --no-launch
  if errorlevel 1 exit /b 1
)
"%PYTHON%" "%~dp0server_manager.py" %*
