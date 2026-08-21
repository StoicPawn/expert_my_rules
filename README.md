# Expert My Rules

**Tell it what done looks like.**

Expert My Rules is a local-first autonomous project workbench. The normal workflow is deliberately simple:

1. Give one final goal.
2. The local planner proposes the project type, expert team and Definition of Done.
3. Edit those conditions if you want.
4. Press **Start autonomous project**.
5. The project keeps taking checkpointed autonomous sessions until every required completion gate passes, or you pause/cancel it.

The core loop is:

`Goal → Director → Worker → independent adversarial Reviewer → external verification → persistent ledger → next task`

The Director may choose new tasks and strategies, but it is explicitly forbidden from weakening the North Star or moving the completion criteria merely to declare success.

## What runs where

With the recommended Docker setup, the same PC runs two persistent services:

```text
Acer / home server
├─ expert-my-rules
│  ├─ web dashboard :8000
│  ├─ orchestrator
│  ├─ project ledger / artifacts
│  └─ model router
└─ ollama
   ├─ local LLM server :11434 (internal Docker network)
   └─ downloaded model weights in a persistent Docker volume
```

Expert My Rules sends normal model requests to `http://ollama:11434`. Ollama does **not** continuously generate tokens: it stays available as a service and performs inference only when the planner or an agent asks for it. The model weights are downloaded once and persisted in the `ollama-models` Docker volume.

Your browser or iPad is only a client. Closing Safari does not move the computation to the iPad and does not stop a running server-side project.

## Start on a small Acer now

Yes: the intended first deployment is a normal always-on PC such as an Acer. It is the same complete system, only with a smaller local model and therefore less reasoning speed/capability than a future GPU server.

Recommended first setup:

```bash
git clone https://github.com/StoicPawn/expert_my_rules.git
cd expert_my_rules
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:8b
```

Then open:

- on the Acer: `http://localhost:8000`
- from an iPad/PC on the same private network: `http://<ACER-IP>:8000`

Check the services:

```bash
docker compose ps
docker compose exec ollama ollama list
```

The default local model is `qwen3:8b`. If the Acer has limited RAM, use a smaller Ollama model and set `AWB_LOCAL_MODEL` / `AWB_PLANNER_MODEL` accordingly. If it has more RAM or a useful GPU, use a larger model. The application architecture does not change.

Practical hardware rule: the machine must have enough RAM/VRAM for the chosen Ollama model. CPU-only inference works but can be slow. A GPU improves speed substantially; it is not required for the framework itself.

## Local-first by default

The default model provider is **Ollama**. Cloud escalation exists in the architecture but is OFF by default:

```yaml
escalation:
  enabled: false
  daily_budget_eur: 0
  max_cloud_calls_per_run: 0
```

An OpenAI escalation can occur only if all of these are true:

- escalation is enabled for the workspace;
- the configured cloud budget is greater than zero;
- a positive cloud-call cap is configured;
- `OPENAI_API_KEY` exists in the environment;
- the task is important enough or has failed locally enough times according to policy.

Therefore a fresh installation makes **zero API calls**. ChatGPT subscriptions are separate from API billing; Expert My Rules never assumes an API entitlement.

### What happens today without an API account

Nothing special is required. Leave the defaults unchanged. Every planner/Director/Worker/Reviewer/verifier call stays on Ollama. The cloud router exists but cannot activate because its budget and call cap are zero and no API key is required.

### How API escalation works later

When you later create an OpenAI API account, set the key only in the server environment (never in `project.yaml`) and explicitly give the workspace a positive budget/call cap:

```bash
export OPENAI_API_KEY="..."
awb cloud workspaces/<project> \
  --enabled \
  --daily-budget 2 \
  --max-calls 5 \
  --model gpt-5
```

The router still uses Ollama normally. It may escalate only under the configured policy, for example after repeated local failures or for a high-priority independent review. Every escalation is recorded in the ledger.

## H24 server setup

Docker Compose is the recommended deployment. It starts both Expert My Rules and Ollama, persists workspaces and model files, and restarts services automatically with `restart: unless-stopped`.

The server can remain online H24. When no project is working, Ollama performs no inference. When you launch or resume a project, the local agents call Ollama on demand.

Workspace state lives under `./workspaces`; local model files live in the persistent `ollama-models` Docker volume. Rebuilding the application container therefore does not erase your projects or downloaded Ollama models.

## Scale later without changing projects

The deployment is deliberately portable. A project contains its goal, proposed team, gates, ledger and artifacts independently from the machine that executes it.

### Stage 1 — small Acer

```text
iPad/PC → Acer
          ├─ Expert My Rules
          └─ Ollama + small/medium local model
```

Use this now. It costs no per-token API money and is enough to validate workflows and run lighter autonomous projects.

### Stage 2 — stronger physical server

Move the repo/workspaces to a machine with more RAM and/or a GPU, keep the same Docker architecture and use a stronger Ollama model:

```text
iPad/PC → GPU server
          ├─ Expert My Rules
          └─ Ollama + larger local model
```

No project format change is required.

### Stage 3 — hybrid local + API

Keep local inference as the default but allow expensive frontier-model calls only when the escalation policy permits them:

```text
                    ┌→ Ollama local (default)
iPad → AWB router ──┤
                    └→ OpenAI API (rare, budgeted escalation)
```

### Stage 4 — cloud/server migration

Expert My Rules and/or Ollama can be moved to a private cloud VM/GPU server. The UI remains the same; only deployment URLs, storage and security/network configuration change. Keep the service private or behind VPN/authentication/TLS rather than directly exposing the current trusted-LAN dashboard to the public Internet.

## Goal-first creation

From the dashboard, the main creation screen asks essentially one question:

> What must exist when this project is truly finished?

For example:

> Obtain a rigorous, novel result strong enough for a submission-ready Annals of Probability paper.

The local planner infers a research workspace and proposes agents and completion conditions. If Ollama is temporarily unavailable during creation, a deterministic domain template is used instead; project creation never falls back to a paid API.

CLI equivalent:

```bash
awb create --goal "Obtain a rigorous, novel result strong enough for a submission-ready Annals of Probability paper"
```

Advanced users can still create explicit templates with `awb init`.

## Definition of Done

Completion is gate-based, not based on an LLM saying `DONE`.

A research workspace typically proposes conditions such as:

- central result proved or strongest valid replacement established;
- no unresolved fatal adversarial objection;
- novelty/priority checked against literature;
- major claims verified at the strongest available level;
- complete manuscript package ready for expert submission review.

A software workspace typically proposes:

- tests pass;
- no critical bugs remain;
- requested acceptance criteria are satisfied;
- release/install/run package is complete.

You can modify or add gates before launch.

## Continuous autonomous projects

From the UI press **Start autonomous project**, or from the CLI:

```bash
awb launch workspaces/<project>
```

A continuous project is split internally into bounded checkpoint sessions. This avoids one unbounded context/run while preserving the project-level rule: **keep working until the required gates pass**.

Continuous job state is persisted in SQLite. The dashboard provides pause, resume and cancel controls. On service startup, running continuous jobs are recovered and resumed.

You can still inject a temporary directive without changing the North Star, e.g.:

> Tonight attack the converse by searching for counterexamples first.

## Tool Layer

Agents receive capabilities explicitly. Tools are deny-by-default.

Current primitives include:

- workspace file listing;
- sandboxed file reads;
- sandboxed file writes;
- fixed configured shell commands such as tests.

A model cannot invent an arbitrary shell command just because a shell tool exists; the command is configured in the workspace.

## Installation without Docker

Python 3.11+ is supported.

macOS/Linux:

```bash
./install.sh
. .venv/bin/activate
awb serve --host 0.0.0.0 --port 8000
```

Windows PowerShell:

```powershell
./install.ps1
.\.venv\Scripts\awb serve --host 0.0.0.0 --port 8000
```

For native installation, install/run Ollama separately and point `OLLAMA_BASE_URL` to it. Docker is simpler because the bundled Compose file already connects the two services internally.

## Important boundary

Expert My Rules can automate research, implementation, criticism and verification workflows, but high-stakes claims remain subject to the strength of the configured validators. In mathematical research, several agreeing LLMs are not a formal proof checker. The system is designed to make unsupported completion difficult and to preserve the complete evidence/objection trail, not to replace external mathematical or scientific validation.

See `ARCHITECTURE.md` and `REMOTE_ACCESS.md` for architecture and private-network deployment guidance.
