param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcVenv = "D:\tools\transcribe-dual\.venv"
$PyRelease = Join-Path $App "runtime\python\python.exe"
if (-not (Test-Path $PyRelease)) { $PyRelease = Join-Path $SrcVenv "Scripts\python.exe" }

if ($Version) {
    Set-Content -Path (Join-Path $App "VERSION") -Value "$Version`n" -Encoding utf8NoBOM
}

$ver = (Get-Content (Join-Path $App "VERSION") -Raw).Trim()
Write-Host "Building exe for v$ver ..."
$env:PYTHONUTF8 = "1"
& (Join-Path $SrcVenv "Scripts\python.exe") -m PyInstaller --noconfirm --clean --windowed --onefile --name NormalTranscribe --distpath $App --workpath "D:\tools\transcribe-dual\pyi-build" --specpath "D:\tools\transcribe-dual\pyi-build" (Join-Path $App "app_gui.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$dst = Join-Path $App "Нормальный транскрайб.exe"
$src = Join-Path $App "NormalTranscribe.exe"
if (Test-Path $dst) {
    $bak = Join-Path $App "Нормальный транскрайб.old.exe"
    Remove-Item -Force $bak -ErrorAction SilentlyContinue
    Move-Item -Force $dst $bak -ErrorAction SilentlyContinue
}
Move-Item -Force $src $dst
Remove-Item -Force (Join-Path $App "Нормальный транскрайб.old.exe") -ErrorAction SilentlyContinue

$OutDir = Join-Path $App "dist"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Zip = Join-Path $OutDir "NormalTranscribe-app-$ver.zip"

& $PyRelease -c "from pathlib import Path; import sys; sys.path.insert(0, r'$App\pipeline'); from app_update import build_app_zip; print(build_app_zip(Path(r'$App'), Path(r'$Zip')))"

Write-Host ""
Write-Host "Zip: $Zip"
Write-Host "Publish:"
Write-Host "  gh release create v$ver `"$Zip`" --title `"Нормальный транскрайб $ver`" --notes `"Обновление приложения (runtime не входит)`""
