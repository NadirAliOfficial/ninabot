@echo off
title BOT BLANK — Lancement
color 0B
cls

echo.
echo  ══════════════════════════════════════════
echo  ██  BOT BLANK  ██  IBKR TRADING BOT  ██
echo  ══════════════════════════════════════════
echo.

REM ── Configurer le chemin ──────────────────────────────────────
set BASEDIR=%~dp0

echo  Vérification de la configuration...
if not exist "%BASEDIR%backend\.env" (
    echo.
    echo  [ERREUR] Fichier .env manquant !
    echo  Ouvrez backend\.env et renseignez votre compte IBKR.
    echo.
    notepad "%BASEDIR%backend\.env"
    pause
    exit /b 1
)

REM Vérifier que IBKR_ACCOUNT est renseigné
findstr /C:"IBKR_ACCOUNT=" "%BASEDIR%backend\.env" | findstr /V "IBKR_ACCOUNT=$" >nul 2>&1
if errorlevel 1 (
    echo  [ERREUR] IBKR_ACCOUNT non renseigné dans .env !
    notepad "%BASEDIR%backend\.env"
    pause
    exit /b 1
)

echo  [1/4] Python...
py -3.12 --version >nul 2>&1 || ( echo  [ERREUR] Python 3.12 requis & pause & exit /b 1 )

echo  [2/4] Node.js...
node --version >nul 2>&1 || ( echo  [ERREUR] Node.js requis & pause & exit /b 1 )

echo  [3/4] Venv Python...
if not exist "%BASEDIR%backend\venv\Scripts\activate.bat" (
    cd /d "%BASEDIR%backend"
    py -3.12 -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt --quiet
) else ( echo  OK )

echo  [4/4] Node modules...
if not exist "%BASEDIR%frontend\node_modules" (
    cd /d "%BASEDIR%frontend"
    call npm install --silent
) else ( echo  OK )

echo.
echo  Lancement Backend...
start "BOT BACKEND" cmd /k "cd /d %BASEDIR%backend && venv\Scripts\activate && python main.py"
timeout /t 10 /nobreak >nul

echo  Lancement Frontend...
start "BOT FRONTEND" cmd /k "cd /d %BASEDIR%frontend && npm run dev"
timeout /t 8 /nobreak >nul

start "" "http://localhost:5173"
echo.
echo  ✅ BOT BLANK lancé — http://localhost:5173
pause
