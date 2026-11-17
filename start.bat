@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

python -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
)

where ffmpeg >nul 2>&1
if errorlevel 1 echo warning: ffmpeg not found on PATH. Install it with: winget install Gyan.FFmpeg

python app.py
