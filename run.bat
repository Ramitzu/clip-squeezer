@echo off
rem Debug launcher: keeps the console open so errors are visible.
cd /d "%~dp0"

where py.exe >nul 2>nul
if %errorlevel% neq 0 goto :try_python

py -3 "ClipSqueezer.py" %*
set "rc=%errorlevel%"
goto :end

:try_python
where python.exe >nul 2>nul
if %errorlevel% neq 0 goto :no_python

python "ClipSqueezer.py" %*
set "rc=%errorlevel%"
goto :end

:no_python
echo.
echo Python was not found. Install Python 3 from python.org and tick
echo "Add python.exe to PATH", then run this file again.
set "rc=9009"

:end
if not "%~1"=="" exit /b %rc%
pause
