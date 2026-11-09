@echo off
setlocal
rem Build ShimmerLive.exe (a folder, dist\ShimmerLive) on this Windows PC.
rem Needs the app's environment: run setup_windows.bat once first.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup_windows.bat first.
  exit /b 1
)
rem Apply the same pandas constraint as the release workflow, even in an existing environment.
.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints-windows-compat.txt "pyinstaller>=6.10" || exit /b 1
.venv\Scripts\python.exe -m pip check || exit /b 1
.venv\Scripts\python.exe packaging\make_icon.py || exit /b 1
rem Prevent unrelated tools on PATH from contributing conflicting native DLLs.
set "PATH=%CD%\.venv\Scripts;%SystemRoot%\System32;%SystemRoot%"
.venv\Scripts\python.exe -m PyInstaller packaging\ShimmerLive.spec --clean --noconfirm --distpath dist --workpath build || exit /b 1
echo Testing the result...
set SHIMMER_LIVE_SMOKE_TEST=%CD%\dist\smoke_test.txt
if exist dist\smoke_test.txt del dist\smoke_test.txt
start /wait "" dist\ShimmerLive\ShimmerLive.exe
set RESULT=%ERRORLEVEL%
set SHIMMER_LIVE_SMOKE_TEST=
type dist\smoke_test.txt
if not "%RESULT%"=="0" (
  echo The built app is missing something: see above.
  exit /b 1
)
findstr /x /c:"SMOKE TEST OK" dist\smoke_test.txt >nul 2>&1
if errorlevel 1 (
  echo The built app did not produce a successful self-test report.
  exit /b 1
)
.venv\Scripts\python.exe -m pip freeze > dist\ShimmerLive\build-packages.txt || exit /b 1
echo.
echo Done: dist\ShimmerLive\ShimmerLive.exe  (copy or zip the whole dist\ShimmerLive folder)
