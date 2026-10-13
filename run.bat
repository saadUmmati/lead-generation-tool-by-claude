@echo off
cd /d %~dp0
if not exist .env if exist .env.example copy .env.example .env >nul
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

set SCRAPER_EXE=google_maps_scraper-1.18.1-windows-amd64.exe
set STARTED_SCRAPER=0

echo Checking the Google Maps scraper ^(port 8080^)...
netstat -ano | findstr /R /C:"TCP.*:8080.*LISTENING" >nul
if not errorlevel 1 goto scraper_already

if exist "scraper\%SCRAPER_EXE%" goto scraper_start
echo.
echo   Could not find scraper\%SCRAPER_EXE%
echo   Download it from the gosom/google-maps-scraper Releases page
echo   and put it in the scraper folder, then run this file again.
echo.
pause
exit /b 1

:scraper_start
echo Starting the Google Maps scraper in a minimized window...
start "LeadFinder-Scraper" /min /d "%~dp0scraper" "%~dp0scraper\%SCRAPER_EXE%" -web
set STARTED_SCRAPER=1
set TRIES=0

:scraper_wait
netstat -ano | findstr /R /C:"TCP.*:8080.*LISTENING" >nul
if not errorlevel 1 goto scraper_ready
set /a TRIES+=1
if %TRIES% GEQ 30 goto scraper_timeout
timeout /t 1 >nul
goto scraper_wait

:scraper_timeout
echo.
echo   The scraper did not open port 8080 within 30 seconds.
echo   Look at the minimized "LeadFinder-Scraper" window for the error.
echo.
pause
exit /b 1

:scraper_already
echo Scraper is already running - reusing it.

:scraper_ready
echo Scraper ready.

start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:9000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 9000

if not "%STARTED_SCRAPER%"=="1" goto finish
echo Shutting down the Google Maps scraper...
taskkill /F /IM %SCRAPER_EXE% >nul 2>&1

:finish
pause
