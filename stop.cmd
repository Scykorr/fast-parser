@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. See docs\USER_GUIDE.md
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m fast_parser stop %*
if errorlevel 1 (
  pause
  exit /b 1
)
