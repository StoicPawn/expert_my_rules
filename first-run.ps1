$ErrorActionPreference = "Stop"

Write-Host "Expert My Rules - first run" -ForegroundColor Cyan
Write-Host "Default local ensemble:"
Write-Host "  Director/Worker/Planner : qwen3:4b      (~2.5 GB model)"
Write-Host "  Adversarial Reviewer    : llama3.2:3b  (~2.0 GB model)"
Write-Host "  Independent Verifier    : gemma3:4b    (~3.3 GB model)"
Write-Host "  Required model payload  : ~7.8 GB"
Write-Host "  Clean-install free space: >=20 GB (25 GB recommended for project growth)"
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install Docker Desktop, start it, then run this script again."
}

try {
    docker info | Out-Null
} catch {
    throw "Docker is installed but the Docker engine is not running. Start Docker Desktop and retry."
}

$RequiredFreeGB = if ($env:AWB_INSTALL_FREE_GB) { [int]$env:AWB_INSTALL_FREE_GB } else { 20 }
if ($RequiredFreeGB -lt 1) {
    throw "AWB_INSTALL_FREE_GB must be a positive integer number of GB."
}

$LocationDrive = (Get-Location).Drive
$SystemDriveName = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd(":") } else { $LocationDrive.Name }
$Drive = Get-PSDrive -Name $SystemDriveName -ErrorAction SilentlyContinue
if (-not $Drive) {
    $Drive = $LocationDrive
}
if (-not $Drive) {
    throw "Could not determine free space for the Docker host drive."
}
$FreeGB = [math]::Floor($Drive.Free / 1GB)
Write-Host "Disk preflight: $FreeGB GB free on host drive $($Drive.Name): (minimum $RequiredFreeGB GB)."
if ($FreeGB -lt $RequiredFreeGB) {
    throw "Not enough free space for a clean multi-model installation. Free at least $RequiredFreeGB GB; 25 GB is recommended for workspace growth."
}

$PlannerModel = if ($env:AWB_PLANNER_MODEL) { $env:AWB_PLANNER_MODEL } else { "qwen3:4b" }
$ConstructiveFallback = if ($env:AWB_LOCAL_MODEL) { $env:AWB_LOCAL_MODEL } else { "qwen3:4b" }
$DirectorModel = if ($env:AWB_DIRECTOR_MODEL) { $env:AWB_DIRECTOR_MODEL } else { $ConstructiveFallback }
$WorkerModel = if ($env:AWB_WORKER_MODEL) { $env:AWB_WORKER_MODEL } else { $ConstructiveFallback }
$ReviewerModel = if ($env:AWB_REVIEWER_MODEL) { $env:AWB_REVIEWER_MODEL } else { "llama3.2:3b" }
$VerifierModel = if ($env:AWB_VERIFIER_MODEL) { $env:AWB_VERIFIER_MODEL } else { "gemma3:4b" }

Write-Host "Starting Expert My Rules and Ollama..."
docker compose up -d --build

$Models = New-Object System.Collections.Generic.List[string]
@("qwen3:4b", "llama3.2:3b", "gemma3:4b", $PlannerModel, $DirectorModel, $WorkerModel, $ReviewerModel, $VerifierModel) |
    ForEach-Object {
        if (-not $Models.Contains($_)) { $Models.Add($_) }
    }

Write-Host "Downloading/checking required local models..."
foreach ($Model in $Models) {
    Write-Host "  -> $Model"
    docker compose exec -T ollama ollama pull $Model
    if ($LASTEXITCODE -ne 0) { throw "Failed to download Ollama model: $Model" }
}

Write-Host "Installed Ollama models:"
docker compose exec -T ollama ollama list

Write-Host "Checking services..."
docker compose ps

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8000" -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    Write-Warning "The containers started, but the web UI did not answer on port 8000 yet. Run: docker compose logs expert-my-rules"
    exit 1
}

Write-Host ""
Write-Host "READY" -ForegroundColor Green
Write-Host "Open on this PC: http://localhost:8000"
Write-Host "For iPad/another device, use: http://<SERVER-IP>:8000"
Write-Host "Role routing: Worker=$WorkerModel | Reviewer=$ReviewerModel | Verifier=$VerifierModel"
Write-Host "Ollama is limited to one loaded model at a time by default to protect small-device RAM."
Write-Host "Cloud/API escalation remains OFF unless you explicitly configure it."
