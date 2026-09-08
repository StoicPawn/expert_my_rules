#!/usr/bin/env sh
set -eu

MODE="${1:-}"
REMOTE_URL="${2:-}"
ENV_FILE=".env"

set_key() {
  key="$1"
  value="$2"
  touch "$ENV_FILE"
  if grep -q "^${key}=" "$ENV_FILE"; then
    tmp="${ENV_FILE}.tmp.$$"
    awk -F= -v k="$key" -v v="$value" 'BEGIN{OFS="="} $1==k {$0=k OFS v} {print}' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

case "$MODE" in
  local)
    set_key AWB_INFERENCE_MODE local
    set_key OLLAMA_BASE_URL http://ollama:11434
    echo 'Switching to bundled local Ollama...'
    docker compose --profile local-inference up -d ollama
    docker compose --profile local-inference up -d --build expert-my-rules
    echo 'Local inference enabled. Existing model volume is preserved.'
    ;;
  remote)
    if [ -z "$REMOTE_URL" ]; then
      echo 'Usage: ./switch-inference.sh remote http://<PRIVATE-IP>:11434' >&2
      exit 1
    fi
    case "$REMOTE_URL" in
      http://ollama:11434|http://127.0.0.1:*|http://localhost:*)
        echo 'Remote URL must point to another machine.' >&2
        exit 1
        ;;
    esac
    set_key AWB_INFERENCE_MODE remote
    set_key OLLAMA_BASE_URL "$REMOTE_URL"
    echo "Switching Expert My Rules to remote Ollama at $REMOTE_URL..."
    docker compose stop ollama >/dev/null 2>&1 || true
    docker compose up -d --build expert-my-rules
    echo 'Remote inference enabled. Local model volume is preserved but unused.'
    echo 'After you verify the remote worker, local model storage can be deleted separately.'
    ;;
  *)
    echo 'Usage:' >&2
    echo '  ./switch-inference.sh local' >&2
    echo '  ./switch-inference.sh remote http://<PRIVATE-IP>:11434' >&2
    exit 1
    ;;
esac

docker compose --profile local-inference ps
