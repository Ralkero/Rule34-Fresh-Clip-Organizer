@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%apply_approved_rule34_plan.ps1" %*
exit /b %ERRORLEVEL%
