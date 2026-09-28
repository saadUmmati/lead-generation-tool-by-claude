@echo off
cd /d %~dp0
python -m venv venv
call venv\Scripts\activate.bat
pip install -r requirements.txt
echo.
echo Setup complete. Run "run.bat" to start Lead Finder.
pause
