$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment missing. Run: python -m venv .venv, then .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$env:COOKIE_SECURE = 'false'
$localSettings = Join-Path $PSScriptRoot 'data\local-clickhouse.json'
if (Test-Path -LiteralPath $localSettings) {
    $databaseSettings = Get-Content -Raw -LiteralPath $localSettings | ConvertFrom-Json
    $env:CLICKHOUSE_USER = $databaseSettings.username
    $env:CLICKHOUSE_PASSWORD = $databaseSettings.password
    $env:CLICKHOUSE_HOST = '127.0.0.1'
    $databaseReady = $false
    try { $null = Invoke-WebRequest 'http://127.0.0.1:8123/ping' -UseBasicParsing -TimeoutSec 2; $databaseReady = $true } catch {}
    if (-not $databaseReady) {
        Write-Host 'Starting the local Ubuntu database...'
        $wslProcess = Start-Process -FilePath 'wsl.exe' -ArgumentList '-d','Ubuntu','-u','root','--','sleep','infinity' -WindowStyle Hidden -PassThru
        $wslProcess.Id | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'data\local-wsl.pid')
        for ($attempt=0; $attempt -lt 30; $attempt++) {
            try { $null = Invoke-WebRequest 'http://127.0.0.1:8123/ping' -UseBasicParsing -TimeoutSec 1; break } catch { Start-Sleep -Seconds 1 }
        }
    }
}
& $python -c "import auth,device_store; c=device_store.sqlite3.connect(device_store.DB_PATH); count=c.execute('SELECT count(*) FROM users WHERE enabled=1 AND role=?', ('ADMIN',)).fetchone()[0]; c.close(); raise SystemExit(0 if count else 2)"
if ($LASTEXITCODE -eq 2) {
    Write-Host 'Create your website login. Username: admin. Type a password of at least 12 characters.'
    & $python manage.py create-admin admin
    if ($LASTEXITCODE -ne 0) { throw 'Admin creation failed.' }
} elseif ($LASTEXITCODE -ne 0) { throw 'Could not check the user database.' }
$running = $false
try { $running = (Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 2).status -eq 'ok' } catch {}
if (-not $running) {
    New-Item -ItemType Directory -Force data | Out-Null
    Start-Process -FilePath $python -ArgumentList '-m','uvicorn','search_api:app','--host','127.0.0.1','--port','8000' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data\api-local.out.log' -RedirectStandardError 'data\api-local.err.log'
}
Write-Host 'Website: http://127.0.0.1:8000'
try {
    $null = Invoke-WebRequest 'http://127.0.0.1:8123/ping' -UseBasicParsing -TimeoutSec 2
    & $python manage.py init-schema
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed.' }
    $listenerPidFile = Join-Path $PSScriptRoot 'data\local-listener.pid'
    $listenerRunning = $false
    if (Test-Path -LiteralPath $listenerPidFile) {
        $listenerProcessId = [int](Get-Content -LiteralPath $listenerPidFile)
        $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId" -ErrorAction SilentlyContinue
        $listenerRunning = $listenerProcess -and $listenerProcess.CommandLine -like '*listener.py*'
    }
    if (-not $listenerRunning) {
        $listenerProcess = Start-Process -FilePath $python -ArgumentList 'listener.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data\listener-local.out.log' -RedirectStandardError 'data\listener-local.err.log' -PassThru
        $listenerProcess.Id | Set-Content -LiteralPath $listenerPidFile
    }
    Write-Host 'ClickHouse connected. Listener started on UDP 514. Docker is not used.'
} catch {
    Write-Host 'Website is available; ClickHouse is not ready. Log search needs the local database.'
}
