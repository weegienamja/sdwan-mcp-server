@echo off
title Demarc local diagnostics
setlocal
"%~dp0python\python.exe" -B -X utf8 -m cisco_vmanage_mcp.workstation --llama-server "%~dp0llama\llama-server.exe" %*
set "sdwan_result=%errorlevel%"
if "%~1"=="" pause
exit /b %sdwan_result%
