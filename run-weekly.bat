@echo off
cd /d "%~dp0"
py weekly.py quiet >> weekly_log.txt 2>&1
