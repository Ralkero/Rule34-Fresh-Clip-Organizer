@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%preview_rule34_batch.ps1" %*
exit /b %ERRORLEVEL%
