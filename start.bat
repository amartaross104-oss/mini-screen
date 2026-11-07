@echo off
cd /d "%~dp0"
if not exist config.json (
  echo Creating config.json from config.example.json ...
  copy config.example.json config.json >nul
)
set "VENV=%USERPROFILE%\MiniScreen\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Setting up Python environment...
  python -m venv "%VENV%" || goto :err
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip || goto :err
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)
"%VENV%\Scripts\python.exe" server.py --open
goto :eof
:err
echo Setup failed.
pause
