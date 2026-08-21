# Expert My Rules

**Multi-agent experts that keep working until your rules say they’re done.**

Expert My Rules is a local-first autonomous project workbench. A workspace declares a stable final objective, expert roles, model providers, validators and completion gates. The runtime then repeats:

`Director → Worker → Adversarial Reviewer → External Verifier → Ledger → next task`

The same engine supports research, software, data work or custom workflows. It does not trust an LLM claiming that a project is finished: required gates determine completion.

## What the MVP can do now

- Create `research`, `software` or fully `custom` workspaces.
- Define a North Star objective, agents, instructions, gates and validators per workspace.
- Use one default model or assign providers/models to individual agents.
- Run completely locally with Ollama, use a deterministic mock for testing, or optionally call the OpenAI API.
- Persist tasks, attempts, events and gate state in SQLite.
- Save every candidate result + adversarial review + verification report as a Markdown artifact.
- Inject a human task at any moment; user tasks receive high priority.
- Stop repeated failures after a configurable attempt budget and ask the Director for a remediation task instead of looping forever.
- Launch a bounded end-of-day session from the CLI **or from the iPad-friendly dashboard**, then close the page while the server continues running.
- Package the service with Docker Compose, or install it as a normal Python CLI on Windows/macOS/Linux.

## Fastest cross-platform setup: Docker

Requirements: Docker Desktop (Windows/macOS) or Docker Engine + Compose (Linux).

```bash
git clone https://github.com/StoicPawn/expert_my_rules.git
cd expert_my_rules
docker compose up -d --build
```

Open `http://localhost:8000`. From an iPad on the same LAN, open `http://<PC-IP>:8000`.

Workspaces are persisted in `./workspaces`, outside the container. `restart: unless-stopped` makes the service available again after PC/service restarts.

For local Ollama, install Ollama on the host PC and set the workspace provider to `ollama`. Docker is preconfigured to reach the host at `host.docker.internal:11434`.

## Native installation

Requires Python 3.11+.

### Windows

```powershell
./install.ps1
.\.venv\Scripts\awb --help
```

### macOS / Linux

```bash
./install.sh
. .venv/bin/activate
awb --help
```

## The end-of-day workflow

Create a project once:

```bash
awb init research observable-complexity \
  --goal "Prove or refute the Observable Dynamic Complexity duality" \
  --provider ollama --model qwen3:8b
```

At the end of the day, inject what you want the experts to attack first:

```bash
awb task-add workspaces/observable-complexity \
  "Attack the converse direction" \
  --description "Try first to construct a counterexample; if none survives, isolate sufficient assumptions."
```

Then launch:

```bash
awb overnight workspaces/observable-complexity --hours 8
```

It stops when either all required gates pass, eight hours expire, or the step safety budget is reached. Every iteration remains inspectable the next morning.

You can do the same from the dashboard: open the workspace, add the task, choose the hours and press **Launch in background**. Closing Safari does not stop the server-side run.

## Providers

### Local / free: Ollama

```bash
awb provider workspaces/observable-complexity ollama --model qwen3:8b
```

Ollama itself must be installed and the chosen model downloaded once.

### OpenAI API: optional

```bash
export OPENAI_API_KEY="..."
awb provider workspaces/observable-complexity openai --model gpt-5
```

The API is optional and is billed separately from ChatGPT subscriptions. Keys are read from environment variables and are never stored in `project.yaml`.

## Workspace = rules

`project.yaml` is the portable declaration of a project. It contains:

```yaml
name: my_project
type: custom
goal: A stable, testable final objective

agents:
  - id: director
    role: director
    instructions: Choose the highest-information next task.
  - id: expert
    role: worker
    instructions: Produce evidence, not just plausible prose.
    provider:
      kind: ollama
      model: qwen3:8b
  - id: critic
    role: reviewer
    instructions: Try to falsify the candidate result.

gates:
  - id: goal_verified
    description: Final objective independently verified.
    required: true
    manual: true

validators: {}

runtime:
  default_provider:
    kind: ollama
    model: qwen3:8b
  max_steps_per_run: 25
  max_minutes_per_run: 60
  max_task_attempts: 3
  pause_seconds: 0
```

A software workspace can replace manual claims with real validators such as tests, linters or compilers. A research workspace can later add literature retrieval, Lean, symbolic checks and reproducibility tests.

## Useful commands

```bash
awb status <workspace>
awb run <workspace> --max-steps 3
awb overnight <workspace> --hours 8
awb task-add <workspace> "Task title"
awb gate <workspace> goal_verified pass --detail "Human review completed"
awb provider <workspace> ollama --model qwen3:8b
awb serve --host 0.0.0.0 --port 8000
```

## Important boundary

For mathematical novelty, scientific claims, safety-critical software, or other high-stakes outputs, `COMPLETE` means **the configured gates passed**. It is not a substitute for human/domain-expert sign-off. The purpose of the framework is to make the evidence, objections and verification trail much stronger and much harder to hand-wave.

See `ARCHITECTURE.md` for the design and next milestones.
