@echo off
title AI Usage Tracker
cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

python ai_usage_tracker.py
if errorlevel 1 (
    echo.
    echo Failed to start. Check the error above.
    pause
)
