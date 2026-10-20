# FlyFF Multi-Agent Orchestrator (Microsoft Agent Framework) installer/launcher
# Right-click -> Run with PowerShell, or: powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "`n=== FlyFF Orchestrator (Microsoft Agent Framework) ===" -ForegroundColor Cyan

# Find Python; if missing, install with winget.
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python and winget are both missing. Install Python 3.12+ from https://python.org, then re-run this script."
    }
    Write-Host "Python not found; installing Python 3.12 via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "Python installation finished but python is not on PATH. Restart PowerShell and run this script again." }
}
$PythonExe = $Python.Source
Write-Host "Python: $PythonExe" -ForegroundColor Green

# Verify npx exists (Unity CLI is separately required to use Unity MCP).
$Npx = Get-Command npx -ErrorAction SilentlyContinue
if (-not $Npx) {
    Write-Host "Node.js/npx not found; installing Node.js LTS via winget..." -ForegroundColor Yellow
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) { throw "Install Node.js LTS (includes npx) from https://nodejs.org, then re-run." }
    winget install --id OpenJS.NodeJS.LTS --exact --accept-package-agreements --accept-source-agreements
    $Npx = Get-Command npx -ErrorAction SilentlyContinue
    if (-not $Npx) { throw "Node installed; restart PowerShell so npx is on PATH, then re-run this script." }
}
Write-Host "npx: $($Npx.Source)" -ForegroundColor Green

# Create and prepare virtual environment.
$VenvPython = Join-Path $ProjectDir "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $PythonExe -m venv (Join-Path $ProjectDir "venv")
}
Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)." }

# Ensure MCP package can be resolved by npx and Unity MCP package exists in target project.
Write-Host "Checking REA MCP package..." -ForegroundColor Cyan
& $Npx.Source -y rea-agents@6.4.0 --version 2>$null
# The REA package is launched lazily by the app; npm's npx cache handles it.

# Set up .env without overwriting an existing key.
$EnvPath = Join-Path $ProjectDir ".env"
$ExamplePath = Join-Path $ProjectDir ".env.example"
if (-not (Test-Path $EnvPath)) {
    Copy-Item $ExamplePath $EnvPath
    Write-Host "Created .env from .env.example" -ForegroundColor Green
} else {
    Write-Host "Keeping existing .env (not overwriting secrets)." -ForegroundColor Green
}

Write-Host "`nPlease open the .env file and insert your API keys before running the server." -ForegroundColor Yellow
Write-Host "Required: DEEPSEEK_API_KEY. Optional: DEEPSEEK_BASE_URL / model IDs."
Write-Host "Unity MCP additionally requires Unity CLI and com.unity.pipeline in your project."
Write-Host "The server will start now; runs needing an API key will fail until .env is filled."
Read-Host "Press Enter to continue and start the server"

# Load .env into this process before starting uvicorn.
Get-Content $EnvPath | ForEach-Object {
    if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
$env:PYTHONPATH = $ProjectDir
Write-Host "Starting the orchestrator at http://127.0.0.1:8765 ..." -ForegroundColor Green
& $VenvPython -m orchestrator.server
