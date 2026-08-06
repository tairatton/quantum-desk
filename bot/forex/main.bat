@echo off
setlocal

rem Double-click this file to start the live bot immediately.
chcp 65001 >nul
title Quantum Desk - Live Trading

rem Run from core/ so Python can import bot, engine and strategy as top-level packages.
cd /d "%~dp0core"
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" -m entrypoints.live %*
) else (
  python -m entrypoints.live %*
)

set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%BOT_EXIT_CODE%"=="0" echo Bot stopped with exit code %BOT_EXIT_CODE%.
pause
exit /b %BOT_EXIT_CODE%
