# Expert My Rules — Architecture

## Product invariant

The user should primarily define **what done looks like**, not how to orchestrate agents.

A project consists of:

1. **North Star** — the stable final objective.
2. **Proposed Definition of Done** — explicit completion gates generated locally and editable by the user.
3. **Configurable expert workflow graph** — task selection, one or more execution agents, one or more adversarial reviewers and deterministic validators.
4. **Persistent state** — tasks, attempts, objections, gate decisions, artifacts, events and job state in SQLite.
5. **Tool capabilities** — explicitly granted, deny-by-default actions.
6. **Model routing policy** — logical roles are separated from physical compute, with local-first inference and optional budget-gated cloud escalation.
7. **Transactional software execution** — candidate code changes live in isolated Git worktrees until accepted.

## Goal-first planning

The default UI asks for a final goal. A local Ollama planner proposes:

- research/software/custom project type;
- expert roles and instructions;
- completion conditions.

The deterministic project template owns security-sensitive capabilities, workflow defaults, validators and Git policy. A planner may change role identities/instructions but cannot silently grant itself tools.

If Ollama is unavailable during project creation, the deterministic domain template is used. Planning never falls back to a paid cloud provider.

## Configurable workflow graph

The original fixed loop remains a backwards-compatible fallback, but new workspaces store an explicit DAG in `project.yaml`.

Default flow:

`select_task → execute → challenge → validate`

Example with two independent challengers:

```yaml
workflow:
  review_policy: all
  stages:
    - id: select
      kind: select_task
      role: director
    - id: build
      kind: execute
      role: worker
      depends_on: [select]
    - id: adversarial
      kind: review
      role: reviewer
      depends_on: [build]
    - id: second_opinion
      kind: review
      role: audit
      depends_on: [build]
    - id: checks
      kind: validate
      validators: [lint, tests]
      depends_on: [adversarial, second_opinion]
```

Stage ids must be unique, dependencies must exist and cycles are rejected before execution. At least one `execute` stage is required. Review policy may require all required reviewers to approve or allow an explicit `any` policy. Multiple execution stages are also supported, so a project can add specialist implementation/documentation agents without changing the orchestrator code.

The Director is explicitly forbidden from weakening the North Star or moving completion criteria to manufacture success. Retryable rejected tasks are reopened with their attempt history intact rather than silently abandoned.

## Transactional Git software workspaces

New software workspaces enable Git isolation by default. The canonical workspace is treated as the accepted state. For each task the runtime creates or reuses a task-specific Git branch and linked worktree under `.awb/worktrees/`.

```text
canonical accepted workspace
          │
          ├── awb/task-TASK-123 worktree  ← Worker edits here
          │          │
          │          ├─ git status / diff
          │          ├─ Ruff lint
          │          └─ automated tests
          │
          ├── Reviewer sees the real patch
          │
          ├── rejected but retryable → preserve candidate worktree
          ├── final rejection       → discard branch/worktree (rollback)
          └── approved + checks     → commit + fast-forward merge
```

This gives four properties that the previous direct-file editing path did not provide:

- **isolation:** the accepted workspace is not modified while an agent experiments;
- **patch-aware review:** challengers receive the actual Git patch, not only the Worker's prose summary;
- **deterministic rollback:** an exhausted/rejected candidate can be deleted without undo heuristics;
- **iterative correction:** retryable rejected candidates remain available for the next attempt.

Full candidate patches are persisted as artifacts. The Worker can inspect `git_status` and `git_diff`; software templates also expose fixed `lint` and `tests` commands. Control paths such as `project.yaml`, `ledger.sqlite3`, `artifacts/`, `logs/`, `.awb/` and `.git` are protected from agent writes.

If a generated software workspace is not already a Git repository, the runtime can initialize it and create a baseline checkpoint. Existing v0.3/v0.2 manifests remain valid because Git isolation defaults to disabled unless the manifest opts in.

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

This means the same workspace can start on a small CPU machine and later move Worker/Reviewer/Verifier inference to stronger GPU hardware **without changing the North Star, workflow, gates, artifacts or ledger format**.

`max_concurrency` is enforced per compute node inside the running process. The default Acer node is deliberately set to `1`, matching the small-device strategy of keeping inference sequential and allowing Ollama to keep only one model resident at a time.

Existing v0.2 workspaces that only contain `AgentSpec.provider` / `runtime.default_provider` remain valid. When `compute_nodes` and `role_routes` are absent, routing transparently falls back to the legacy provider path.

## Attempt genealogy

A task is not represented only by its latest state. Every execution attempt has a persistent ledger record containing:

- task id and attempt number;
- final status;
- model/compute routes actually used;
- aggregate and per-stage adversarial review results;
- verification result;
- produced artifact and optional patch artifact;
- runtime error when an attempt fails.

This preserves the correction history needed for long-running autonomous work and makes route-quality scoring, regression analysis and model-specific benchmarking possible without changing the task model.

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

Tools are capability-based and deny-by-default. Current primitives include sandboxed file listing/read/write, Git status/diff and fixed configured shell commands. A model cannot invent arbitrary shell commands from a fixed shell tool.

For software work, tools execute inside the task worktree rather than the accepted workspace. The planner does not control tool grants; template capabilities are reattached after AI planning.

## Persistence

Each workspace owns:

- `project.yaml` — portable goal/team/workflow/gates/tools/runtime policy;
- `ledger.sqlite3` — tasks, attempts, events, gate state and continuous jobs;
- `artifacts/` — candidate outputs, reviews, verification evidence and patches;
- `logs/` — runtime/service logs;
- `.awb/worktrees/` — temporary/retryable software candidate worktrees.

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
        ↓ same project/workflow format
local GPU workstation
        ↓ same project/workflow format
multiple local/private inference nodes
        ↓ optional
budgeted cloud escalation
```

## Correctness boundary

Independent LLM review is not equivalent to formal proof or external scientific validation. The architecture therefore treats completion as a configurable evidence problem and supports external validators. For serious mathematical research the natural next adapters remain literature retrieval, Lean, SymPy and numerical counterexample search. For software, Git isolation, real patches, lint and executable tests now provide stronger evidence, while browser/E2E tests, type checking, security scanners, language-specific build systems and release validators remain extensible next layers.
