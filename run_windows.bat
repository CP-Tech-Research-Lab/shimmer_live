@echo off
setlocal
rem Setup retries incomplete installs and notices changed requirements.
cd /d "%~dp0"
call setup_windows.bat --launch %*
if not errorlevel 1 exit /b 0
echo Shimmer Live could not start. See the error above.
pause
exit /b 1
