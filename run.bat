@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
:loop
python bot.py
echo Bot stopped, restart in 15 s (close this window to exit)...
timeout /t 15 >nul
goto loop
