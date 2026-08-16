# Requires PowerShell 5.1+
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

Write-Host "=== Wiki Video Pipeline - Windows Setup ===" -ForegroundColor Cyan

$python = $null
$candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notmatch "WindowsApps") {
            $python = $cmd.Source
            break
        }
    }
}
if (-not $python) {
    Write-Host "Python not found. Install with: winget install Python.Python.3.12" -ForegroundColor Red
    exit 1
}
Write-Host "Using Python: $python"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "FFmpeg not found. Install with: winget install Gyan.FFmpeg" -ForegroundColor Yellow
} else {
    Write-Host "FFmpeg: OK"
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    & $python -m venv .venv
}

Write-Host "Installing dependencies..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example" -ForegroundColor Yellow
}

$skillLlm = Join-Path $env:USERPROFILE ".cursor\skills\llm-chain\scripts"
if (Test-Path (Join-Path $skillLlm "sync_env.py")) {
    Write-Host "Syncing LLM keys via llm-chain skill..."
    & .\.venv\Scripts\python.exe (Join-Path $skillLlm "sync_env.py") --project .
    & .\.venv\Scripts\python.exe (Join-Path $skillLlm "install_module.py") --project . --package src.llm
} else {
    Write-Host "llm-chain skill not found — set LLM_* keys in .env manually." -ForegroundColor Yellow
}

$skillGauth = Join-Path $env:USERPROFILE ".cursor\skills\google-auth\scripts\sync.py"
if (Test-Path $skillGauth) {
    Write-Host "Syncing YouTube OAuth clients via google-auth skill..."
    & .\.venv\Scripts\python.exe $skillGauth --project . --service youtube --write-yaml
} else {
    Write-Host "google-auth skill not found — copy secrets/ from news-shorts-pipeline." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path output | Out-Null

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Start now: .\scripts\restart-app.ps1 -Background"
Write-Host "  2. Open: http://127.0.0.1:8082"
Write-Host "  3. Start with Windows: .\scripts\restart-app.ps1 -RegisterStartup"
Write-Host "  4. Generate: python -m src.pipeline --topic `"Voyager 1`" --format short --mock"
