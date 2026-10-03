@echo off
cd /d "%~dp0"
py -3 gui.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Bot encountered an error while starting.
    pause
)
