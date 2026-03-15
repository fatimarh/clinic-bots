@echo off
setlocal
cd /d "%~dp0.."
echo === tail logs/doctor.log ===
powershell -NoProfile -Command "Get-Content -Path 'logs/doctor.log' -Wait -Tail 100"