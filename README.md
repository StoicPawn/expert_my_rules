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

## H24 server setup

Docker Compose is the recommended deployment. It starts both Expert My Rules and Ollama, persists workspaces and model files, and restarts services automatically.

```bash
git clone https://github.com/StoicPawn/expert_my_rules.git
cd expert_my_rules
docker compose up -d --build
```

Pull the local model once:

```bash
docker compose exec ollama ollama pull qwen3:8b
```

Then open:

- PC: `http://localhost:8000`
- iPad/another device on the same private network: `http://<SERVER-IP>:8000`

The server can remain online H24. Ollama is a service: it only performs inference when a project asks for it.

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

## Model escalation

Local inference is the normal path. A future API budget can be enabled per workspace from the UI or CLI:

```bash
awb cloud workspaces/<project> \
  --enabled \
  --daily-budget 2 \
  --max-calls 5 \
  --model gpt-5
```

With the current default (`enabled=false`, budget `0`, max calls `0`) it cannot escalate.

The router considers cloud escalation only for configured roles and only after sufficient local failures or for high-priority tasks. Every escalation is written into the event ledger.

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

For native installation, run Ollama separately and point `OLLAMA_BASE_URL` to it.

## Important boundary

Expert My Rules can automate research, implementation, criticism and verification workflows, but high-stakes claims remain subject to the strength of the configured validators. In mathematical research, several agreeing LLMs are not a formal proof checker. The system is designed to make unsupported completion difficult and to preserve the complete evidence/objection trail, not to replace external mathematical or scientific validation.

See `ARCHITECTURE.md` and `REMOTE_ACCESS.md` for architecture and private-network deployment guidance.
