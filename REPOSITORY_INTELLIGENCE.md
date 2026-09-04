# Repository intelligence for small local coding models

v0.6 adds a compact, deterministic repository-inspection layer designed for weak/CPU-bound local models.

The objective is not to give the model an unrestricted terminal. It is to make the normal coding loop efficient:

```text
repo map → targeted search → line-range read → surgical edit → diff → lint/tests → review
```

This reduces context volume and avoids rewriting large files merely to change a few lines.

## Tools

New software workspaces grant the Worker these repository tools in addition to the existing Git/test capabilities.

### `repo_map`

Returns a bounded list of repository files. Python files additionally expose top-level class/function names and line numbers when they can be parsed cheaply.

Ignored runtime/vendor directories include `.git`, `.awb`, `artifacts`, `logs`, virtual environments, caches, `node_modules`, `dist` and `build`.

### `search`

Performs a literal, portable text search without shell execution. Results contain workspace-relative path, line number and a bounded line snippet.

Search is intentionally literal rather than arbitrary regular-expression execution: predictable bounded work is preferable for an H24 autonomous runtime.

Binary files, very large files and ignored directories are skipped.

### `read_range`

Reads only a requested line range and adds line numbers. A single call is capped at 500 lines. The existing whole-file reader remains available when it is genuinely necessary.

### `replace`

Performs an exact text replacement only when the number of matches equals `expected_count` (default `1`). If the precondition is not satisfied, **no write occurs**.

This gives the Worker a deterministic compare-and-replace primitive: an edit cannot silently hit the wrong occurrence after the repository changed.

Protected control paths (`project.yaml`, ledger, artifacts, logs, `.awb`, `.git`) remain non-writable, and all paths are confined to the task execution root/worktree.

## Why this matters on the Acer

A 3–4B model has much less useful context/reasoning capacity than a frontier coding model. Feeding it an entire repository makes that limitation worse.

The intended strategy is therefore:

1. map structure cheaply;
2. search for the relevant symbol/text;
3. inspect only nearby lines;
4. modify a uniquely identified region;
5. inspect the actual Git patch;
6. let deterministic validators and independent reviewers challenge the result.

The same primitives remain useful when the Worker later moves to a 30B/70B GPU model; no project/workflow migration is required.

## Security boundary

Repository-intelligence tools are implemented with Python filesystem operations rather than model-generated shell commands. They do not broaden the shell capability model.

They enforce the same execution-root confinement used by the existing file tools, avoid following directory symlinks while scanning, reject file targets that resolve outside the worktree, cap scanned files/results/file sizes and preserve protected-path write restrictions.
