# Expert My Rules — Repeatable Benchmarks

The framework should be able to improve its compute without changing its architecture. That only matters if model/hardware changes can be measured on the **same tasks**.

v0.5 therefore includes an offline coding benchmark suite. It creates isolated software workspaces, gives the agent fixed acceptance tests, lets the normal Worker → Reviewer → validator workflow operate through transactional Git worktrees, and records both correctness and runtime telemetry.

## Run the standard local ensemble

From the repository/virtual environment:

```bash
awb benchmark
```

By default this uses the normal role routes from the generated benchmark workspaces:

- Worker: configured Qwen model;
- Reviewer: configured Llama model;
- normal compute-node scheduler and failover policy.

The built-in `awb_builtin_coding_v1` suite currently contains:

1. a small arithmetic regression repair;
2. a specification-driven key/value parser implementation;
3. a multi-file order-total repair with validation and rounding behavior.

All cases use executable acceptance tests. Semantic LLM completion gates are intentionally removed from benchmark scoring so the score measures delivered behavior rather than verifier optimism.

## Choose an output directory

```bash
awb benchmark --output benchmarks/acer-baseline --max-attempts 3
```

The result is written to:

```text
benchmarks/acer-baseline/
├── benchmark.json
├── repair_addition/
├── implement_pairs_parser/
└── multi_file_order_total/
```

Every case directory is a normal Expert My Rules software workspace, including its ledger, attempt genealogy, model-call telemetry, artifacts and Git history.

## Benchmark a single model/provider override

For a controlled comparison that deliberately uses one model for every role:

```bash
awb benchmark --output benchmarks/qwen-test --provider ollama --model qwen3:4b
```

Do not pass `--provider` if the objective is to test the full heterogeneous multi-agent configuration.

## Compare machines later

Run the exact same suite on the Acer and on a future GPU workstation, then compare the saved reports:

```bash
awb benchmark-compare \
  benchmarks/acer-baseline/benchmark.json \
  benchmarks/gpu-workstation/benchmark.json
```

The comparison includes:

- passed cases / total cases;
- objective score;
- total elapsed time;
- number of model calls;
- machine/CPU metadata.

Each individual case also stores grouped model telemetry such as route, role, success rate, average call time and approximate output characters/second. This makes it possible to distinguish **better reasoning** from merely **faster inference**.

## Runtime telemetry outside benchmarks

Normal project runs also persist model-call telemetry in `ledger.sqlite3`. Inspect it with:

```bash
awb status path/to/workspace
```

The status output includes configured scheduler policy, current in-process compute-node state and historical per-role/model statistics.

## Interpretation

A benchmark score is not a universal measure of coding intelligence. The built-in suite is deliberately small, deterministic and cheap enough to run on weak local hardware. Its purpose is regression detection and apples-to-apples comparison across model, routing and hardware changes.

Future benchmark versions should be added under new suite identifiers rather than silently changing `awb_builtin_coding_v1`, so old Acer results remain comparable with future workstation results.
