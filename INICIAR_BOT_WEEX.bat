@echo off
setlocal
cd /d "%~dp0"
title WEEX Telegram Bot

echo ==========================================
echo       WEEX TELEGRAM MARKET ASSISTANT
echo ==========================================
echo.

if not exist ".env" (
    echo ERROR: No existe el archivo .env
    echo Copia .env.example como .env y configura tus credenciales.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo No encuentro el entorno virtual. Voy a crearlo...
    py -m venv .venv
    if errorlevel 1 (
        echo ERROR: No se pudo crear el entorno virtual.
        echo Comprueba que Python este instalado.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

if not exist ".venv\installed.ok" (
    echo Instalando dependencias necesarias...
    py -m pip install --upgrade pip
    py -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Fallo la instalacion de dependencias.
        pause
        exit /b 1
    )
    echo ok>.venv\installed.ok
)

echo.
echo Iniciando bot + centro de aprendizaje WEEX. No cierres esta ventana durante la sesion.
echo Para detenerlo, pulsa Ctrl+C.
echo.
py bot_weex.py

echo.
echo El bot se ha detenido.
pause
