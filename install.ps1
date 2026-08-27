param(
    [int]$RequiredFreeGB = 15,
    [switch]$TestModels
)

$ErrorActionPreference = "Stop"

Write-Host "Expert My Rules - native Windows install (no Docker)" -ForegroundColor Cyan
Write-Host "Default local ensemble:"
Write-Host "  Director/Worker/Planner : qwen3:4b      (~2.5 GB model)"
Write-Host "  Adversarial Reviewer    : llama3.2:3b  (~2.0 GB model)"
Write-Host "  Independent Verifier    : gemma3:4b    (~3.3 GB model)"
Write-Host "  Required model payload  : ~7.8 GB"
Write-Host "  Native free space       : >=15 GB (20 GB recommended)"
Write-Host ""

if ($RequiredFreeGB -lt 1) {
    throw "RequiredFreeGB must be a positive integer number of GB."
}

$Drive = (Get-Location).Drive
if (-not $Drive) {
    throw "Could not determine free space for the current drive."
}
$FreeGB = [math]::Floor($Drive.Free / 1GB)
Write-Host "Disk preflight: $FreeGB GB free on drive $($Drive.Name): (minimum $RequiredFreeGB GB)."
if ($FreeGB -lt $RequiredFreeGB) {
    throw "Not enough free space for the native multi-model installation. Free at least $RequiredFreeGB GB; 20 GB is recommended."
}

function Resolve-PythonExe {
    $candidates = @()

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) { $candidates += $pythonCmd.Source }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        foreach ($selector in @("-3.13", "-3.12", "-3.11")) {
            try {
                $resolved = & $pyCmd.Source $selector -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $resolved) { $candidates += $resolved.Trim() }
            } catch {}
        }
    }

    foreach ($path in @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe"
    )) {
        if (Test-Path $path) { $candidates += $path }
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        try {
            $ok = & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch {}
    }
    return $null
}

function Resolve-OllamaExe {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($path in @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

$PythonExe = Resolve-PythonExe
if (-not $PythonExe) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python 3.11+ was not found and winget is unavailable. Install Python 3.11 or newer, then rerun install.ps1."
    }
    Write-Host "Python 3.11+ not found. Installing Python 3.12 with winget..."
    & $Winget.Source install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed." }
    $PythonExe = Resolve-PythonExe
    if (-not $PythonExe) { throw "Python was installed but could not be resolved in this session. Open a new PowerShell and rerun install.ps1." }
}

Write-Host "Using Python: $PythonExe"
& $PythonExe -m venv .venv
$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
$AwbExe = Join-Path (Get-Location) ".venv\Scripts\awb.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e .

$OllamaExe = Resolve-OllamaExe
if (-not $OllamaExe) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Ollama was not found and winget is unavailable. Install Ollama for Windows from https://ollama.com/download/windows, then rerun install.ps1."
    }
    Write-Host "Ollama not found. Installing Ollama with winget..."
    & $Winget.Source install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Ollama installation failed." }
    $OllamaExe = Resolve-OllamaExe
    if (-not $OllamaExe) { throw "Ollama was installed but could not be resolved in this session. Open a new PowerShell and rerun install.ps1." }
}

Write-Host "Using Ollama: $OllamaExe"

# These settings keep small machines conservative. They are inherited by a
# serve process started by this script. Existing Ollama background processes
# may require a restart before they pick up the values.
$env:OLLAMA_MAX_LOADED_MODELS = "1"
$env:OLLAMA_NUM_PARALLEL = "1"

function Test-OllamaServer {
    try {
        & $OllamaExe list *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-OllamaServer)) {
    Write-Host "Starting local Ollama server..."
    Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-OllamaServer) { $ready = $true; break }
    }
    if (-not $ready) { throw "Ollama did not become ready on http://127.0.0.1:11434." }
}

$BaselineModels = @("qwen3:4b", "llama3.2:3b", "gemma3:4b")
$ExtraModels = @(
    $env:AWB_PLANNER_MODEL,
    $env:AWB_DIRECTOR_MODEL,
    $env:AWB_WORKER_MODEL,
    $env:AWB_REVIEWER_MODEL,
    $env:AWB_VERIFIER_MODEL
) | Where-Object { $_ }
$Models = @($BaselineModels + $ExtraModels | Select-Object -Unique)

Write-Host "Downloading/checking required local models..."
foreach ($Model in $Models) {
    Write-Host "  -> $Model"
    & $OllamaExe pull $Model
    if ($LASTEXITCODE -ne 0) { throw "Failed to download Ollama model: $Model" }
}

Write-Host "Installed Ollama models:"
& $OllamaExe list

if ($TestModels) {
    Write-Host "Running one inference smoke test per baseline model..."
    foreach ($Model in $BaselineModels) {
        Write-Host "  -> testing $Model"
        $Body = @{
            model = $Model
            prompt = "Reply with exactly: OK"
            stream = $false
            keep_alive = 0
        } | ConvertTo-Json
        $Response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:11434/api/generate" -ContentType "application/json" -Body $Body -TimeoutSec 600
        if (-not $Response.response) { throw "Model smoke test returned no response: $Model" }
        Write-Host "     response: $($Response.response.Trim())"
    }
}

Write-Host ""
Write-Host "READY" -ForegroundColor Green
Write-Host "Expert My Rules installed natively. No Docker is required."
Write-Host "Start the dashboard with:"
Write-Host "  .\.venv\Scripts\awb.exe serve --host 127.0.0.1 --port 8000"
Write-Host "Then open: http://localhost:8000"
Write-Host "Role routing: Worker=qwen3:4b | Reviewer=llama3.2:3b | Verifier=gemma3:4b"
Write-Host "Cloud/API escalation remains OFF by default."
