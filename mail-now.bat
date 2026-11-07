@echo off
rem Same as the scheduled task: ask, then run everything in a window
cd /d %~dp0
start "" pyw gmail_ai.py --ask
