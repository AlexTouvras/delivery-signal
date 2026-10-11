@echo off
REM In Command Prompt: run opens the configuration page.
REM A single CSV still works: run issues.csv
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
