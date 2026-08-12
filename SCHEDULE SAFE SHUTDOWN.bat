@echo off
setlocal
cd /d "%~dp0"

set "HOURS=%~1"
if not defined HOURS set /p "HOURS=Hours until the servers should stop (decimals allowed, e.g. 1.5): "

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$text = $env:HOURS; $hours = 0.0;" ^
  "$style = [Globalization.NumberStyles]::AllowDecimalPoint;" ^
  "$culture = [Globalization.CultureInfo]::InvariantCulture;" ^
  "if (-not [double]::TryParse($text, $style, $culture, [ref]$hours) -or $hours -le 0 -or $hours -gt 596523.23) { Write-Host 'Enter a number greater than 0 using a dot for decimals, for example 1.5.' -ForegroundColor Red; exit 2 };" ^
  "$seconds = [long][Math]::Round($hours * 3600, [MidpointRounding]::AwayFromZero);" ^
  "$stopAt = (Get-Date).AddSeconds($seconds);" ^
  "Write-Host ('Servers will be stopped at {0:yyyy-MM-dd HH:mm:ss}.' -f $stopAt) -ForegroundColor Cyan;" ^
  "Write-Host 'Press Ctrl+C before then to cancel this timer.' -ForegroundColor Yellow;" ^
  "Start-Sleep -Seconds $seconds"

if errorlevel 1 goto :failed

echo Requesting a graceful save and stop...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0profile-action.ps1" -Action Stop
if errorlevel 1 goto :stop_failed

echo.
echo Server shutdown completed successfully.
echo Windows will shut down in 5 minutes. Run shutdown.exe /a to cancel it.
shutdown.exe /s /t 300 /c "Minecraft servers saved and stopped. This PC will shut down in 5 minutes."
if errorlevel 1 goto :shutdown_failed
exit /b 0

:stop_failed
echo.
echo The server did not stop cleanly. Windows shutdown was NOT scheduled.
pause
exit /b 1

:shutdown_failed
echo.
echo Windows could not schedule the shutdown.
pause
exit /b 1

:failed
echo.
echo The shutdown timer was cancelled or the entered time was invalid.
pause
exit /b 1
