@echo off
cd /d %~dp0
call venv\Scripts\activate.bat

echo Checking whether port 9000 is already in use by an old instance...
netstat -ano | findstr /R /C:"TCP.*:9000.*LISTENING" >nul
if %errorlevel%==0 (
    echo.
    echo ============================================================
    echo   PORT 9000 IS ALREADY IN USE.
    echo   This almost always means an OLD run.bat window from a
    echo   previous session is still running in the background,
    echo   and this new one would silently talk to THAT old copy
    echo   of the code instead of the one you just updated.
    echo.
    echo   Fix: find and close every other window titled similar to
    echo   this one ^(or open Task Manager, end any extra python.exe
    echo   / uvicorn processes^), then run this file again.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

echo Port is free - starting fresh.
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:9000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 9000
pause
