# Native Windows installation (no Docker)

Use this path when Docker Desktop cannot be installed or is blocked by policy.

## What is installed

The native installer uses Python 3.11+ and Ollama for Windows. It always installs/checks these baseline local models:

- `qwen3:4b` for Planner / Director / Worker
- `llama3.2:3b` for the adversarial Reviewer
- `gemma3:4b` for the independent Verifier

The model payload is approximately 7.8 GB. Reserve at least **15 GB free** for a native installation; **20 GB is recommended** for model cache, Python environment, logs and project workspaces.

## Install and test

Open PowerShell in the repository folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -TestModels
```

The script:

1. checks free disk space;
2. finds Python 3.11+ or installs Python 3.12 with `winget` when available;
3. creates `.venv` and installs Expert My Rules;
4. finds Ollama or installs `Ollama.Ollama` with `winget` when available;
5. starts the local Ollama server if needed;
6. downloads the three baseline model families;
7. runs one real inference smoke test on each model when `-TestModels` is supplied.

If company policy blocks `winget`, install Python 3.11+ and Ollama manually, then rerun `install.ps1`.

Official Ollama Windows download: https://ollama.com/download/windows

## Start the dashboard

```powershell
.\.venv\Scripts\awb.exe serve --host 127.0.0.1 --port 8000
```

Then open:

```text
http://localhost:8000
```

No Docker service is involved. Expert My Rules talks directly to the native Ollama API at `http://127.0.0.1:11434`.

## Smaller-machine behavior

The installer starts a new Ollama server with `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1` when no server is already running. The smoke test also uses `keep_alive=0` so each model is unloaded after its check.

An Ollama process that was already running before the installer may need to be restarted to inherit those environment settings.
