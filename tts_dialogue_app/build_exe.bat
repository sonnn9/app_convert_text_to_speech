@echo off
REM ============================================================
REM  Build script for TTS Dialogue App (Windows .exe)
REM  - Creates a virtual environment (if missing)
REM  - Installs dependencies
REM  - Builds a single-file windowed .exe with PyInstaller
REM  Output: dist\TTS_Dialogue_App.exe
REM ============================================================

setlocal

echo.
echo [1/4] Checking virtual environment...
if not exist ".venv\" (
    echo Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment. Is Python 3.11+ installed and on PATH?
        pause
        exit /b 1
    )
)

echo.
echo [2/4] Activating virtual environment...
call ".venv\Scripts\activate.bat"

echo.
echo [3/4] Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [4/4] Building .exe with PyInstaller...
REM --onefile     : single .exe
REM --windowed    : no console window (GUI app)
REM --name        : output exe name
REM --add-data    : bundle the assets folder (icon, etc.). Format on Windows is "src;dest"
REM --icon        : application icon (only added if assets\icon.ico exists)
set "ICON_ARG="
if exist "assets\icon.ico" set ICON_ARG=--icon "assets\icon.ico"

pyinstaller --onefile --windowed ^
    --name "TTS_Dialogue_App" ^
    --add-data "assets;assets" ^
    %ICON_ARG% ^
    main.py

if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Build complete!  ->  dist\TTS_Dialogue_App.exe
echo.
echo  NOTE: ffmpeg is required for audio merge/export.
echo  Either install ffmpeg and add it to PATH, or place
echo  ffmpeg.exe next to TTS_Dialogue_App.exe in the dist folder.
echo ============================================================
echo.
pause
endlocal
