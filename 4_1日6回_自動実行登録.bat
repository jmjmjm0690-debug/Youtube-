@echo off
cd /d "%~dp0"
schtasks /create /tn "YouTubeTrendAutoStudio_6xDaily" /tr "\"%~dp03_YouTube自動生成＆公開実行.bat\"" /sc hourly /mo 4 /st 00:00 /f
echo.
echo Created 6x daily schedule task: YouTubeTrendAutoStudio_6xDaily (Every 4 hours: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00)
pause
