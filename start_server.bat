@echo off
cd /d "%~dp0"
echo Starting Douyin Web Server...
python skills/douyin_downloader/web.py --port 8080
pause
