@echo off
cd /d D:\autogen_projects
call .venv\Scripts\activate
python -m skills.douyin_downloader.web --port 8080
