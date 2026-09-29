$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Falha ao executar $Program. Consulte a mensagem acima." }
}

try {
    $existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        try {
            $status = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health/' -TimeoutSec 3
            if ($status.application -eq 'food-inventory-operations') {
                Start-Process 'http://127.0.0.1:8000'
                Write-Host 'O FoodOps Estoque ja esta aberto.'
                exit 0
            }
        } catch { }
        throw 'A porta 8000 esta ocupada por outro programa. Nenhum processo foi encerrado.'
    }
    $uvProgram = 'uv'
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $uvProgram = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (-not (Test-Path -LiteralPath $uvProgram -PathType Leaf)) {
            throw 'Instale o uv para preparar o ambiente Python.'
        }
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Instale e abra o Docker Desktop para iniciar o PostgreSQL.' }
    Invoke-Checked $uvProgram @('sync', '--locked')
    Invoke-Checked '.\.venv\Scripts\python.exe' @('scripts/configurar_local.py')
    Invoke-Checked 'docker' @('compose', 'up', '-d', '--wait')
    Invoke-Checked '.\.venv\Scripts\python.exe' @('manage.py', 'migrate', '--noinput')
    Invoke-Checked '.\.venv\Scripts\python.exe' @('manage.py', 'preparar_local')
    Invoke-Checked '.\.venv\Scripts\python.exe' @('manage.py', 'check')
    Invoke-Checked '.\.venv\Scripts\python.exe' @('manage.py', 'collectstatic', '--noinput', '--verbosity', '0')
    Write-Host 'FoodOps Estoque: http://127.0.0.1:8000'
    Write-Host 'Primeiro acesso: .local\acesso-inicial.txt'
    Write-Host 'Para encerrar o servidor web, pressione Ctrl+C.'
    Invoke-Checked '.\.venv\Scripts\python.exe' @('manage.py', 'runserver', '127.0.0.1:8000', '--noreload')
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host 'Pressione Enter para fechar'
    exit 1
}
