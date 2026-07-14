@echo off
REM VibeNode launcher (Windows).
REM
REM Detached spawn: pythonw.exe has no console window, so the web server
REM survives a closed terminal, a sign-out, and any Windows console reclaim.
REM This removes the "KEEP THIS TERMINAL OPEN" failure mode where the user
REM accidentally closes the minimized launcher window and the web server
REM dies with it (daemon survived, sessions intact, but no UI to reach them).
REM See CLAUDE.md for the full rationale.
REM
REM Fallback: if pythonw is not on PATH (rare), fall back to the legacy
REM minimized console pattern so the launcher still works.

cd /d "%~dp0"

where pythonw >nul 2>&1
if not errorlevel 1 (
    REM Detached path — preferred. start "" returns immediately, pythonw has
    REM no console, .bat exits, nothing visible is left to close.
    start "" pythonw session_manager.py
    exit /b
)

REM ---- Legacy fallback: no pythonw available ----
if not defined MINIMIZED (
    set MINIMIZED=1
    start /min "" "%~f0"
    exit /b
)
python session_manager.py
if errorlevel 1 (
    echo.
    echo ERROR: VibeNode failed to start. See message above.
    pause
)
