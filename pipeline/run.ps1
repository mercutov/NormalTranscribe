param(
    [string]$InputPath = "",
    [string]$OutputPath = "",
    [int]$Speakers = 0,
    [int]$MinSpeakers = 0,
    [int]$MaxSpeakers = 0,
    [switch]$Watch,
    [switch]$Force,
    [switch]$RenderOnly,
    [switch]$Check,
    [string]$Glossary = ""
)

$ErrorActionPreference = "Stop"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:HF_HOME = "D:\tools\transcribe-dual\hf"
$env:HUGGINGFACE_HUB_CACHE = "D:\tools\transcribe-dual\hf\hub"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
if (Test-Path "D:\RVC_new") {
    $env:PATH = "D:\RVC_new;" + $env:PATH
}

$Python = "D:\tools\transcribe-dual\.venv\Scripts\python.exe"
$Marker = "D:\tools\transcribe-dual\.setup-ok"
if (-not (Test-Path $Python) -or -not (Test-Path $Marker)) {
    & "$PSScriptRoot\setup.ps1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$AppRoot = Split-Path -Parent $PSScriptRoot
if (-not $InputPath) { $InputPath = Join-Path $AppRoot "Ролики" }
if (-not $OutputPath) { $OutputPath = Join-Path $AppRoot "Заметки" }

$argv = @("$PSScriptRoot\transcribe_batch.py")
if ($Check) { $argv += "--check" }
if ($InputPath) { $argv += @("--input", $InputPath) }
if ($OutputPath) { $argv += @("--output", $OutputPath) }
if ($Speakers -gt 0) { $argv += @("--speakers", "$Speakers") }
if ($MinSpeakers -gt 0) { $argv += @("--min-speakers", "$MinSpeakers") }
if ($MaxSpeakers -gt 0) { $argv += @("--max-speakers", "$MaxSpeakers") }
if ($Watch) { $argv += "--watch" }
if ($Force) { $argv += "--force" }
if ($RenderOnly) { $argv += "--render-only" }
if ($Glossary) { $argv += @("--glossary", $Glossary) }

& $Python @argv
exit $LASTEXITCODE
