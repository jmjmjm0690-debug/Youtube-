@echo off
cd /d "%~dp0"
python -X utf8 scripts/setup_email_notification.py
pause
