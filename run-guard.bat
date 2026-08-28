@echo off
cd /d "%~dp0"
py crypto_guard.py quiet >> guard_log.txt 2>&1
