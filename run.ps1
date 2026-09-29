param(
    [switch]$Demo,
    [switch]$SetupModel,
    [switch]$WithModel
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
$modelProcess = $null
try {
    & uv sync
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости приложения' }
    if ($SetupModel) {
        & uv run python services/imajev/manage.py setup
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить imajev' }
    }
    if ($WithModel -and -not $Demo) {
        New-Item -ItemType Directory -Force -Path 'data' | Out-Null
        $modelProcess = Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList @('services/imajev/manage.py', 'start') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput 'data\imajev.stdout.log' -RedirectStandardError 'data\imajev.stderr.log'
        Write-Host 'Сервис запускается. Журнал: data\imajev.stderr.log'
    }
    $appArguments = @('run', 'streetjev')
    if ($Demo) { $appArguments += '--demo' }
    & uv @appArguments
} finally {
    if ($null -ne $modelProcess -and -not $modelProcess.HasExited) {
        # Завершаем только сервис, который запущен этим скриптом, вместе с его GPU-процессом.
        & taskkill /PID $modelProcess.Id /T /F 2>$null | Out-Null
    }
    Pop-Location
}
