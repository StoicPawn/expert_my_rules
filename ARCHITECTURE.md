# Expert My Rules — Architecture

## Product invariant

The runtime is domain-agnostic. Every workspace is defined by:

1. **North Star** — a stable final objective.
2. **Agents** — roles, instructions and optional per-role model providers.
3. **State** — persistent tasks, attempts, objections, evidence and events.
4. **Tools** — explicitly granted actions the Worker may call while solving a task.
5. **Validators** — executable sources of truth outside the language model.
6. **Gates** — explicit predicates controlling completion.
7. **Runtime policy** — budgets for steps, time, tool calls and retries.

## Autonomous loop

1. The Director selects one high-information task.
2. The Worker executes it and may enter a bounded tool-call loop. Only tools granted to that Worker are callable; file tools are workspace-sandboxed and shell tools use fixed configured commands.
3. The Reviewer tries to reject it.
4. External validators run.
5. The result and review are written to an immutable-ish artifact file and SQLite event ledger.
6. A successful task closes; a failed task becomes blocked/rejected after its retry budget.
7. Validator-backed gates are recomputed; manual semantic gates remain open until explicit approval.
8. Repeat until required gates pass or the run budget expires.

The orchestrator never accepts a free-form `DONE` statement as proof of completion.

## Model gateway

Current providers:

- `mock` — deterministic development/tests.
- `ollama` — local-first, zero API cost after hardware/electricity.
- `openai` — optional cloud escalation via Responses API.

A workspace defines a default provider. Any agent may override it, allowing configurations such as local Worker + cloud Referee + local Director.

## Persistence

Each workspace owns:

- `project.yaml` — portable rules/configuration.
- `ledger.sqlite3` — task/event/gate/job runtime state.
- `artifacts/` — candidate outputs and review evidence.
- `logs/` — reserved for service/runtime logs.

This separation makes projects portable while keeping high-volume runtime state out of Git if desired.

## Availability

Two supported deployment paths:

- Native Python 3.11+ installation.
- Docker Compose, persisting `./workspaces` as a host volume.

The FastAPI dashboard is responsive and can be used from iPad on the same LAN. A background executor allows bounded long runs to continue after the browser is closed. Job state is persisted and exposes pause/resume/cancel controls between agent iterations. The MVP assumes a trusted local network; public Internet exposure must add authentication/TLS before use.

## Safety budgets

Autonomy is always bounded by configured time, step and retry budgets. An `overnight` run may stop because:

- all required gates passed;
- the time budget expired;
- the step budget expired;
- a provider/tool failed.

This makes long unattended runs useful without allowing an accidental unbounded API/cost loop.

## Implemented Tool Layer

The Worker uses a small JSON tool protocol. The runtime executes the action, records arguments/results in the event ledger, returns the result to the model and allows another bounded tool call. Current built-in tool types:

- `list_files`
- `read_file`
- `write_file` (requires `writable: true`)
- `shell` with a fixed manifest command

This is intentionally capability-based rather than giving the model unrestricted shell access.

## Next milestones

1. Reconcile/recover jobs automatically after a host/service restart.
2. Dashboard editor for validators and deletion/reordering of agents/gates/tools.
3. Structured task DAG/dependencies instead of a flat priority queue.
4. Git worktree/branch-per-task execution for software projects.
5. External tool adapters: GitHub and browser/literature search.
6. Formal and scientific validators: Lean, SymPy, numerical counterexample search, reproducibility harnesses.
7. Independent multi-model referee panels and explicit consensus policies.
8. Cost/token budgets and escalation policy: cheap local → stronger local → cloud.
9. Authentication plus secure remote access beyond the LAN.
