$ErrorActionPreference = "Stop"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$App = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $App "runtime"
$SrcPython = "C:\Users\imerc\AppData\Roaming\uv\python\cpython-3.12.9-windows-x86_64-none"
$SrcVenv = "D:\tools\transcribe-dual\.venv"
$SrcHf = "D:\tools\transcribe-dual\hf"
$SrcModels = "D:\tools\transcribe-dual\models"
$SrcPyannote = Join-Path $env:USERPROFILE ".cache\torch\pyannote"
$DstPython = Join-Path $Runtime "python"
# «Нормальный транскрайб.exe»
$ExeName = "Нормальный транскрайб.exe"

Write-Host "Building portable runtime in $Runtime"
New-Item -ItemType Directory -Force -Path $Runtime, (Join-Path $Runtime "work"), (Join-Path $Runtime "logs"), (Join-Path $Runtime "torch") | Out-Null

Write-Host "1/5 Python..."
if (Test-Path $DstPython) { Remove-Item $DstPython -Recurse -Force }
robocopy $SrcPython $DstPython /MIR /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy python failed: $LASTEXITCODE" }

Write-Host "2/5 Packages..."
$Site = Join-Path $DstPython "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $Site | Out-Null
robocopy (Join-Path $SrcVenv "Lib\site-packages") $Site /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy site-packages failed: $LASTEXITCODE" }

Write-Host "3/5 Whisper cache..."
$Hf = Join-Path $Runtime "hf"
if (Test-Path $Hf) { Remove-Item $Hf -Recurse -Force }
robocopy $SrcHf $Hf /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy hf failed: $LASTEXITCODE" }

Write-Host "4/5 GigaAM..."
$Models = Join-Path $Runtime "models"
if (Test-Path $Models) { Remove-Item $Models -Recurse -Force }
robocopy $SrcModels $Models /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy models failed: $LASTEXITCODE" }

Write-Host "5/5 Speakers..."
$Pyannote = Join-Path $Runtime "pyannote"
if (Test-Path $Pyannote) { Remove-Item $Pyannote -Recurse -Force }
robocopy $SrcPyannote $Pyannote /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy pyannote failed: $LASTEXITCODE" }

Set-Content -Path (Join-Path $Runtime ".models-ok") -Value "portable`n" -Encoding ascii
Write-Host "Runtime ready."

Write-Host "Building exe..."
$env:PYTHONUTF8 = "1"
& (Join-Path $SrcVenv "Scripts\python.exe") -m PyInstaller --noconfirm --clean --windowed --onefile --name NormalTranscribe --distpath $App --workpath "D:\tools\transcribe-dual\pyi-build" --specpath "D:\tools\transcribe-dual\pyi-build" (Join-Path $App "app_gui.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Move-Item -Force (Join-Path $App "NormalTranscribe.exe") (Join-Path $App $ExeName)
Write-Host "Done: $(Join-Path $App $ExeName)"
