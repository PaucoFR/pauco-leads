# Start Pauco Gestion App on Railway
Write-Host "[START] Launching App Gestion (wsgi:app)" -ForegroundColor Green

# Navigate to app_client directory
$appDir = Join-Path $PSScriptRoot "app_client"
Set-Location $appDir

# Default to port 8000 if PORT env var not set
$port = if ($env:PORT) { $env:PORT } else { 8000 }

# Use venv's Python from root
$pythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    Write-Host "Error: Virtual environment not found at $pythonExe" -ForegroundColor Red
    exit 1
}

# Run waitress via Python module (Windows-compatible alternative to gunicorn)
& $pythonExe -m waitress `
    --port=$port `
    --threads=4 `
    wsgi:app
