@echo off
rem Creates the "Anime Viewer" shortcut with the application icon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\create_shortcut.ps1"
pause
