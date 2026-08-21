# Expert My Rules — Architecture

## Product invariant

The user should primarily define **what done looks like**, not how to orchestrate agents.

A project consists of:

1. **North Star** — the stable final objective.
2. **Proposed Definition of Done** — explicit completion gates generated locally and editable by the user.
3. **Dynamic expert workflow** — Director, Worker, independent Reviewer and Verifier roles; domain templates may specialize them.
4. **Persistent state** — tasks, attempts, objections, gate decisions, artifacts, events and job state in SQLite.
5. **Tool capabilities** — explicitly granted, deny-by-default actions.
6. **Model routing policy** — local-first inference with optional budget-gated cloud escalation.

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

## Continuous projects

A project-level run is not one infinite LLM context. It is a sequence of bounded checkpoint sessions. The outer continuous job remains ACTIVE until:

- all required gates pass;
- the user pauses it;
- the user cancels it;
- a fatal runtime/provider error occurs.

Continuous job state is persisted. On service startup, RUNNING continuous jobs are recovered and resumed.

## Model routing

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
- `ledger.sqlite3` — tasks, events, gate state and continuous jobs;
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

## Correctness boundary

Independent LLM review is not equivalent to formal proof or external scientific validation. The architecture therefore treats completion as a configurable evidence problem and supports external validators. For serious mathematical research the natural next adapters are literature retrieval, Lean, SymPy and numerical counterexample search.
