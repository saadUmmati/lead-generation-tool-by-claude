@echo off
cd /d %~dp0
python -m venv venv
call venv\Scripts\activate.bat

echo Installing core requirements (required)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ============================================================
    echo   CORE INSTALL FAILED - this is a real problem, stop here
    echo   and check the error above.
    echo ============================================================
    pause
    exit /b 1
)

echo.
echo Installing optional extras (JS-render fallback)...
echo If this step fails, that is OK - the core tool still works fine,
echo you just won't have the "render stubborn sites with a real browser"
echo option until this is sorted out separately.
pip install -r requirements-optional.txt
if errorlevel 1 (
    echo.
    echo NOTE: optional extras failed to install - continuing anyway,
    echo the core tool is unaffected. See the error above if you want
    echo to fix this later.
)

echo.
echo Setup complete. Run "run.bat" to start Lead Finder.
pause
