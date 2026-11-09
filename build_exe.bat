@echo off
setlocal
cd /d "%~dp0"
title Build Clip Squeezer.exe

echo Building the standalone app folder and the release zip.
echo FFmpeg is not included. The app downloads it on first use.
echo.

py -3 -m pip install --quiet --upgrade --disable-pip-version-check pyinstaller -r requirements.txt
if errorlevel 1 (
    echo [!] Could not install the build tools.
    if not defined CI pause
    exit /b 1
)

py -3 -m PyInstaller --noconfirm --clean --windowed --onedir ^
    --name "Clip Squeezer" ^
    --icon "assets\icon.ico" ^
    --collect-all tkinterdnd2 ^
    ClipSqueezer.py

if errorlevel 1 (
    echo.
    echo [!] Build failed - see the messages above.
    if not defined CI pause
    exit /b 1
)

rem Files a person (and the licences) expect to find next to the exe.
set "STAGE=dist\Clip Squeezer"
mkdir "%STAGE%\licenses"
copy /y "packaging\HOW TO USE.txt" "%STAGE%\" >nul
copy /y "LICENSE" "%STAGE%\LICENSE.txt" >nul
copy /y "THIRD_PARTY_NOTICES.md" "%STAGE%\licenses\" >nul
py -3 -c "import os,shutil,sys; shutil.copy(os.path.join(sys.prefix,'LICENSE.txt'), r'%STAGE%\licenses\Python-LICENSE.txt')"
copy /y "%STAGE%\_internal\_tk_data\license.terms" "%STAGE%\licenses\Tcl-Tk-LICENSE.txt" >nul
for /d %%d in ("%STAGE%\_internal\tkinterdnd2-*.dist-info") do copy /y "%%d\licenses\LICENSE" "%STAGE%\licenses\tkinterdnd2-LICENSE.txt" >nul
for %%f in (Python-LICENSE.txt Tcl-Tk-LICENSE.txt tkinterdnd2-LICENSE.txt THIRD_PARTY_NOTICES.md) do (
    if not exist "%STAGE%\licenses\%%f" (
        echo [!] Missing licence file: %%f
        if not defined CI pause
        exit /b 1
    )
)

for /f %%v in ('py -3 -c "import ClipSqueezer as c; print(c.VERSION)"') do set "VERSION=%%v"
if not exist release mkdir release
set "ZIP=release\ClipSqueezer-v%VERSION%-Windows.zip"
powershell -NoProfile -Command "Compress-Archive -LiteralPath 'dist\Clip Squeezer' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 (
    echo [!] Could not create the zip.
    if not defined CI pause
    exit /b 1
)

echo.
echo Done. Release zip:
echo   "%~dp0%ZIP%"
echo.
if not defined CI pause
