# Expert My Rules

**Tell it what done looks like.**

Expert My Rules is a local-first autonomous project workbench. You give a final goal; the system proposes the project type, expert team and Definition of Done; you may edit them; then the project keeps working in checkpointed autonomous sessions until all required completion gates pass, or you pause/cancel it.

Core loop:

`Goal → Director → Worker → independent adversarial Reviewer → verification/gatekeeper → persistent ledger → next task`

The Director may choose new tasks and strategies, but it is explicitly forbidden from weakening the North Star or moving the completion criteria merely to declare success.

## Default local ensemble: three different model families

A clean installation now includes **at least three distinct local models** and assigns them to different epistemic roles by default:

| Role | Default local model | Purpose |
| --- | --- | --- |
| Planner / Director / Worker | `qwen3:4b` | planning and constructive work |
| Adversarial Reviewer | `llama3.2:3b` | independent hostile review / falsification |
| Verifier | `gemma3:4b` | conservative evidence and completion checks |

This is intentional. Different prompts on one model are useful, but they can share correlated blind spots. Expert My Rules therefore defaults to different model families for construction, challenge and verification.

The models still are not statistically independent and multi-model agreement is **not** a formal proof. Strong validators remain preferable whenever available.

### Disk space to reserve

Approximate current Ollama model payload:

- `qwen3:4b`: ~2.5 GB
- `llama3.2:3b`: ~2.0 GB
- `gemma3:4b`: ~3.3 GB
- required model payload: **~7.8 GB**

A clean Docker installation needs additional space for the Ollama image, the Expert My Rules image, extracted layers, caches, logs and workspaces.

**Before first run, reserve at least 20 GB of free disk space. 25 GB is recommended.**

The bootstrap scripts perform a disk preflight and refuse a clean install below **20 GB free** by default. Override only if you understand the existing Docker/model cache state:

```bash
export AWB_INSTALL_FREE_GB=15
```

or on PowerShell:

```powershell
$env:AWB_INSTALL_FREE_GB="15"
```

For small devices, Docker also defaults Ollama to `OLLAMA_MAX_LOADED_MODELS=1` and `OLLAMA_NUM_PARALLEL=1`: the three models are stored on disk, but only one is intended to be resident at a time. This reduces RAM/VRAM pressure at the cost of model-switch latency.

## Fastest first run on the Acer

### 1. Requirements

Install and start:

- Git
- Docker Desktop on Windows, or Docker Engine + Compose on Linux
- **20 GB free disk space minimum** for a clean installation

You do **not** need Python, Ollama or an OpenAI API account when using the Docker setup. Docker runs both Expert My Rules and Ollama.

### 2. Clone

```powershell
git clone https://github.com/StoicPawn/expert_my_rules.git
cd expert_my_rules
```

### 3. One-command bootstrap

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\first-run.ps1
```

Linux/macOS:

```bash
chmod +x first-run.sh
./first-run.sh
```

The bootstrap:

1. checks Docker;
2. reports the three default models and installation disk budget;
3. checks free disk space;
4. builds/starts Expert My Rules + Ollama;
5. downloads `qwen3:4b`, `llama3.2:3b` and `gemma3:4b`;
6. downloads any additional role-specific model overrides;
7. lists installed Ollama models;
8. verifies that the web UI answers.

### 4. Open the UI

- server itself: `http://localhost:8000`
- iPad/other PC on the same private network: `http://<SERVER-IP>:8000`

The first model downloads can be slow. Switching between three model families on a small CPU-only machine can also be slow; this is a performance trade-off for greater epistemic diversity.

## Model routing and overrides

The default role routing is:

```text
Planner / Director / Worker → qwen3:4b
Reviewer                    → llama3.2:3b
Verifier                    → gemma3:4b
```

Role-specific environment variables:

```text
AWB_PLANNER_MODEL
AWB_DIRECTOR_MODEL
AWB_WORKER_MODEL
AWB_REVIEWER_MODEL
AWB_VERIFIER_MODEL
```

`AWB_LOCAL_MODEL` remains as a legacy constructive-model override for Director/Worker. It does **not** silently replace Reviewer or Verifier defaults, because doing so would collapse model diversity.

Example:

```bash
export AWB_WORKER_MODEL=qwen3:8b
export AWB_REVIEWER_MODEL=llama3.2:3b
export AWB_VERIFIER_MODEL=gemma3:4b
./first-run.sh
```

The installer still keeps the three baseline local families available.

## What runs where

With Docker Compose:

```text
Home server
├─ expert-my-rules
│  ├─ web dashboard :8000
│  ├─ orchestrator
│  ├─ project ledger / artifacts
│  └─ model router
└─ ollama
   ├─ local LLM server :11434 (private Docker network)
   ├─ qwen3:4b
   ├─ llama3.2:3b
   ├─ gemma3:4b
   └─ persistent model volume
```

Expert My Rules talks to Ollama at `http://ollama:11434`. Ollama is a service, not a continuously running generation job: when no project requests inference it does not keep producing tokens. The browser is only a client; closing it does not stop server-side work.

Both services use `restart: unless-stopped`. Project state is persisted in SQLite and recoverable jobs are resumed when the application service starts again.

## Goal-first project creation

From the dashboard, the main input is simply:

> What must exist when this project is truly finished?

Examples:

> Obtain a rigorous, novel result strong enough for a submission-ready Annals of Probability paper.

> Deliver an installable application that performs X, Y and Z, passes its test suite and satisfies these acceptance criteria.

The local planner proposes:

- workspace type (`research`, `software`, `custom`);
- agents and instructions;
- Definition of Done / completion gates.

You can edit the proposal before launch.

Planner-generated roles are re-attached to the configured per-role models after planning. This prevents an automatically generated team from accidentally reverting Director, Worker, Reviewer and Verifier to a single common model.

Then press **Start autonomous project**. The project remains active across bounded checkpoint sessions until all required gates pass or you pause/cancel it.

You can inject temporary directives without changing the North Star, for example:

> Tonight attack the converse by searching for counterexamples first.

## Completion is gate-based

The system does not finish because an LLM says `DONE`.

A research project may require conditions such as:

- central result proved or strongest valid replacement established;
- no unresolved fatal adversarial objection;
- novelty/priority checked against literature;
- major claims verified at the strongest available level;
- complete manuscript package ready for expert submission review.

A software project may require:

- requested functionality implemented;
- tests pass;
- no critical bugs remain;
- acceptance criteria satisfied;
- install/run/release package complete.

Semantic LLM gatekeeping is useful but is not a substitute for stronger validators such as formal proof checking, external literature retrieval or executable tests when those are required.

## API / cloud escalation

Fresh installations make **zero OpenAI API calls**.

Cloud escalation exists in the architecture but defaults to:

```yaml
escalation:
  enabled: false
  daily_budget_eur: 0
  max_cloud_calls_per_run: 0
```

A cloud call is possible only when all relevant conditions hold:

1. the workspace explicitly enables escalation;
2. a positive daily monetary budget is configured;
3. a positive cloud-call cap is configured;
4. `OPENAI_API_KEY` exists in the server environment;
5. the routing policy decides the task merits escalation.

Without an API account/key and positive budget, the router stays local.

A ChatGPT subscription is separate from OpenAI API billing.

## H24 usage

The intended deployment is an always-on home server:

```text
iPad / laptop
      ↓
private LAN or VPN
      ↓
server
├─ Expert My Rules
└─ Ollama local ensemble
```

You create/inspect projects from the browser and leave the machine doing the work. Do not expose the current dashboard directly to the public Internet; for remote access use a private VPN/network and add authentication/TLS before any public-facing deployment.

## Scaling path

### Stage 1 — small server

```text
browser → small server → Expert My Rules + 3 small local models
```

Zero per-token API cost. Model diversity is preserved, but inference and model swapping may be slow.

### Stage 2 — stronger physical server

Move `expert_my_rules` and the `workspaces/` directory to a machine with more RAM and/or GPU. Keep the same Compose architecture and assign stronger models to the same roles.

### Stage 3 — hybrid

Keep the local ensemble as default and enable budgeted API escalation only for selected difficult tasks/reviews.

### Stage 4 — cloud/private GPU server

Run the same containers/storage on a private cloud VM or GPU server. The UI and workspace format remain the same.

## Data and persistence

Important state is separated from the application container:

- `./workspaces/` — project manifests, SQLite ledgers, artifacts and logs;
- Docker volume `ollama-models` — downloaded local LLM weights.

Rebuilding/updating the application container does not delete either location.

### Backup projects

```bash
docker compose stop
# copy/archive ./workspaces
docker compose start
```

Ollama models can be re-downloaded, so backing up the model volume is optional.

## Updating later

```bash
git pull
docker compose up -d --build
```

If new mandatory baseline models are introduced in a future release, run `first-run.sh` / `first-run.ps1` again to perform the model preflight and pulls.

## Diagnostics

Service state:

```bash
docker compose ps
```

Installed local models:

```bash
docker compose exec ollama ollama list
```

Application logs:

```bash
docker compose logs -f expert-my-rules
```

Ollama logs:

```bash
docker compose logs -f ollama
```

Restart:

```bash
docker compose restart
```

Stop without deleting data:

```bash
docker compose stop
```

Start again:

```bash
docker compose start
```

Do **not** use `docker compose down -v` unless you intentionally want to remove persistent Docker volumes, including downloaded Ollama models.

## Native installation (optional)

Docker is the recommended route. A native Python 3.11+ installation is also supported.

Windows:

```powershell
.\install.ps1
.\.venv\Scripts\awb serve --host 0.0.0.0 --port 8000
```

Linux/macOS:

```bash
./install.sh
. .venv/bin/activate
awb serve --host 0.0.0.0 --port 8000
```

For native installation you must install/run Ollama separately, configure `OLLAMA_BASE_URL`, and make sure the role models are installed yourself.

## Current validation boundary

Expert My Rules automates orchestration, persistent research/software work, adversarial review and configured verification. Model heterogeneity is a defense against correlated blind spots, not a correctness guarantee.

For high-stakes outputs, quality is limited by the validators/tools available to the workspace. Multiple agreeing LLM families do not constitute a formal mathematical proof or an exhaustive novelty search. Stronger adapters—literature retrieval, Lean, SymPy/numerical counterexample search, Git worktrees, browser tests and other domain tools—remain the natural next layer.

See `ARCHITECTURE.md` and `REMOTE_ACCESS.md` for more detail.
