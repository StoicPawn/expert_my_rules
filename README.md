# Autonomous Workbench

A configurable multi-agent framework where experts work, review, challenge, and iterate — by your rules.

Autonomous Workbench is a local-first project runtime for iterative work across domains such as mathematical research and software development. It separates execution, criticism, verification, orchestration, persistent state, and completion gates so a project does not become "done" merely because one model says so.

## MVP

- Director → Worker → Reviewer → Verifier loop
- Persistent SQLite ledger
- Configurable research and software workspaces
- Semantic completion gates
- Swappable model providers: mock, Ollama, OpenAI API
- CLI and minimal web UI
- Git-friendly project state

See `ARCHITECTURE.md` for the design and roadmap.
