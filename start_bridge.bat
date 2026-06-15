@echo off
title IBKR Bridge - Dashboard
color 0A

echo.
echo  ==========================================
echo   IBKR Bridge para Dashboard Estrategico
echo  ==========================================
echo.
echo  Instalando dependencias...
pip install ib_insync flask flask-cors --quiet

echo.
echo  Iniciando servidor...
echo  Asegurate de que TWS este abierto y con login
echo.

python ibkr_bridge.py

echo.
echo  El servidor se detuvo. Presiona cualquier tecla para cerrar.
pause
