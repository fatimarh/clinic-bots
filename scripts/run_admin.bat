@echo off
setlocal
set PYTHONUNBUFFERED=1

REM Перейти в корень проекта
cd /d "%~dp0.."
set PYTHONPATH=%CD%

if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate

python -m pip install --upgrade pip
pip install -r requirements.txt

REM ВАЖНО: без редиректа (логгер сам пишет в logs\admin.log)
python -m bots.admin_bot
pause