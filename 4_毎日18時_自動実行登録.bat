@echo off
cd /d "%~dp0"
schtasks /create /tn "YouTubeTrendAutoStudio" /tr "\"%~dp03_YouTube自動生成＆公開実行.bat\"" /sc daily /st 18:00 /f
echo.
echo Created daily schedule task: YouTubeTrendAutoStudio (Every day at 18:00)
pause
