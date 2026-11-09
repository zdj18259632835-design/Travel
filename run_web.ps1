# TripAgent web UI launcher (PowerShell). Right-click -> "Run with PowerShell".
# Independent of the .bat file association.
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[!] .venv not found, creating ..." -ForegroundColor Yellow
    py -3.12 -m venv .venv
    & $py -m pip install -r requirements.txt
}
$env:PYTHONIOENCODING = "utf-8"
Write-Host "Starting TripAgent at http://localhost:8501" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") -m streamlit run app.py
