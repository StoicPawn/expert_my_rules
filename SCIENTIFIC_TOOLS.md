# Scientific tools and slow-inference policy

## Slow local inference: liveness, not a wall-clock deadline

Ollama generation is streamed. Expert My Rules does not terminate a local generation merely because it has taken one hour (or several hours) on a weak CPU-only machine.

The watchdog distinguishes speed from failure:

1. streamed Ollama chunks count as immediate liveness;
2. after a silent interval, Expert probes fixed Ollama health endpoints;
3. if Ollama responds, the generation continues with no total time limit;
4. only a silent stream plus repeated failed health probes becomes a technical failure;
5. a configurable `num_predict` budget limits logical output size independently of hardware speed.

The Live activity panel shows coarse operational telemetry: elapsed time, chunks, visible-output character count, silence duration and health state. Hidden reasoning/token text is never exposed as telemetry.

Configuration:

- `AWB_OLLAMA_STALL_CHECK_SECONDS` (default 60)
- `AWB_OLLAMA_HEALTH_TIMEOUT_SECONDS` (default 5)
- `AWB_OLLAMA_HEALTH_FAILURES` (default 15)
- `AWB_OLLAMA_PROGRESS_EVENT_SECONDS` (default 60)
- `AWB_OLLAMA_MAX_OUTPUT_TOKENS` (default 8192; 0 omits `num_predict`)

`AWB_OLLAMA_READ_TIMEOUT_SECONDS` remains accepted for deployment compatibility but no longer imposes a generation deadline.

## Service ownership

Scientific capability is deliberately split instead of embedding everything into one LLM service.

### Expert My Rules — orchestration and epistemic policy

Expert decides what to investigate, which evidence is required, when to falsify, when to review and which completion gates remain open. It exposes typed adapters to the services below.

### Tutor LLM — indexed knowledge and RAG

Tutor owns uploaded documents, embeddings, chunks, source/page provenance and the knowledge graph. Expert uses the grounded retrieval API directly rather than asking Tutor to generate another answer. This avoids an unnecessary second LLM layer and keeps source evidence explicit.

Available Expert tool when configured: `tutor_knowledge`.

### Research Lab — bounded computation

Research Lab owns reproducible Python execution with resource limits. Expert delegates symbolic and numerical checks there rather than installing scientific execution logic into the orchestrator container.

Available Expert tools when configured:

- `research_lab` — direct bounded experiment/context API;
- `symbolic_math` — fixed SymPy operations executed in Research Lab;
- `counterexample_search` — bounded numerical witness search executed in Research Lab.

A successful numerical search without a witness is explicitly not treated as a proof.

### Public literature metadata — candidate discovery

`literature_search` queries fixed Crossref/arXiv metadata endpoints. It is useful for finding related work and priority candidates, but a negative result is never sufficient evidence that a theorem is novel.

## Formal proof systems

Formal verification is capability-gated. Expert must not claim Lean or another proof assistant verified a theorem unless a real validator is installed and actually executed. On the current small machine, heavy formal-proof infrastructure is intentionally not installed merely to make the capability list look larger. A future stronger node can expose that validator without changing the orchestration model.

## Non-disruptive deployment

Expert My Rules' deployment already pauses continuous jobs and waits for a safe checkpoint before replacing the service. If a scientific/model task is still active, deployment refuses to interrupt it.

Research Lab deployment follows the same principle at experiment boundaries: it waits for active Lab child processes to finish and refuses a disruptive replacement. Tutor/Research Lab workflows stage connection credentials for Expert but do not restart Expert themselves; Expert applies changes only through its own safe-checkpoint deployment.
