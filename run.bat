@echo off
rem ===========================================================================
rem  Anime Viewer launcher.
rem  NOTE: this file is intentionally ASCII-only, because cmd.exe reads .bat
rem  files in the OEM codepage (cp866 on Russian Windows) and UTF-8 Cyrillic
rem  text inside a batch file breaks command parsing.
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem proper encoding for console output
set "PYTHONIOENCODING=utf-8"

rem --detach: start without a console window, so closing any terminal
rem cannot kill the application (uses pythonw.exe)
if /i "%~1"=="--detach" (
    if exist "%~dp0runtime\pythonw.exe" (
        start "" "%~dp0runtime\pythonw.exe" "%~dp0main.py"
        goto :eof
    )
)

rem 1) portable Python bundled with this folder (preferred)
if exist "%~dp0runtime\python.exe" (
    "%~dp0runtime\python.exe" "%~dp0main.py" %*
    goto :check
)

rem 2) system Python launcher
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0main.py" %*
    goto :check
)

rem 3) plain python from PATH
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0main.py" %*
    goto :check
)

echo.
echo Python 3.10+ not found.
echo Keep the "runtime" folder next to main.py, or install Python yourself.
echo.
pause
goto :eof

:check
if errorlevel 1 (
    echo.
    echo The application exited with error code %errorlevel%.
    pause
)
endlocal
