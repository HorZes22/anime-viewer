@echo off
rem ===========================================================================
rem  Anime Viewer - start WITHOUT a console window (pythonw.exe).
rem  Double-click this file: the window will not die if you close any terminal.
rem  For logs/diagnostics use run.bat instead (it keeps the console attached).
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

if exist "%~dp0runtime\pythonw.exe" (
    start "" "%~dp0runtime\pythonw.exe" "%~dp0main.py"
    goto :eof
)

if exist "%~dp0runtime\python.exe" (
    start "" "%~dp0runtime\python.exe" "%~dp0main.py"
    goto :eof
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    goto :eof
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python "%~dp0main.py"
    goto :eof
)

echo Python 3.10+ not found. Keep the "runtime" folder next to main.py.
pause
endlocal
