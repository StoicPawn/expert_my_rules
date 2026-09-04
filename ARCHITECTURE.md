# Expert My Rules — Architecture

## Product invariant

The user should primarily define **what done looks like**, not how to orchestrate agents.

A project consists of:

1. **North Star** — the stable final objective.
2. **Proposed Definition of Done** — explicit completion gates generated locally and editable by the user.
3. **Dynamic expert workflow** — Director, Worker, independent Reviewer and Verifier roles; domain templates may specialize them.
4. **Persistent state** — tasks, attempts, objections, gate decisions, artifacts, events and job state in SQLite.
5. **Tool capabilities** — explicitly granted, deny-by-default actions.
6. **Model routing policy** — logical roles are separated from physical compute, with local-first inference and optional budget-gated cloud escalation.

## Goal-first planning

The default UI asks for a final goal. A local Ollama planner proposes:

- research/software/custom project type;
- expert roles and instructions;
- completion conditions.

If Ollama is unavailable during project creation, a deterministic domain template is used. Planning never falls back to a paid cloud provider.

## Autonomous loop

`Goal → Director → Worker → Adversarial Reviewer → External/semantic Verifier → Ledger → next task`

The Director is explicitly forbidden from weakening the North Star or moving completion criteria to manufacture success.

After each accepted/rejected task:

- external validators run where configured;
- the independent verifier evaluates non-manual semantic completion gates conservatively;
- the project stops only when every required gate is PASS.

Manual gates remain available for goals that genuinely require a human decision, but are not the default.

## Compute plane and model routing

The orchestration layer does not treat the Acer, a GPU workstation or a remote server as part of the project definition. They are **compute nodes**.

A runtime may define:

```yaml
runtime:
  compute_nodes:
    - id: local-ollama
      kind: ollama
      base_url_env: OLLAMA_BASE_URL
      max_concurrency: 1
    - id: gpu-box
      kind: ollama
      base_url: http://10.0.0.50:11434
      max_concurrency: 2

  role_routes:
    worker:
      - node: gpu-box
        model: qwen3-coder:30b
        priority: 10
      - node: local-ollama
        model: qwen3:4b
        priority: 100
```

The first enabled route wins. If a routed node fails and another route exists for the same role, the orchestrator fails over to the next one and records the event.

This means the same workspace can start on a small CPU machine and later move Worker/Reviewer/Verifier inference to stronger GPU hardware **without changing the North Star, agents, gates, artifacts or ledger format**.

`max_concurrency` is enforced per compute node inside the running process. The default Acer node is deliberately set to `1`, matching the small-device strategy of keeping inference sequential and allowing Ollama to keep only one model resident at a time.

Existing v0.2 workspaces that only contain `AgentSpec.provider` / `runtime.default_provider` remain valid. When `compute_nodes` and `role_routes` are absent, routing transparently falls back to the legacy provider path.

## Attempt genealogy

A task is no longer represented only by its latest state. Every execution attempt has a persistent ledger record containing:

- task id and attempt number;
- final status;
- model/compute routes actually used;
- adversarial review result;
- verification result;
- produced artifact;
- runtime error when an attempt fails.

This preserves the correction history needed for long-running autonomous work and makes future policies such as regression analysis, route-quality scoring and model-specific benchmarking possible without changing the task model.

## Continuous projects

A project-level run is not one infinite LLM context. It is a sequence of bounded checkpoint sessions. The outer continuous job remains ACTIVE until:

- all required gates pass;
- the user pauses it;
- the user cancels it;
- a fatal runtime/provider error occurs.

Continuous job state is persisted. On service startup, RUNNING continuous jobs are recovered and resumed.

## Model routing and cloud escalation

Default provider: local Ollama.

Cloud escalation is structurally present but disabled by default. It requires all of:

- `escalation.enabled = true`;
- positive configured monetary budget;
- positive cloud-call cap;
- an `OPENAI_API_KEY` environment variable;
- a role/task satisfying escalation policy (e.g. repeated local failure or high priority).

Every escalation is logged. With the default configuration, cloud escalation is impossible.

## Tool layer

Tools are capability-based and deny-by-default. Current primitives include sandboxed workspace file listing/read/write and fixed configured shell commands. A model cannot invent arbitrary shell commands from a fixed shell tool.

## Persistence

Each workspace owns:

- `project.yaml` — portable goal/team/gates/tools/runtime policy;
- `ledger.sqlite3` — tasks, attempts, events, gate state and continuous jobs;
- `artifacts/` — candidate outputs + reviews + verification evidence;
- `logs/` — runtime/service logs.

## Deployment

Recommended H24 deployment is Docker Compose:

- Expert My Rules service;
- local Ollama service;
- persistent workspace volume;
- persistent Ollama model volume;
- `restart: unless-stopped`.

The same application can run natively on Python 3.11+.

The intended scaling path is therefore:

```text
small Acer / CPU node
        ↓ same project format
local GPU workstation
        ↓ same project format
multiple local/private inference nodes
        ↓ optional
budgeted cloud escalation
```

## Correctness boundary

Independent LLM review is not equivalent to formal proof or external scientific validation. The architecture therefore treats completion as a configurable evidence problem and supports external validators. For serious mathematical research the natural next adapters are literature retrieval, Lean, SymPy and numerical counterexample search. For software projects the next layer is richer Git/worktree isolation, patch-aware review, static analysis, browser tests and release validators.
