# Push this repo to GitHub (needs Git + GitHub CLI, repo already has commits).
# Run from repo root:
#   powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
#
# Steps: gh auth login (browser) -> gh repo create -> push main
# User-visible messages are ASCII-only (Windows PowerShell 5.1 -File encoding safe).

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$env:Path = "C:\Program Files\Git\bin;C:\Program Files\GitHub CLI;" + $env:Path

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git not found. Install Git for Windows first."
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "gh not found. Run: winget install GitHub.cli"
}

$null = & git rev-parse --git-dir 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Not a git repository. Run: git init"
}

Write-Host ""
Write-Host ">>> Step 1/3: GitHub login (browser will open)" -ForegroundColor Cyan
Write-Host "    If login fails (TLS timeout), fix VPN/proxy/firewall, then run this script again." -ForegroundColor DarkGray
& gh auth login -h github.com -p https -w
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: gh auth login failed. You are not logged in. Fix network, then re-run this script." -ForegroundColor Red
    exit 1
}

$ErrorActionPreference = "SilentlyContinue"
$null = gh auth status 2>&1
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: Still not authenticated. Run: gh auth login -h github.com -p https -w" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host ">>> Step 2/3: New repository name" -ForegroundColor Cyan
Write-Host "    Press Enter for default. Do NOT type y/n here (that was only for the previous question)." -ForegroundColor DarkGray
$defaultName = "vlv"
$repoName = Read-Host "Repo name [$defaultName]"
if ([string]::IsNullOrWhiteSpace($repoName)) {
    $repoName = $defaultName
} elseif ($repoName -match '^(?i)(y|n|yes|no)$') {
    Write-Host "    Ignoring '$repoName' (looks like y/n). Using default: $defaultName" -ForegroundColor Yellow
    $repoName = $defaultName
}

Write-Host ""
Write-Host ">>> Step 3/3: Create public repo and push branch main" -ForegroundColor Cyan

# Do not use: if (& git remote get-url origin) — stderr breaks under $ErrorActionPreference Stop
$ErrorActionPreference = "SilentlyContinue"
git remote get-url origin 2>&1 | Out-Null
$hasOrigin = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"

if ($hasOrigin) {
    Write-Host "Remote origin exists; pushing only."
    & git push -u origin main
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    & gh repo create $repoName --public --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host ""
$login = ""
$ErrorActionPreference = "SilentlyContinue"
try { $login = (& gh api user --jq .login 2>&1 | Out-String).Trim() } catch { }
$ErrorActionPreference = "Stop"
if ($login) {
    Write-Host "Done: https://github.com/$login/$repoName" -ForegroundColor Green
} else {
    Write-Host "Done. Open GitHub in the browser to see the new repository." -ForegroundColor Green
}
