@echo off
rem TripAgent CLI launcher. Usage: run_cli.bat plan "Chengdu 3 days"
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [X] .venv not found. Run run_web.bat once to create it, or:
  echo     py -3.12 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  goto :eof
)

if "%~1"=="" (
  echo Usage examples:
  echo   run_cli.bat plan "Chengdu 3 days budget 800"
  echo   run_cli.bat ask  "Chengdu nice quiet bars"
  echo   run_cli.bat weather Harbin
  echo   run_cli.bat doctor
  echo   run_cli.bat demo
  goto :eof
)

".venv\Scripts\python.exe" -m tripagent.cli %*
