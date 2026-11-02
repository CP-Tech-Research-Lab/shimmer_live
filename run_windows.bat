@echo off
rem Creates the virtual environment on first run, then starts Shimmer Live.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv || python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe -m shimmer_live %*
