@echo off
title PlugICT Installer
echo ========================================
echo   PlugICT Knowledge Vault Setup
echo ========================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found! Please install Python 3.10+ from:
    echo https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Run the installer
echo Starting installer...
python setup.py
if errorlevel 1 (
    echo.
    echo Installation encountered an error. See messages above.
    pause
    exit /b 1
)

echo.
echo Setup complete. You can close this window.
pause
