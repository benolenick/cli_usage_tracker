@echo off
cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Use pythonw to suppress the console window (tkinter-only)
start "" pythonw ai_usage_tracker.py
