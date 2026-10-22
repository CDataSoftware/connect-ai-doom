@echo off
rem Runs setup.ps1 past Windows' default execution policy, which blocks .ps1 scripts.
rem   setup.cmd         build everything
rem   setup.cmd -Game   only rebuild the game
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
