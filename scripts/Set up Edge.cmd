@echo off
setlocal
"%~dp0python\python.exe" -B -X utf8 -m cisco_vmanage_mcp.edge_host --setup %*
set "sdwan_result=%errorlevel%"
if "%~1"=="" pause
exit /b %sdwan_result%
