@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
:loop
python launcher.py
if %ERRORLEVEL% EQU 3 (
  echo Launcher is already running in another window - this one exits.
  timeout /t 10 >nul
  exit /b 3
)
echo Launcher stopped, restart in 15 s (close this window to exit)...
timeout /t 15 >nul
goto loop
