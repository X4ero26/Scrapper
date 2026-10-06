@echo off
rem Detiene el monitor (el bucle de reinicio y el scraper). Para volver a iniciarlo: iniciar_monitor.bat
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'iniciar_monitor|scraper\.py --loop' } | ForEach-Object { Write-Host ('Deteniendo ' + $_.Name + ' (' + $_.ProcessId + ')'); Stop-Process -Id $_.ProcessId -Force }"
echo Listo.
pause
