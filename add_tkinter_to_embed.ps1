# Add Tcl/Tk + tkinter to official embeddable Python (embed zip has no GUI stdlib).
# Same CPython version as standalone build required (extension ABI).
# Run from project root:
#   powershell -ExecutionPolicy Bypass -File ".\add_tkinter_to_embed.ps1"

param(
    [string]$ProjectRoot = "",
    [string]$EmbedPythonVersion = "3.11.9",
    [string]$StandaloneReleaseTag = "20240713"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) { $ProjectRoot = $PSScriptRoot }
Set-Location $ProjectRoot

$py = Join-Path $ProjectRoot "python_embed\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "Missing python_embed\python.exe. Run prepare script first."
}

$dest = Join-Path $ProjectRoot "python_embed"
$marker = Join-Path $dest "Lib\tkinter\__init__.py"
if (Test-Path $marker) {
    Write-Host "tkinter already present under python_embed\Lib\tkinter — ensuring *._pth lists Lib ..."
    $pth = Get-ChildItem -LiteralPath $dest -Filter "*._pth" -File | Select-Object -First 1
    if ($null -ne $pth) {
        $pthText = [System.IO.File]::ReadAllText($pth.FullName)
        if ($pthText -notmatch '(?m)^Lib\s*$') {
            $lines = $pthText -split "`r?`n"
            $out = [System.Collections.Generic.List[string]]::new()
            $inserted = $false
            foreach ($line in $lines) {
                [void]$out.Add($line)
                if (-not $inserted -and $line.Trim() -eq ".") {
                    [void]$out.Add("Lib")
                    $inserted = $true
                }
            }
            if ($inserted) {
                [System.IO.File]::WriteAllText($pth.FullName, ($out -join "`r`n"))
                Write-Host "Patched $($pth.Name) (added Lib)."
            }
        }
    }
    exit 0
}

$tarName = "cpython-$EmbedPythonVersion+$StandaloneReleaseTag-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
$url = "https://github.com/astral-sh/python-build-standalone/releases/download/$StandaloneReleaseTag/$tarName"
$tmp = Join-Path $env:TEMP ("cpython-embed-tkinter-" + [Guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$tg = Join-Path $tmp $tarName

Write-Host "Downloading $tarName (astral-sh/python-build-standalone, matches Python $EmbedPythonVersion) ..."
Invoke-WebRequest -Uri $url -OutFile $tg -UseBasicParsing

Write-Host "Extracting ..."
tar -xf $tg -C $tmp
$src = Join-Path $tmp "python"
if (-not (Test-Path (Join-Path $src "DLLs\_tkinter.pyd"))) {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Write-Error "Archive layout unexpected: missing python/DLLs/_tkinter.pyd"
}

Copy-Item (Join-Path $src "DLLs\_tkinter.pyd") (Join-Path $dest "_tkinter.pyd") -Force
Copy-Item (Join-Path $src "DLLs\tcl86t.dll") (Join-Path $dest "tcl86t.dll") -Force
Copy-Item (Join-Path $src "DLLs\tk86t.dll") (Join-Path $dest "tk86t.dll") -Force

$libTk = Join-Path $dest "Lib\tkinter"
New-Item -ItemType Directory -Force -Path (Split-Path $libTk) | Out-Null
Copy-Item (Join-Path $src "Lib\tkinter") $libTk -Recurse -Force

$tclDest = Join-Path $dest "tcl"
if (Test-Path $tclDest) { Remove-Item -Recurse -Force $tclDest }
Copy-Item (Join-Path $src "tcl") $tclDest -Recurse -Force

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

$pth = Get-ChildItem -LiteralPath $dest -Filter "*._pth" -File | Select-Object -First 1
if ($null -eq $pth) { Write-Error "No *._pth under python_embed." }
$pthText = [System.IO.File]::ReadAllText($pth.FullName)
if ($pthText -notmatch '(?m)^Lib\s*$') {
    Write-Host "Patching $($pth.Name): add Lib line so tkinter package is on sys.path ..."
    $lines = $pthText -split "`r?`n"
    $out = [System.Collections.Generic.List[string]]::new()
    $inserted = $false
    foreach ($line in $lines) {
        [void]$out.Add($line)
        if (-not $inserted -and $line.Trim() -eq ".") {
            [void]$out.Add("Lib")
            $inserted = $true
        }
    }
    if (-not $inserted) { Write-Error "Could not find '.' line in $($pth.Name) to insert Lib after." }
    [System.IO.File]::WriteAllText($pth.FullName, ($out -join "`r`n"))
}

Write-Host "Verifying import tkinter ..."
& $py -c "import tkinter; print('tkinter OK', tkinter.TkVersion)"
Write-Host "Done."
