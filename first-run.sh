#!/usr/bin/env sh
set -eu

printf '%s\n' 'Expert My Rules - first run'

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker was not found. Install Docker Engine/Desktop with Compose, start it, then rerun this script.' >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo 'Docker is installed but the Docker engine is not running.' >&2
  exit 1
fi

MODEL="${AWB_LOCAL_MODEL:-qwen3:8b}"

echo 'Starting Expert My Rules and Ollama...'
docker compose up -d --build

echo "Downloading/checking local model: $MODEL"
docker compose exec -T ollama ollama pull "$MODEL"

echo 'Checking services...'
docker compose ps

READY=0
i=0
while [ "$i" -lt 30 ]; do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000', timeout=2)" >/dev/null 2>&1; then
    READY=1
    break
  fi
  i=$((i+1))
  sleep 2
done

if [ "$READY" -ne 1 ]; then
  echo 'Containers started, but the web UI did not answer on port 8000 yet.' >&2
  echo 'Inspect with: docker compose logs expert-my-rules' >&2
  exit 1
fi

printf '\nREADY\n'
echo 'Open on this PC: http://localhost:8000'
echo 'For iPad/another device: http://<SERVER-IP>:8000'
echo "Local model: $MODEL"
echo 'Cloud/API escalation remains OFF unless explicitly configured.'
