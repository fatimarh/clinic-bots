@echo off
setlocal
set PYTHONUNBUFFERED=1

cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

REM Без редиректа — логгер пишет в logs\doctor.log
python -m bots.doctor_bot
pause
