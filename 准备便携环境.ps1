# Build a truly portable folder: official Python embeddable + pip + deps + Chromium in pw-browsers
# Run from project root:
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1"
# Clean rebuild:
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1" -Recreate
# Skip Whisper model pre-download (~3GB):
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1" -SkipWhisperModel
# NVIDIA GPU：安装 CUDA 12 cuBLAS/cuDNN wheel（~1.2GB，faster-whisper 用 GPU 时需要）：
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1" -Gpu
# Skip Playwright Chromium download (~400MB, first B站/YouTube fetch will install):
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1" -SkipPlaywright
# Online-only mode（最小安装，仅 yt-dlp + 云端 LLM，~50MB，无需 Whisper/Chromium）：
#   powershell -ExecutionPolicy Bypass -File ".\准备便携环境.ps1" -OnlineOnly

param([switch]$Recreate, [switch]$SkipWhisperModel, [switch]$Gpu, [switch]$SkipPlaywright, [switch]$OnlineOnly)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root
$ProgressPreference = "SilentlyContinue"

$EmbedVer = "3.11.9"
$EmbedDir = Join-Path $Root "python_embed"
$EmbedZipName = "python-$EmbedVer-embed-amd64.zip"
$EmbedUrl = "https://www.python.org/ftp/python/$EmbedVer/$EmbedZipName"
$PwBrowsers = Join-Path $Root "pw-browsers"

if ($Recreate) {
    foreach ($p in @($EmbedDir, $PwBrowsers)) {
        if (Test-Path $p) {
            Write-Host "Removing $p ..."
            Remove-Item -Recurse -Force $p
        }
    }
}

$py = Join-Path $EmbedDir "python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Downloading embeddable Python $EmbedVer (python.org) ..."
    $zipPath = Join-Path $env:TEMP $EmbedZipName
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $zipPath -UseBasicParsing
    New-Item -ItemType Directory -Force $EmbedDir | Out-Null
    Expand-Archive -LiteralPath $zipPath -DestinationPath $EmbedDir -Force
    Remove-Item $zipPath -ErrorAction SilentlyContinue

    $pth = Get-ChildItem -LiteralPath $EmbedDir -Filter "*._pth" -File | Select-Object -First 1
    if ($null -eq $pth) {
        Write-Error "No *._pth file in embed zip."
    }
    $txt = [System.IO.File]::ReadAllText($pth.FullName)
    $txt = $txt.Replace("#import site", "import site")
    [System.IO.File]::WriteAllText($pth.FullName, $txt)
}

if (-not (Test-Path $py)) {
    Write-Error "python.exe missing under python_embed."
}

# Embeddable Python ignores PYTHONPATH; add repo src/ so ``-m bilibili_vision.*`` works (path relative to python_embed/).
$pthNow = Get-ChildItem -LiteralPath $EmbedDir -Filter "*._pth" -File -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pthNow) {
    $t = [System.IO.File]::ReadAllText($pthNow.FullName)
    if ($t -notmatch '(?m)^\.\.\\src\s*\r?$') {
        $nl = if ($t.Contains("`r`n")) { "`r`n" } else { "`n" }
        $trim = $t.TrimEnd()
        if (-not [string]::IsNullOrWhiteSpace($trim) -and -not $trim.EndsWith($nl)) { $trim += $nl }
        [System.IO.File]::WriteAllText($pthNow.FullName, $trim + "..\src" + $nl)
        Write-Host "Patched $($pthNow.Name): added ..\src for package imports."
    }
}

$pipMarker = Join-Path $EmbedDir "Lib\site-packages\pip\__init__.py"
if (-not (Test-Path $pipMarker)) {
    Write-Host "Installing pip (get-pip) ..."
    $getPip = Join-Path $env:TEMP "get-pip-embed.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
    & $py $getPip --no-warn-script-location
    Remove-Item $getPip -ErrorAction SilentlyContinue
}

Write-Host "pip install / upgrade ..."
& $py -m pip install -q --upgrade pip

if ($OnlineOnly) {
    Write-Host ""
    Write-Host "=== Online-Only Mode ==="
    Write-Host "Installing minimal dependencies (yt-dlp + Pillow only) ..."
    & $py -m pip install -r (Join-Path $Root "requirements-online.txt")

    $addTk = Join-Path $Root "add_tkinter_to_embed.ps1"
    if (Test-Path $addTk) {
        Write-Host "Adding tkinter + Tcl/Tk for GUI ..."
        & $addTk -ProjectRoot $Root
    }

    Write-Host ""
    Write-Host "Done (Online-Only mode)."
    Write-Host "Supports: YouTube / any site with public subtitles + cloud LLM analysis."
    Write-Host "Does NOT include: Bilibili login, local Whisper ASR, local VLM."
    Write-Host ""
    Write-Host "To upgrade later:"
    Write-Host "  Full mode:     .\准备便携环境.ps1           (adds Whisper + Chromium)"
    Write-Host "  Bilibili only: pip install playwright && playwright install chromium"
    Write-Host ""
    Write-Host "Run START.bat or 启动.bat to launch."
    exit 0
}

& $py -m pip install -r (Join-Path $Root "requirements.txt")

$gpuReq = Join-Path $Root "requirements-gpu.txt"
if ($Gpu) {
    if (-not (Test-Path $gpuReq)) {
        Write-Warning "requirements-gpu.txt not found, skip GPU wheels."
    } else {
        Write-Host "pip install GPU CUDA 12 wheels (nvidia-cublas / cudnn, large download) ..."
        & $py -m pip install -r $gpuReq
    }
}

$addTk = Join-Path $Root "add_tkinter_to_embed.ps1"
if (Test-Path $addTk) {
    Write-Host "Adding tkinter + Tcl/Tk for GUI (embed zip has no GUI stdlib) ..."
    & $addTk -ProjectRoot $Root
}

$srcDir = Join-Path $Root "src"
if ((-not $SkipWhisperModel) -and (Test-Path $srcDir)) {
    Write-Host "Pre-download faster-whisper large-v3 + small -> whisper-models (~3GB + ~500MB, avoids HF on recipient PC) ..."
    & $py -m bilibili_vision.download_whisper_models large-v3 small
}

if (-not $SkipPlaywright) {
    New-Item -ItemType Directory -Force $PwBrowsers | Out-Null
    $env:PLAYWRIGHT_BROWSERS_PATH = $PwBrowsers
    Write-Host "playwright install chromium -> pw-browsers (large, needs stable network) ..."
    & $py -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Warning "Chromium download failed (network). Double-click install_chromium.bat later, or run:"
        Write-Host ('  $env:PLAYWRIGHT_BROWSERS_PATH = "' + $PwBrowsers + '"')
        Write-Host ('  & "' + $py + '" -m playwright install chromium')
    }
} else {
    Write-Host "Skipping Chromium download (-SkipPlaywright). Recipient can run install_chromium.bat on first use."
}

Write-Host ""
Write-Host "Done (Full mode). Zip the WHOLE project folder including: python_embed, pw-browsers, src/, run_gui.py, *.bat"
Write-Host "Recipient: run START.bat or 启动.bat — no system Python needed."
