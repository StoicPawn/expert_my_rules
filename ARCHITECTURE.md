# Architecture

## Design invariant

The runtime is domain-agnostic. A project is defined by five things:

1. **Goal** — stable north star.
2. **State** — persistent ledger of tasks, objections, evidence and gates.
3. **Agents** — interchangeable roles operating on state.
4. **Tools / validators** — external sources of truth.
5. **Gates** — explicit completion predicates.

## Runtime loop

1. Director selects one high-information task.
2. Worker executes it.
3. Adversarial reviewer tries to reject it.
4. External validators run.
5. Ledger records evidence and objections.
6. Gates are reevaluated.
7. Repeat until all required gates pass or the caller stops the bounded run.

The orchestrator itself never accepts a free-form `DONE` assertion from an agent as proof of completion.

## Provider boundary

`ModelProvider.generate(system, user)` is the only model-facing API in the MVP.
Providers currently implemented:

- Mock (offline deterministic testing)
- Ollama (local)
- OpenAI Responses API (optional)

Future routing can implement per-role models and escalation policies without changing the project model.

## Persistence

Each workspace contains:

- `project.yaml` — declarative project configuration
- `ledger.sqlite3` — task/event/gate state
- `artifacts/` — generated project outputs
- `logs/` — runtime logs

Git versions source, manifests and artifacts selected for commit; transient DB/log files are ignored by default.
