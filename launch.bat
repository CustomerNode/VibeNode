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
REM Interpreter selection order (Step 6 ARM64 stabilization, 2026-09-26):
REM   1. Local override: .local\python.txt (one line, absolute path). Optional,
REM      gitignored. Lets a specific machine pin the interpreter (e.g. an
REM      ARM64 venv) WITHOUT publishing that path to other users via git.
REM   2. pythonw on PATH  -- the portable default. Works out of the box on
REM      any machine that has Python installed system-wide.
REM   3. Legacy console fallback (minimized python.exe) -- only if pythonw
REM      is not on PATH.
REM
REM launch.bat is committed to a public repo and shared with other users, so
REM it must never carry any machine-specific interpreter path.

cd /d "%~dp0"

REM ---- 1. Local override --------------------------------------------------
REM If .local\python.txt exists, its first non-blank line is used as the
REM interpreter. A line that starts with # is skipped (comment). If the file
REM exists but the path it names does not, we fall through to the default so
REM a stale override never leaves the user with a dead launcher.
set "VN_PY="
if exist "%~dp0.local\python.txt" (
    for /f "usebackq eol=# tokens=* delims=" %%L in ("%~dp0.local\python.txt") do (
        if not defined VN_PY set "VN_PY=%%L"
    )
)
if defined VN_PY (
    if exist "%VN_PY%" (
        start "" "%VN_PY%" session_manager.py
        exit /b
    )
)

REM ---- 2. pythonw on PATH (portable default) -------------------------------
where pythonw >nul 2>&1
if not errorlevel 1 (
    REM Detached path — preferred. start "" returns immediately, pythonw has
    REM no console, .bat exits, nothing visible is left to close.
    start "" pythonw session_manager.py
    exit /b
)

REM ---- 3. Legacy fallback: no pythonw available ----------------------------
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
