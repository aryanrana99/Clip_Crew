@echo off
rem Starts ClipCrew and opens it in your browser. The first run sets everything up (takes a few minutes).
cd /d "%~dp0"
title ClipCrew - app

if exist ".venv\Scripts\python.exe" goto run

echo.
echo  Setting up ClipCrew for the first time...
echo.

rem --- find Python 3.10+ ---
set PY=
py -3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set PY=py -3
if not defined PY python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set PY=python
if not defined PY (
  echo  Python 3.10 or newer is not installed.
  echo  Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" in the installer,
  echo  then run start.bat again.
  echo.
  pause
  exit /b 1
)

%PY% -m venv .venv || (echo  Could not create the Python environment. & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (
  echo.
  echo  Installing the Python packages failed - check your internet connection and run start.bat again.
  rmdir /s /q .venv
  pause
  exit /b 1
)

:run
where ffmpeg >nul 2>&1 || (
  echo.
  echo  FFmpeg is not installed - ClipCrew needs it to cut and render clips.
  echo  Easiest way: open PowerShell and run   winget install Gyan.FFmpeg
  echo  then close this window and run start.bat again.
  echo.
  pause
  exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul

start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" run.py
pause
