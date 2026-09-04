# Stack-aware software validation

v0.7 replaces the previous Python-only default validators with a deterministic dispatcher.

The problem with a fixed command such as `python -m unittest discover -s tests -v` is that it can return success with **zero tests** for a JavaScript, Go, Rust or even an untested Python project. An autonomous agent could therefore receive false evidence that its change is valid.

New software workspaces instead use:

```bash
python -m awb.core.validation lint
python -m awb.core.validation tests
```

The dispatcher inspects the task worktree without consulting an LLM and currently recognizes:

- Python;
- Node/npm;
- Go;
- Rust/Cargo.

For mixed repositories every detected stack must satisfy its applicable checks.

## Test policy

Test validation is deliberately fail-closed.

- Python: real `test_*.py` / `*_test.py` files must exist. Pytest is used when repository configuration indicates pytest; otherwise unittest discovery is used.
- Node: `package.json` must contain a real `scripts.test`; the common `Error: no test specified` placeholder is rejected.
- Go: `go test ./...`.
- Rust: `cargo test --quiet`.
- No supported stack or no real test suite: validation fails rather than reporting a misleading green result.

A required runtime executable/framework that is missing also produces a failure with explicit evidence.

## Static checks

The lint/static phase uses the strongest deterministic check readily available:

- Python: Ruff;
- Node: `npm run lint` when the repository defines it;
- Go: `go vet ./...`;
- Rust: `cargo check --quiet`.

A stack with no configured optional linter may record a skipped static check, but that never substitutes for the required test gate.

## Security and reproducibility

The validator chooses commands from framework-owned rules; the model does not generate them. Commands run with `shell=False` inside the transactional task worktree and their output is bounded before it is returned as evidence.

This keeps project validation extensible while avoiding an unrestricted model-controlled terminal. New ecosystems can be added as deterministic adapters without changing the agent workflow or compute architecture.
