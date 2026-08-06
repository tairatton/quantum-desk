@echo off
setlocal

chcp 65001 >nul
title Quantum Desk - Futures Terminal

rem Run from core/ so Python can import bot, engine and strategy as top-level packages.
cd /d "%~dp0core"
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" -m entrypoints.main %*
) else (
  python -m entrypoints.main %*
)

set "FUTURE_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%FUTURE_EXIT_CODE%"=="0" echo Futures terminal stopped with exit code %FUTURE_EXIT_CODE%.
pause
exit /b %FUTURE_EXIT_CODE%
