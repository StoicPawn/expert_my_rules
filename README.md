# Autonomous Workbench (AWB)

Local-first framework for iterative autonomous work on research, software, data-analysis and custom projects.

The core loop is deliberately domain-agnostic:

`Goal -> Director -> Worker -> Reviewer -> Verifier -> State update -> next task -> completion gates`

The framework does **not** trust an LLM saying that a project is finished. Completion is determined by explicit workspace gates and external validators where available.

## Current MVP

- Git-friendly project workspaces with YAML manifests.
- Persistent SQLite task/decision ledger.
- Reusable agent roles: director, worker, reviewer, verifier.
- Model gateway with `mock`, local `ollama`, and optional `openai` providers.
- Research and software workspace templates.
- One-step and bounded-loop execution.
- Minimal web dashboard usable from another device on the LAN, including an iPad.
- Every iteration persists prompts, outputs, objections and task status.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

awb init research my-theory --goal "Prove or refute the central conjecture"
awb run workspaces/my-theory --provider mock --max-steps 3
awb serve --host 0.0.0.0 --port 8000
```

Open `http://<PC-IP>:8000` from another device on the same network.

### Ollama

Install and run Ollama separately, then:

```bash
awb run workspaces/my-theory --provider ollama --model qwen3:8b --max-steps 10
```

### OpenAI API

API use is optional and separate from the local-first workflow. Set:

```bash
export OPENAI_API_KEY="..."
awb run workspaces/my-theory --provider openai --model gpt-5 --max-steps 5
```

No API key is stored in project files.

## Workspace manifest

Each workspace owns a `project.yaml` file describing:

- north-star goal;
- agent roles;
- completion gates;
- validator commands;
- iteration policy.

The same runtime can therefore execute a mathematical research project or a software project without hard-coding either domain into the orchestrator.

## Safety / correctness principle

`PAPER_READY`, `RELEASE_READY`, or similar terminal states are candidate states only. For high-stakes claims (e.g. new mathematics), final human/domain-expert review remains mandatory.

## Roadmap

1. Structured task DAG and dependency graph.
2. Git branch-per-task execution for software projects.
3. Browser/literature and GitHub tools.
4. Formal-verification adapters (Lean) and symbolic/numerical validators.
5. Provider escalation policies: cheap local model -> stronger local -> cloud API.
6. Multi-model independent referee panels.
7. Rich iPad-first UI and artifact diff/review.
