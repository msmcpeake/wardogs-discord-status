@echo off
rem Installs the Python packages this project needs (see requirements.txt).
rem Double-click this file, or run it from a terminal - either way.
cd /d "%~dp0"

echo Installing required Python packages...
echo.
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Install failed - see the error above. Most likely cause: Python
    echo isn't installed, or wasn't added to PATH during install - see the
    echo "Prerequisites" section in README.md.
    pause
    exit /b 1
)

echo.
echo Done. Next: follow README.md starting at "1. Create a Discord bot".
pause
