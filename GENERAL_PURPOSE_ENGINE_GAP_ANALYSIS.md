# Expert My Rules — General-purpose deep engine gap analysis

## North-star for the engine

Expert My Rules is not a Kellerer solver and not a fixed `task -> model -> answer` pipeline. Kellerer is only a demanding benchmark.

The engine target is a **general-purpose, local-first system for complex work on weak hardware**. It optimizes for eventual quality, evidence and recoverability rather than latency. On an ACEPC it may take hours, days or months, but useful verified state must monotonically accumulate and an interruption must not force an intellectual restart.

Core cycle:

`classify -> define success -> decompose -> execute a small step -> verify -> critique -> repair/re-plan -> persist -> repeat`

Every step must be small enough to inspect, have an explicit verification contract before execution, and be reversible or supersedable without destroying accepted state.

## Gap analysis

| Target property | Current engine | Gap | Required change |
| --- | --- | --- | --- |
| Initial meta-orchestrator | Goal-first planner proposes type, roles and project gates | Planner configures a workspace but does not persist a problem profile, risk model, tool needs or decomposition policy used by every later decision | Add a persistent meta-plan/problem profile produced locally and deterministically fall back when the model output is invalid |
| Dynamic graph of micro-tasks | Tasks exist and recovery can create children; workflow stages are a DAG | Task dependencies are mostly metadata conventions and task selection is priority-first | Make dependencies and verification contracts first-class task state and select only graph-ready work |
| Verification defined before work | Project gates and workflow validators exist | Individual tasks can be created without saying in advance what evidence would close them | Require a task verification contract before the Worker runs; deterministic validators execute before epistemic acceptance |
| Reasoning vs execution separation | ToolRunner, Git worktrees, validators, Research Lab, symbolic/numeric tools already exist | The model can still spend tokens on work that software could do, because planning does not explicitly bind tasks to deterministic evidence | Meta-plan and task contract declare tools/validators/evidence; Worker prompt must prefer deterministic execution and cannot claim tool results |
| Persistent external memory | SQLite ledger, attempts, events, artifacts, checkpoints and Git state exist | Planning still relies heavily on a rolling prompt snapshot rather than a compact durable memory of accepted facts, failed strategies and unresolved objections | Add durable project memory records and retrieve only relevant records for a micro-task |
| Logical roles, one physical model | Director/Worker/Reviewer/Verifier roles and compute routing exist | Older defaults swapped several local models, causing RAM churn on ACEPC | Use one resident local model by default; role identity is prompt/state, not simultaneous model residency |
| Explicit failure learning | Technical vs scientific failures, recovery history and objections already exist | No canonical strategy identity prevents semantically identical retries; repeated failure can still resemble a blind loop | Persist strategy fingerprints and rejected strategies; force re-planning when a strategy repeats without new evidence |
| Reviewer barrier | Focused recovery loop keeps reviewer-blocked work active | This behavior is layered on the old orchestrator rather than represented by the task graph itself | Move the invariant into graph readiness: a task cannot become DONE until its verification contract, Reviewer and required validators pass |
| Reversible work | Git worktrees for software, artifacts for research | Non-software steps are persisted but do not share a uniform evidence/result object | Persist every micro-task outcome as evidence + review + verification + strategy lineage; never overwrite accepted evidence |
| Long-runtime endurance | Checkpoints, interrupted stream capture, unlimited local queue/retry work are being added in PR #37 | Legacy timeout and saturation assumptions still exist in docs/tests and some paths | Normalize ACEPC policy to unbounded queue/time, retry recoverable failures, checkpoint at stable boundaries |
| Live observability | Dashboard has role/event/resource telemetry | Need direct visibility into output/token/tool/code activity and current graph phase | Deep-live dashboard polls the stream journal and task graph at 1s cadence; no hidden chain-of-thought is exposed |
| General-purpose tools | Sandboxed file/repo tools, Research Lab, symbolic math, counterexample search, literature and source ingestion exist | Tool availability is template-oriented rather than selected from a general problem profile | Treat tools as capabilities in a registry; meta-orchestrator recommends allowed tools but cannot grant privileges itself |
| Domain neutrality | Templates support research/software/custom | Some recovery prompts and tests are still research-flavoured | Core engine vocabulary becomes evidence/task/verification/strategy; domain templates supply capabilities and validators only |

## Refactor order

1. **Make task state explicit.** Introduce first-class dependencies, verification contracts, strategy lineage and graph-ready selection while preserving backward compatibility with existing ledgers.
2. **Add the meta-orchestrator.** On first run, classify the problem, define success evidence, identify deterministic tools and risks, and persist the problem profile outside the model context.
3. **Externalize working memory.** Store accepted facts/evidence, unresolved objections and failed strategies in SQLite; retrieve a compact relevant slice for each micro-task.
4. **Make the deep loop authoritative.** A micro-task follows PLAN/EXECUTE/VERIFY/CRITIQUE/REWORK. Reviewer rejection or failed verification returns to the same focus chain or an explicit prerequisite. Unrelated work cannot jump the queue.
5. **Prefer software verification.** Execute tests, parsers, code, numerical checks, symbolic engines, database queries and other deterministic capabilities whenever applicable before asking an LLM to judge them.
6. **Harden failure semantics.** Resource contention is waiting, not failure. Recoverable technical errors retry with bounded backoff. Repeated strategies are detected and must change before another scientific attempt.
7. **Checkpoint and resume.** Persist inputs, public partial output, tool results, strategy/evidence state and task graph at stable boundaries and on explicit stop. Resume from this state rather than recreating a generic reassessment task.
8. **Expose the state machine.** Dashboard shows current micro-task, dependencies, verification contract, logical role, model/token/chunk progress, visible output, tool/code activity, Reviewer objections and checkpoint state.
9. **Benchmark across domains.** Kellerer becomes one benchmark beside software repair, document synthesis, data analysis and other complex tasks. Performance metric is eventual verified completion/correctness, not tokens/sec.

## Non-goals

- Do not create many simultaneous agents on the ACEPC.
- Do not assume a larger context window is memory.
- Do not treat Reviewer agreement alone as formal proof when deterministic/formal verification is available.
- Do not retry the same failed strategy because a timeout or attempt counter fired.
- Do not specialize core scheduling, prompts or state transitions to Kellerer.
- Do not optimize for response speed at the expense of recoverability or evidence.

## Acceptance invariants for production

A production build is acceptable only if all of the following hold:

1. Paid API calls are impossible unless the user explicitly enables FORCE mode.
2. One local model can sequentially play all logical roles.
3. A saturated local model waits instead of causing project failure.
4. Every newly planned micro-task has an explicit verification contract before execution.
5. Dependencies are persisted and task selection respects them.
6. A task cannot close while required deterministic checks fail or the Reviewer rejects it.
7. A blocked task stays the active focus until repaired, decomposed into prerequisites, explicitly reframed or evidence-rejected.
8. Failed strategy identity/history survives restart and is supplied to re-planning.
9. Checkpoint/stop preserves durable project state plus visible in-flight generation/tool evidence sufficient for useful resume.
10. Dashboard reports the actual current role/task/phase, public stream progress and tool activity without exposing hidden chain-of-thought.
11. Core engine tests are domain-neutral; Kellerer-specific content is never required for scheduler/orchestrator correctness.
