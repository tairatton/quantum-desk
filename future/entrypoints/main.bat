@echo off
setlocal

chcp 65001 >nul
title Quantum Desk - Futures Terminal
cd /d "%~dp0.."
if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" -m entrypoints.main %*
) else (
  python -m entrypoints.main %*
)

set "FUTURE_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%FUTURE_EXIT_CODE%"=="0" echo Futures terminal stopped with exit code %FUTURE_EXIT_CODE%.
pause
exit /b %FUTURE_EXIT_CODE%
