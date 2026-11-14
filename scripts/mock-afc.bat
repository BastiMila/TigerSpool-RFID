@echo off
rem Runs the Moonraker mock as a Klipper printer with AFC on port 7125, the
rem port Tiger Studio scans. Extra arguments are passed on, for example
rem   mock-afc.bat --print-seconds 60
cd /d "%~dp0.."
python scripts\moonraker-ws-mock.py --profile afc --port 7125 %*
pause
