@echo off
setlocal
cd /d "%~dp0"

echo [1/2] Starting native Lavalink 4.2 server...
start "BaoGe Lavalink 4.2" cmd /k ""%~dp0lavalink\start_lavalink.bat""

echo Waiting for Lavalink on http://127.0.0.1:2333/version ...
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(45); while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:2333/version' -Headers @{Authorization='youshallnotpass'} -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { Write-Host ('Lavalink ready: ' + $r.Content); exit 0 } } catch {}; Start-Sleep -Seconds 1 }; Write-Host 'Lavalink startup timed out, continuing anyway...'; exit 0"

echo [2/2] Starting BaoGe Music Bot...
python -u bot/main.py
