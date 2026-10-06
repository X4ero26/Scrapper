@echo off
rem Corre el monitor de precios para siempre. Si el programa se cae, lo vuelve a levantar a los 60 s.
rem La salida queda en monitor.log (rotativo). Para detenerlo: detener_monitor.bat
cd /d "%~dp0"
:loop
py scraper.py --loop
echo [%date% %time%] el monitor se detuvo (codigo %errorlevel%); reinicio en 60 s>> monitor_reinicios.log
timeout /t 60 /nobreak >nul
goto loop
