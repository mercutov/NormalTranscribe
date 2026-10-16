# Нормальный транскрайб — установка для тестера (Windows PowerShell)
# Одна команда:
#   irm https://raw.githubusercontent.com/mercutov/NormalTranscribe/master/install.ps1 | iex
#
# Куда ставить (по желанию до команды):
#   $env:NT_HOME = "F:\NormalTranscribe"

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "mercutov/NormalTranscribe"
$Dest = $env:NT_HOME
if (-not $Dest) {
    if (Test-Path "F:\") { $Dest = "F:\NormalTranscribe" }
    else { $Dest = "C:\NormalTranscribe" }
}
$Dest = $Dest.TrimEnd("\", "/")

Write-Host ""
Write-Host "=== Нормальный транскрайб ===" -ForegroundColor Yellow
Write-Host "Установка в: $Dest"
Write-Host ""

if ($Dest -match "[^\x00-\x7F]") {
    throw "Путь не должен содержать кириллицу (ломается GigaAM). Пример: F:\NormalTranscribe"
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$tmp = Join-Path $env:TEMP ("NT-install-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    Write-Host "Спрашиваю GitHub Releases..."
    $headers = @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "NormalTranscribe-Installer"
    }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers
    $tag = [string]$release.tag_name
    Write-Host "Релиз: $tag"

    $assets = @($release.assets)
    if (-not $assets -or $assets.Count -eq 0) { throw "В релизе нет файлов (assets)." }

    $app = $assets | Where-Object { $_.name -like "NormalTranscribe-app-*.zip" } | Select-Object -First 1
    $parts = @($assets | Where-Object { $_.name -like "NormalTranscribe-data-*.part*.zip" } | Sort-Object name)
    if (-not $app) { throw "Нет NormalTranscribe-app-*.zip в релизе. Попроси разработчика выложить полный релиз." }

    function Get-Asset([object]$asset) {
        $out = Join-Path $tmp $asset.name
        Write-Host ("Скачиваю {0} ({1:N1} MB)..." -f $asset.name, ($asset.size / 1MB))
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $out -UseBasicParsing
        return $out
    }

    $appZip = Get-Asset $app
    Write-Host "Распаковываю приложение..."
    Expand-Archive -Path $appZip -DestinationPath $Dest -Force

    if ($parts.Count -eq 0) {
        Write-Host "Внимание: data-part архивов нет. Runtime нужно скопировать отдельно." -ForegroundColor Yellow
    } else {
        foreach ($part in $parts) {
            $zip = Get-Asset $part
            Write-Host "Распаковываю $($part.name)..."
            Expand-Archive -Path $zip -DestinationPath $Dest -Force
        }
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $Dest "Заметки"), (Join-Path $Dest "Ролики") | Out-Null
    $obs = Join-Path $Dest "Заметки\.obsidian"
    if (-not (Test-Path $obs)) {
        New-Item -ItemType Directory -Force -Path $obs | Out-Null
        Set-Content (Join-Path $obs "app.json") "{}" -Encoding utf8
    }

    $exe = Join-Path $Dest "Нормальный транскрайб.exe"
    $modelsOk = Join-Path $Dest "runtime\.models-ok"
    if (-not (Test-Path $exe)) { throw "После установки не найден exe." }
    if (-not (Test-Path $modelsOk)) {
        Write-Host "Предупреждение: нет runtime\.models-ok — модели могли не докачаться." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Готово." -ForegroundColor Green
    Write-Host "Запуск: $exe"
    Write-Host "Заметки: $(Join-Path $Dest 'Заметки')"
    Write-Host ""
    Write-Host "Ollama (по желанию): https://ollama.com  затем  ollama pull qwen2.5:7b-instruct"
    Write-Host ""

    $launch = Read-Host "Запустить сейчас? (y/n)"
    if ($launch -match "^(y|д|yes|да)$") {
        Start-Process $exe
    }
}
finally {
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}
