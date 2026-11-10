param(
    [string]$OdooRoot = 'C:\Workspace\odoo-18',
    [string]$PythonExe = 'C:\Workspace\odoo-18\venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
& {
    $ErrorActionPreference = 'Stop'
    Write-Host '===== SHIPSTATION ODOO19 STAGING PREFLIGHT (READ ONLY) =====' -ForegroundColor Cyan
    Write-Host ('Project: ' + $OdooRoot)
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "Python not found: $PythonExe" }
    & $PythonExe --version
    if ($LASTEXITCODE -ne 0) { throw 'Python executable failed' }
    $candidates = @(
        (Join-Path $OdooRoot 'odoo-bin'),
        (Join-Path $OdooRoot 'odoo\odoo-bin'),
        (Join-Path $OdooRoot 'server\odoo-bin')
    )
    $bin = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($bin) {
        Write-Host ('Odoo CLI found: ' + $bin) -ForegroundColor Green
        & $PythonExe -B $bin --version
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Odoo CLI failed to launch. Check Python packages and addons paths before upgrade.' }
    } else { Write-Warning 'Odoo CLI not found at standard project locations.' }
    $configs = @(
        (Join-Path $OdooRoot 'odoo.conf'),
        (Join-Path $OdooRoot 'odoo.cfg'),
        (Join-Path $OdooRoot 'config\odoo.conf'),
        (Join-Path $OdooRoot 'odoo\odoo.conf')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
    if ($configs) { $configs | ForEach-Object { Write-Host ('Odoo config filename found: ' + $_) } }
    else { Write-Warning 'No standard Odoo config filename found. It may be in a custom location.' }
    $services = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*postgres*' })
    if ($services.Count) { $services | Select-Object Name, Status | Format-Table -AutoSize }
    else { Write-Warning 'No Windows PostgreSQL service found (DB may be in Docker/WSL/remote).' }
    Write-Host 'No database, cron, remote API, stock adjustment, or label operation was executed.' -ForegroundColor Green
    Write-Host 'IMPORTANT: Do not run module upgrade until a distinct disposable staging DB is selected and backed up.' -ForegroundColor Yellow
}
