@echo off
rem TripAgent web UI launcher  (ASCII only + CRLF: cmd.exe parses .bat with the
rem system codepage, so Chinese text here can garble or break parsing.)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [!] Creating project virtual environment .venv ...
  py -3.12 -m venv .venv
  if errorlevel 1 goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
)

set PYTHONIOENCODING=utf-8
echo Starting TripAgent at http://localhost:8501
echo Close this window or press Ctrl+C to stop.
".venv\Scripts\python.exe" -m streamlit run app.py
if errorlevel 1 goto :fail
goto :eof

:fail
echo.
echo [X] Failed to start. Common causes:
echo     - Python 3.12 not installed  (check: py -3.12 --version)
echo     - port 8501 already used     (try:  ... run app.py --server.port 8505)
echo.
pause
