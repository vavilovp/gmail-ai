@echo off
chcp 65001 >nul
title Mail - test run (no changes)
cd /d %~dp0
py gmail_ai.py --dry-run %*
pause
