@echo off
chcp 65001 >nul
title Mail - sort inbox
cd /d %~dp0
py gmail_ai.py %*
pause
