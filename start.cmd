@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. See docs\USER_GUIDE.md
  pause
  exit /b 1
)
echo Open http://127.0.0.1:8000 in your browser. Ctrl+C to stop.
".venv\Scripts\python.exe" -m fast_parser serve
