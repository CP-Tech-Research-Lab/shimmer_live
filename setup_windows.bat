@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" setup_windows.py %*
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 setup_windows.py %*
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python setup_windows.py %*
    exit /b
)
echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
echo Enable the Python launcher or Add Python to PATH, then run this file again.
exit /b 1
