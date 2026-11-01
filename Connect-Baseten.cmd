@echo off
setlocal
"%~dp0backend\.venv\Scripts\python.exe" "%~dp0tools\preflight\configure_baseten.py"
if errorlevel 1 pause
