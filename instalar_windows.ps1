# Prepara este computador para correr el monitor de precios todo el día:
#   1) instala las dependencias   2) evita que el equipo se suspenda   3) lo inicia solo al abrir sesión
# Uso (PowerShell, dentro de la carpeta del proyecto):  powershell -ExecutionPolicy Bypass -File .\instalar_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "1/4 Revisando Python..."
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "No encuentro Python. Instálalo desde https://www.python.org/downloads/ (marca 'Add python.exe to PATH' y deja 'py launcher') y vuelve a ejecutar este script." -ForegroundColor Red
    exit 1
}
py --version

Write-Host "2/4 Instalando dependencias (puede tardar unos minutos)..."
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m playwright install chromium

if (-not (Test-Path ".env")) {
    Write-Host "OJO: falta el archivo .env con TELEGRAM_TOKEN y TELEGRAM_CHAT_ID (copia .env.example como .env y complétalo, o copia el .env del otro PC)." -ForegroundColor Yellow
}

Write-Host "3/4 Evitando que el equipo se suspenda con corriente conectada..."
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0

Write-Host "4/4 Configurando el inicio automático al abrir sesión..."
$startup = [Environment]::GetFolderPath("Startup")
$bat = Join-Path $PSScriptRoot "iniciar_monitor.bat"
# .vbs con ventana oculta (estilo 0): arranca el .bat sin dejar una consola abierta
$vbs = "CreateObject(""Wscript.Shell"").Run """"""$bat"""""", 0, False"
Set-Content -Path (Join-Path $startup "monitor_precios.vbs") -Value $vbs -Encoding ASCII
Write-Host "Listo. Inicio automático creado en: $startup\monitor_precios.vbs" -ForegroundColor Green
Write-Host ""
Write-Host "Para probarlo ahora sin reiniciar:  .\iniciar_monitor.bat   (o ejecuta monitor_precios.vbs para verlo oculto)"
Write-Host "Para ver qué hace:                  Get-Content monitor.log -Wait -Tail 30"
Write-Host "Para detenerlo:                     .\detener_monitor.bat"
Write-Host "Para quitar el inicio automático:   Remove-Item '$startup\monitor_precios.vbs'"
