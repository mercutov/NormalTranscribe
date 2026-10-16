$ErrorActionPreference = "Stop"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

$AppRoot = Split-Path $PSScriptRoot -Parent
$Bin = Join-Path $AppRoot "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$Uv = Join-Path $Bin "uv.exe"
if (-not (Test-Path $Uv)) {
    $fallback = "D:\KritaServer\ComfyUI\uv\uv.exe"
    if (Test-Path $fallback) {
        Copy-Item $fallback $Uv
    }
}
if (-not (Test-Path $Uv)) {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { $Uv = $found.Source }
}
if (-not (Test-Path $Uv)) {
    Write-Host "Downloading uv..."
    $zip = Join-Path $env:TEMP "uv-windows.zip"
    Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $env:TEMP "uv-windows") -Force
    $downloaded = Get-ChildItem (Join-Path $env:TEMP "uv-windows") -Filter "uv.exe" -Recurse | Select-Object -First 1
    Copy-Item $downloaded.FullName $Uv
}

$Root = "D:\tools\transcribe-dual"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Here = $PSScriptRoot
New-Item -ItemType Directory -Force -Path $Root, (Join-Path $Root "work"), (Join-Path $Root "models"), (Join-Path $Root "hf"), (Join-Path $Root "logs") | Out-Null

& $Uv python install 3.12
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$BasePython = (& $Uv python find 3.12).Trim()
if (-not (Test-Path $VenvPython)) {
    & $Uv venv (Join-Path $Root ".venv") --python $BasePython
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Installing transcription packages..."
& $Uv pip install --python $VenvPython -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Installing CUDA PyTorch 2.5..."
& $Uv pip install --python $VenvPython --reinstall "torch==2.5.1+cu124" "torchaudio==2.5.1+cu124" --index-url "https://download.pytorch.org/whl/cu124"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Installing GigaAM..."
$GigaLocal = Join-Path $AppRoot "third_party\GigaAM"
if (Test-Path (Join-Path $GigaLocal "pyproject.toml")) {
    & $Uv pip install --python $VenvPython --reinstall --no-deps $GigaLocal
} else {
    & $Uv pip install --python $VenvPython --reinstall --no-deps "gigaam @ git+https://github.com/salute-developers/GigaAM.git"
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Config = Join-Path $Root "config.yaml"
if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Here "config.example.yaml") $Config
}

& $VenvPython -c "import torch, gigaam, faster_whisper, soundfile, pyannote.audio"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Marker = Join-Path $Root ".setup-ok"
Set-Content -Path $Marker -Value (Get-Date -Format "yyyy-MM-dd HH:mm") -Encoding ascii
Write-Host "Environment ready: $Root"
