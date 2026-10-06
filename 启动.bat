@echo off
cd /d "%~dp0"
echo Starting YuanQi LiFang demo on http://127.0.0.1:8775
start "" http://127.0.0.1:8775
python app.py
pause
