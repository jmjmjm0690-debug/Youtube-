@echo off
cd /d "%~dp0"
python -X utf8 scripts/get_youtube_refresh_token.py
pause
