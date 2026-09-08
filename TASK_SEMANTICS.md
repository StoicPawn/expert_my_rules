# Task and retry semantics

Expert My Rules separates epistemic progress from infrastructure reliability.

## Task lifecycle

- `OPEN`: ready for execution.
- `IN_PROGRESS`: currently executing.
- `BLOCKED`: a candidate was produced, independently reviewed/verified, and material objections remain. This is a scientific/evidentiary state, not a runtime error.
- `ERROR`: execution failed technically (timeout, model/network/runtime failure). Technical errors are retried independently and do not consume a scientific attempt.
- `DONE`: the task's candidate passed the configured review and verification stages.
- `REJECTED`: deliberate evidence-based resolution only: the task/claim was shown false, ill-posed, superseded, not required, or explicitly reframed. A numeric attempt count is never sufficient reason for rejection.

## Counters

Each task tracks separately:

- `scientific_attempts`: completed candidate → review/verification cycles.
- `technical_failures`: runtime failures before a scientific cycle completed.
- `execution_attempts`: all executions, including technical retries.
- `interrupt_recoveries`: restarts/deploy recoveries; these do not consume a scientific attempt.

Legacy mixed counters are reconciled from the durable attempt ledger when an orchestrator opens a workspace.

## Constructive recovery from BLOCKED

After a scientific block, the Director receives the actual objections and chooses an epistemically useful recovery action:

1. `retry`: use a materially different strategy that explicitly addresses the objections;
2. `decompose`: create prerequisite/falsification subtasks and return to the parent task later;
3. `reframe`: supersede the over-strong or poorly framed task with an explicit replacement;
4. `reject`: only with an admissible evidence-based resolution such as false, ill-posed, superseded, or not required.

The next Worker attempt receives the unresolved objections, the chosen recovery strategy, and recent recovery history. Repeating the same approach mechanically is explicitly disallowed.

`max_task_attempts` remains in manifests for compatibility but is no longer a hard stop. The adaptive re-plan threshold is a soft signal to prefer decomposition/reframing when repeated direct retries are not producing new information.

## Continuous technical recovery

Technical retry policy is independent of scientific task semantics. With `technical_retry_limit = 0` (the default), transient runtime failures retry indefinitely with bounded exponential backoff, while the persistent scientific ledger is preserved.
