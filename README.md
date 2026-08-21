# Expert My Rules

**Tell it what done looks like.**

Expert My Rules is a local-first autonomous project workbench. You give a final goal; the system proposes the project type, expert team and Definition of Done; you may edit them; then the project keeps working in checkpointed autonomous sessions until all required completion gates pass, or you pause/cancel it.

Core loop:

`Goal → Director → Worker → independent adversarial Reviewer → verification/gatekeeper → persistent ledger → next task`

The Director may choose new tasks and strategies, but it is explicitly forbidden from weakening the North Star or moving the completion criteria merely to declare success.

## Fastest first run on the Acer (recommended)

### 1. Requirements

Install and start:

- Git
- Docker Desktop on Windows, or Docker Engine + Compose on Linux

You do **not** need Python, Ollama or an OpenAI API account when using the Docker setup. Docker runs both Expert My Rules and Ollama.

### 2. Clone

```powershell
git clone https://github.com/StoicPawn/expert_my_rules.git
cd expert_my_rules
```

### 3. One-command Windows bootstrap

```powershell
powershell -ExecutionPolicy Bypass -File .\first-run.ps1
```

The script checks Docker, builds/starts the two services, downloads the default local model and verifies that the web UI answers.

macOS/Linux equivalent:

```bash
chmod +x first-run.sh
./first-run.sh
```

### 4. Open the UI

- Acer itself: `http://localhost:8000`
- iPad/other PC on the same private network: `http://<ACER-IP>:8000`

If the Acer is modest, the first model download and first inference can be slow. This affects model speed/capability, not the project format or orchestration architecture.

## What runs where

With Docker Compose, the Acer/server runs two persistent containers:

```text
Acer / home server
├─ expert-my-rules
│  ├─ web dashboard :8000
│  ├─ orchestrator
│  ├─ project ledger / artifacts
│  └─ model router
└─ ollama
   ├─ local LLM server :11434 (private Docker network)
   └─ model weights in a persistent Docker volume
```

Expert My Rules talks to Ollama at `http://ollama:11434`. Ollama is a service, not a continuously running generation job: when no project requests inference it does not keep producing tokens. The iPad/browser is only a client; closing Safari does not stop server-side work.

Both services use `restart: unless-stopped` and health checks. Continuous project state is persisted in SQLite and recoverable jobs are resumed when the application service starts again.

## Default local model and a smaller Acer

The default is:

```text
qwen3:8b
```

To start with another Ollama model, set the environment variable before first run.

Windows PowerShell example:

```powershell
$env:AWB_LOCAL_MODEL="qwen3:4b"
$env:AWB_PLANNER_MODEL="qwen3:4b"
powershell -ExecutionPolicy Bypass -File .\first-run.ps1
```

Linux/macOS:

```bash
export AWB_LOCAL_MODEL=qwen3:4b
export AWB_PLANNER_MODEL=qwen3:4b
./first-run.sh
```

The exact model you should use depends on the Acer's RAM/VRAM and CPU/GPU. CPU-only inference works but may be slow. Moving later to a larger model does not change existing projects.

## Goal-first project creation

From the dashboard, the main input is simply:

> What must exist when this project is truly finished?

Examples:

> Obtain a rigorous, novel result strong enough for a submission-ready Annals of Probability paper.

> Deliver an installable application that performs X, Y and Z, passes its test suite and satisfies these acceptance criteria.

The local planner proposes:

- workspace type (`research`, `software`, `custom`);
- agents and their instructions;
- Definition of Done / completion gates.

You can edit the proposal before launch. If Ollama is temporarily unavailable during creation, a deterministic template is used; project creation never falls back automatically to a paid API.

Then press **Start autonomous project**. The project remains active across bounded checkpoint sessions until all required gates pass or you pause/cancel it.

You can also inject temporary directives without changing the North Star, for example:

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
5. the routing policy decides the task merits escalation, e.g. repeated local failure or a configured high-priority independent review.

Therefore, without an API account/key and positive budget, **the router always stays local**.

A ChatGPT subscription is separate from OpenAI API billing.

Later, after creating an API account:

```bash
export OPENAI_API_KEY="..."
awb cloud workspaces/<project> --enabled --daily-budget 2 --max-calls 5 --model gpt-5
```

Ollama remains the default path; expensive calls are exceptional and logged in the project ledger.

## H24 usage

The intended deployment is an always-on Acer/home server.

```text
iPad / laptop
      ↓
private LAN or VPN
      ↓
Acer
├─ Expert My Rules
└─ Ollama
```

You create/inspect projects from the browser and leave the machine doing the work. Do not expose the current dashboard directly to the public Internet; for remote access use a private VPN/network and add authentication/TLS before any public-facing deployment.

## Scaling path

The project format is deliberately machine-independent.

### Stage 1 — Acer now

```text
iPad → Acer → Expert My Rules + small/medium Ollama model
```

Zero per-token API cost. Good for validating the framework and running tasks within the local model's capability.

### Stage 2 — stronger physical server

Move `expert_my_rules` and the `workspaces/` directory to a machine with more RAM and/or GPU. Keep the same Compose architecture, download a stronger Ollama model and continue the same projects.

### Stage 3 — hybrid

Keep Ollama as default and enable budgeted API escalation only for selected difficult tasks/reviews.

```text
                    ┌→ Ollama local (default)
iPad → AWB router ──┤
                    └→ OpenAI API (rare, budgeted)
```

### Stage 4 — cloud/private GPU server

Run the same containers/storage on a private cloud VM or GPU server. The UI and workspace format remain the same; networking, security and storage become production-grade.

## Data and persistence

Important state is separated from the application container:

- `./workspaces/` — project manifests, SQLite ledgers, artifacts and logs;
- Docker volume `ollama-models` — downloaded local LLM weights.

Rebuilding/updating the application container does not delete either location.

### Backup projects

The important backup is simply the `workspaces/` directory.

Before major migrations, stop the stack and copy it:

```bash
docker compose stop
# copy/archive ./workspaces
docker compose start
```

Ollama models can always be re-downloaded, so backing up the model volume is optional.

## Updating later

From the repository directory:

```bash
git pull
docker compose up -d --build
```

Your workspace directory and model volume remain persistent.

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

macOS/Linux:

```bash
./install.sh
. .venv/bin/activate
awb serve --host 0.0.0.0 --port 8000
```

For native installation you must install/run Ollama separately and configure `OLLAMA_BASE_URL` yourself.

## Current validation boundary

Expert My Rules automates orchestration, persistent research/software work, adversarial review and configured verification. For high-stakes outputs, quality is limited by the validators/tools available to the workspace. Several agreeing LLM agents do not constitute a formal mathematical proof or an exhaustive novelty search. The architecture is designed so stronger adapters—literature retrieval, Lean, SymPy/numerical counterexample search, Git worktrees, browser tests and other domain tools—can be added without changing the goal-first core.

See `ARCHITECTURE.md` and `REMOTE_ACCESS.md` for more detail.
