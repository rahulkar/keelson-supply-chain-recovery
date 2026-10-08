@echo off
rem Keelson + Merxa on Windows WITHOUT Docker - see scripts\native\keelson-native.ps1 for details.
rem
rem   startup.bat                    start everything (first run downloads ~650 MB and builds; later runs ~2-4 min)
rem   startup.bat status             health of every native service
rem   startup.bat logs engine-worker follow one service's log
rem   startup.bat stop               stop all native services (data in .native\data is kept)
rem   startup.bat restart
rem   startup.bat fetch              download, install and build only (no services started)
rem Options: -Yes (answer prompts), -Rebuild (reinstall venvs, rebuild web apps), -SkipWeb, -TimeoutSec N,
rem          -NativeDir C:\path (where tools/venvs/data go; default .native, needs ~6-7 GB; remembered)
setlocal
set "ROOT=%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
  echo ERROR: Windows PowerShell was not found on PATH.
  exit /b 1
)

rem Double-clicked from Explorer (cmd /c ""...startup.bat" "): keep the window open at the end.
rem Delayed expansion: CMDCMDLINE contains quotes that would break a %...% comparison.
setlocal EnableDelayedExpansion
set "_CCL=!CMDCMDLINE!"
set "_CCL2=!_CCL:""=!"
set "_DBL="
if not "!_CCL!"=="!_CCL2!" set "_DBL=1"
endlocal & set "DBLCLICK=%_DBL%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\native\keelson-native.ps1" %*
set "RC=%ERRORLEVEL%"

if defined DBLCLICK (
  echo.
  if "%RC%"=="0" (echo Done.) else (echo Finished with errors ^(exit code %RC%^) - see the log above.)
  pause
)
endlocal & exit /b %RC%
