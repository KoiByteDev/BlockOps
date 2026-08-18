@echo off
setlocal
cd /d "%~dp0"
set "TOOLS=%~dp0.blockops-tools"
set "UV=%TOOLS%\uv.exe"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

echo BlockOps setup
echo ==============
echo This installs a private Python runtime inside BlockOps. Administrator access is not required.
echo.

if not exist "%UV%" (
  echo [1/3] Downloading the trusted uv bootstrap tool from Astral...
  set "UV_UNMANAGED_INSTALL=%TOOLS%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/0.12.1/install.ps1 | iex"
  if errorlevel 1 goto :failed
) else (
  echo [1/3] Bootstrap tool is ready.
)

echo [2/3] Preparing private Python 3.12...
if not exist "%PYTHON%" (
  "%UV%" venv --python 3.12 "%~dp0.venv"
  if errorlevel 1 goto :failed
)

echo [3/3] Checking the app...
"%PYTHON%" -c "import dashboard, server_manager; assert dashboard.WEB_ROOT.is_dir(), 'dashboard_web is missing'"
if errorlevel 1 goto :failed

echo.
echo Setup complete. You can use BlockOps.bat from now on.
if /I not "%~1"=="--no-launch" (
  echo BlockOps is opening in your browser.
  start "BlockOps" "%PYTHON%" "%~dp0dashboard.py"
)
exit /b 0

:failed
echo.
echo Setup could not finish. Make sure the ZIP was fully extracted, then run setup.bat again.
echo If you used Git, pull the latest changes before retrying.
echo If Windows showed a security prompt, allow PowerShell to download uv from astral.sh.
pause
exit /b 1
