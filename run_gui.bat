@echo off
rem GTNHMapSlim launcher
rem Prefers the local virtual environment: native window with real drag paths.

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "gui\server.py" --native %*
    if errorlevel 1 pause
    exit /b 0
)

set "PY="

call :probe python
if not defined PY call :probe py

if not defined PY call :scan "%LOCALAPPDATA%\Programs\Python"
if not defined PY call :scan "C:/"
if not defined PY call :scan "%ProgramFiles%"
if not defined PY call :scan "%USERPROFILE%\.workbuddy-ai\binaries\python\versions"

if not defined PY goto :nopython

echo Using Python: %PY%
echo.
"%PY%" "gui\server.py" %*
if errorlevel 1 pause
exit /b 0

:probe
rem Verify the command exists and runs Python 3.
%~1 -c "import sys;sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
for /f "delims=" %%P in ('where %~1 2^>nul') do (
    if not defined PY set "PY=%%P"
)
exit /b 0

:scan
rem Look for python.exe one level below the given directory.
if not exist "%~1" exit /b 0
for /d %%V in ("%~1\*") do (
    if not defined PY if exist "%%~V\python.exe" (
        "%%~V\python.exe" -c "import sys;sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=%%~V\python.exe"
    )
)
exit /b 0

:nopython
echo.
echo [ERROR] Python 3 was not found on this computer.
echo.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo During installation, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
