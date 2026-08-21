$ErrorActionPreference = "Stop"

Write-Host "Expert My Rules - first run" -ForegroundColor Cyan

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install Docker Desktop, start it, then run this script again."
}

try {
    docker info | Out-Null
} catch {
    throw "Docker is installed but the Docker engine is not running. Start Docker Desktop and retry."
}

$Model = if ($env:AWB_LOCAL_MODEL) { $env:AWB_LOCAL_MODEL } else { "qwen3:8b" }

Write-Host "Starting Expert My Rules and Ollama..."
docker compose up -d --build

Write-Host "Downloading/checking local model: $Model"
docker compose exec -T ollama ollama pull $Model

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
Write-Host "For iPad/another device, use: http://<ACER-IP>:8000"
Write-Host "Local model: $Model"
Write-Host "Cloud/API escalation remains OFF unless you explicitly configure it."
