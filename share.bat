@echo off
rem Shares ClipCrew with friends through a free Serveo tunnel. Close the window to stop sharing.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0share.ps1"
pause
