@echo off
setlocal
cd /d "%~dp0.."
echo === tail logs/admin.log ===
powershell -NoProfile -Command "Get-Content -Path 'logs/admin.log' -Wait -Tail 100"