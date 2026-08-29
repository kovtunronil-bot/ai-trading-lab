@echo off
rem Launcher for the nightly local AI + trading run. Called by Windows Task Scheduler.
rem Uses the py launcher (Python 3.13) and logs output to night_runner.log.
cd /d "%~dp0"
py night_runner.py >> night_runner.log 2>&1
