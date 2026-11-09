@echo off
setlocal
cd /d "%~dp0"
title Clip Squeezer setup

echo ==============================
echo   Clip Squeezer - one time setup
echo ==============================
echo.

where py.exe >nul 2>nul
if not %errorlevel%==0 (
    echo [!] Python was not found.
    echo     Install Python 3 from https://www.python.org/downloads/windows/
    echo     and tick "Add python.exe to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('py -3 --version 2^>^&1') do echo [ok] Python %%v

echo.
echo [1/2] Optional: drag-and-drop support (tkinterdnd2)
py -3 -m pip install --quiet --disable-pip-version-check tkinterdnd2
if errorlevel 1 (
    echo       Could not install it - the app still works, you just click
    echo       instead of dragging files in.
) else (
    echo       Installed.
)

echo.
echo [2/2] ffmpeg engine
if exist "bin\ffmpeg.exe" (
    if exist "bin\ffprobe.exe" (
        echo       Already bundled in bin\ - nothing to do.
        goto :done
    )
)
py -3 "ClipSqueezer.py" --install-ffmpeg
if errorlevel 1 (
    echo       Download failed. Check your connection and run this again.
    pause
    exit /b 1
)

:done
echo.
echo ==============================
echo   Ready. Launch "Clip Squeezer.vbs"
echo ==============================
pause
