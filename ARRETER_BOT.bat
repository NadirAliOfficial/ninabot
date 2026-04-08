@echo off
title Arrêt BOT BLANK
taskkill /f /fi "WINDOWTITLE eq BOT BACKEND*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq BOT FRONTEND*" >nul 2>&1
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im node.exe >nul 2>&1
echo Bot arrêté.
timeout /t 2 >nul
