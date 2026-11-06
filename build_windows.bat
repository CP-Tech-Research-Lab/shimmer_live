@echo off
rem Build ShimmerLive.exe (a folder, dist\ShimmerLive) on this Windows PC.
rem Needs the app's environment: run setup_windows.bat once first.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.bat first.
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade "pyinstaller>=6.10" || exit /b 1
.venv\Scripts\python.exe packaging\make_icon.py || exit /b 1
.venv\Scripts\python.exe -m PyInstaller packaging\ShimmerLive.spec --noconfirm --distpath dist --workpath build || exit /b 1
echo Testing the result...
set SHIMMER_LIVE_SMOKE_TEST=%CD%\dist\smoke_test.txt
start /wait "" dist\ShimmerLive\ShimmerLive.exe
set RESULT=%ERRORLEVEL%
set SHIMMER_LIVE_SMOKE_TEST=
type dist\smoke_test.txt
if not "%RESULT%"=="0" (
  echo The built app is missing something: see above.
  exit /b 1
)
echo.
echo Done: dist\ShimmerLive\ShimmerLive.exe  (copy or zip the whole dist\ShimmerLive folder)
