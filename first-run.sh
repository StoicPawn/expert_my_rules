#!/usr/bin/env sh
set -eu

printf '%s\n' 'Expert My Rules - first run'
printf '%s\n' 'Default local ensemble:'
printf '%s\n' '  Director/Worker/Planner : qwen3:4b      (~2.5 GB model)'
printf '%s\n' '  Adversarial Reviewer    : llama3.2:3b  (~2.0 GB model)'
printf '%s\n' '  Independent Verifier    : gemma3:4b    (~3.3 GB model)'
printf '%s\n' '  Required model payload  : ~7.8 GB'
printf '%s\n' '  Clean-install free space: >=20 GB (25 GB recommended for project growth)'
printf '\n'

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker was not found. Install Docker Engine/Desktop with Compose, start it, then rerun this script.' >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo 'Docker is installed but the Docker engine is not running.' >&2
  exit 1
fi

REQUIRED_FREE_GB="${AWB_INSTALL_FREE_GB:-20}"
case "$REQUIRED_FREE_GB" in
  ''|*[!0-9]*) echo 'AWB_INSTALL_FREE_GB must be an integer number of GB.' >&2; exit 1 ;;
esac

# Prefer Docker's storage filesystem on native Linux. Docker Desktop may expose
# a path that is not mounted on the host, so fall back to the current drive.
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
CHECK_PATH='.'
if [ -n "$DOCKER_ROOT" ] && [ -d "$DOCKER_ROOT" ]; then
  CHECK_PATH="$DOCKER_ROOT"
fi
FREE_KB="$(df -Pk "$CHECK_PATH" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
if [ -z "$FREE_KB" ]; then
  CHECK_PATH='.'
  FREE_KB="$(df -Pk "$CHECK_PATH" | awk 'NR==2 {print $4}')"
fi
FREE_GB=$((FREE_KB / 1024 / 1024))
echo "Disk preflight: ${FREE_GB} GB free on filesystem used for install check (minimum ${REQUIRED_FREE_GB} GB)."
if [ "$FREE_GB" -lt "$REQUIRED_FREE_GB" ]; then
  echo "Not enough free space for a clean multi-model installation." >&2
  echo "Free at least ${REQUIRED_FREE_GB} GB, then rerun. 25 GB is recommended for workspace growth." >&2
  exit 1
fi

PLANNER_MODEL="${AWB_PLANNER_MODEL:-qwen3:4b}"
DIRECTOR_MODEL="${AWB_DIRECTOR_MODEL:-${AWB_LOCAL_MODEL:-qwen3:4b}}"
WORKER_MODEL="${AWB_WORKER_MODEL:-${AWB_LOCAL_MODEL:-qwen3:4b}}"
REVIEWER_MODEL="${AWB_REVIEWER_MODEL:-llama3.2:3b}"
VERIFIER_MODEL="${AWB_VERIFIER_MODEL:-gemma3:4b}"

echo 'Starting Expert My Rules and Ollama...'
docker compose up -d --build

# Always install the three default independent families. Add any role overrides
# too, without downloading duplicates.
TO_PULL='qwen3:4b llama3.2:3b gemma3:4b'
for model in "$PLANNER_MODEL" "$DIRECTOR_MODEL" "$WORKER_MODEL" "$REVIEWER_MODEL" "$VERIFIER_MODEL"; do
  case " $TO_PULL " in
    *" $model "*) ;;
    *) TO_PULL="$TO_PULL $model" ;;
  esac
done

echo 'Downloading/checking required local models...'
for model in $TO_PULL; do
  echo "  -> $model"
  docker compose exec -T ollama ollama pull "$model"
done

echo 'Installed Ollama models:'
docker compose exec -T ollama ollama list

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
echo "Role routing: Worker=$WORKER_MODEL | Reviewer=$REVIEWER_MODEL | Verifier=$VERIFIER_MODEL"
echo 'Ollama is limited to one loaded model at a time by default to protect small-device RAM.'
echo 'Cloud/API escalation remains OFF unless explicitly configured.'
