@echo off
setlocal

rem Double-click this file to start the live bot immediately.
chcp 65001 >nul
title Quantum Desk - Live Trading

rem Run from the project root so Python can import bot and xau.
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m bot.main %*
) else (
  python -m bot.main %*
)

set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%BOT_EXIT_CODE%"=="0" echo Bot stopped with exit code %BOT_EXIT_CODE%.
pause
exit /b %BOT_EXIT_CODE%
