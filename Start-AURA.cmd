@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.local\environment\Docker\resources\bin\docker.exe" set "PATH=%~dp0.local\environment\Docker\resources\bin;%PATH%"

if /I "%~1"=="--setup" goto setup

if not exist "backend\.venv\Scripts\python.exe" (
  echo ERROR: Backend environment is missing ^(backend\.venv^).
  echo Run Start-AURA.cmd --setup first.
  pause
  exit /b 1
)
if not exist "frontend\dist\index.html" (
  echo ERROR: Frontend build is missing ^(frontend\dist\index.html^).
  echo Run Start-AURA.cmd --setup to build it.
  pause
  exit /b 1
)

echo Starting AURA at http://127.0.0.1:8000/
backend\.venv\Scripts\python.exe tools\runtime\aura_runtime.py serve
if errorlevel 1 pause
exit /b %errorlevel%

:setup
where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: uv was not found on PATH. Install uv, then run this again.
  pause
  exit /b 1
)
call uv sync --locked --project backend
if errorlevel 1 (
  echo ERROR: Backend setup failed. See the output above.
  pause
  exit /b 1
)
backend\.venv\Scripts\python.exe tools\runtime\aura_runtime.py setup --build-frontend --skip-uv-sync
if errorlevel 1 (
  echo ERROR: Setup failed. See the output above.
  pause
  exit /b 1
)
echo Setup complete. Run Start-AURA.cmd to start AURA.
pause
exit /b 0
