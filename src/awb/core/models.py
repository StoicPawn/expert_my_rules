from __future__ import annotations
from enum import Enum
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    OPEN = 'OPEN'
    IN_PROGRESS = 'IN_PROGRESS'
    BLOCKED = 'BLOCKED'
    DONE = 'DONE'
    REJECTED = 'REJECTED'


class JobStatus(str, Enum):
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    PAUSED = 'PAUSED'
    CANCEL_REQUESTED = 'CANCEL_REQUESTED'
    CANCELLED = 'CANCELLED'
    COMPLETE = 'COMPLETE'
    BUDGET_FINISHED = 'BUDGET_FINISHED'
    FAILED = 'FAILED'


class ProviderSpec(BaseModel):
    """Legacy/direct provider binding kept for backwards compatibility."""
    kind: str = 'mock'
    model: str | None = None


class ComputeNodeSpec(BaseModel):
    """A physical or logical inference node that can be swapped without changing agents."""
    id: str
    kind: str = 'ollama'
    base_url: str | None = None
    base_url_env: str | None = None
    enabled: bool = True
    max_concurrency: int = 1
    priority: int = 100
    tags: list[str] = Field(default_factory=list)


class ModelRouteSpec(BaseModel):
    """Route one epistemic role to a model on a compute node."""
    node: str
    model: str | None = None
    priority: int = 100
    enabled: bool = True


class SchedulerPolicy(BaseModel):
    """Runtime health/load policy for replaceable inference nodes.

    Priority in role_routes remains the primary routing signal. The scheduler only
    adds bounded back-pressure and temporary circuit breaking so a dead or saturated
    node cannot stall an H24 autonomous project indefinitely.
    """
    enabled: bool = True
    queue_timeout_seconds: float = 120.0
    failure_threshold: int = 2
    cooldown_seconds: float = 60.0
    load_penalty: int = 10
    failure_penalty: int = 25
    allow_cooldown_probe: bool = True


class WorkflowStageSpec(BaseModel):
    """One configurable stage in the autonomous project workflow graph."""
    id: str
    kind: str
    role: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)
    enabled: bool = True
    required: bool = True


class WorkflowSpec(BaseModel):
    """Project-level workflow. Empty stages transparently use the v0.3 legacy loop."""
    stages: list[WorkflowStageSpec] = Field(default_factory=list)
    review_policy: str = 'all'


class GitIsolationPolicy(BaseModel):
    """Transactional Git worktree policy for autonomous software changes."""
    enabled: bool = False
    auto_init: bool = True
    checkpoint_dirty: bool = False
    merge_approved: bool = True
    discard_rejected: bool = True
    patch_context_chars: int = 60_000
    protected_paths: list[str] = Field(default_factory=lambda: [
        'project.yaml', 'ledger.sqlite3', 'artifacts', 'logs', '.awb', '.git'
    ])


class EscalationPolicy(BaseModel):
    enabled: bool = False
    cloud_provider: ProviderSpec = Field(default_factory=lambda: ProviderSpec(kind='openai', model='gpt-5'))
    daily_budget_eur: float = 0.0
    max_cloud_calls_per_run: int = 0
    after_local_failures: int = 3
    priority_threshold: float = 9.0
    roles: list[str] = Field(default_factory=lambda: ['worker', 'reviewer'])


class Gate(BaseModel):
    id: str
    description: str
    required: bool = True
    validator: str | None = None
    manual: bool = False


class ToolSpec(BaseModel):
    id: str
    type: str
    description: str = ''
    command: str | None = None
    writable: bool = False
    enabled: bool = True
    timeout_seconds: int = 120


class AgentSpec(BaseModel):
    id: str
    role: str
    instructions: str
    provider: ProviderSpec | None = None
    tools: list[str] = Field(default_factory=list)


class RuntimePolicy(BaseModel):
    # Legacy provider remains valid for every existing workspace.
    default_provider: ProviderSpec = Field(default_factory=lambda: ProviderSpec(kind='ollama', model='qwen3:4b'))
    # v0.3 scalable runtime. Empty values mean "use the legacy provider path".
    compute_nodes: list[ComputeNodeSpec] = Field(default_factory=list)
    role_routes: dict[str, list[ModelRouteSpec]] = Field(default_factory=dict)
    # v0.5 adaptive scheduling. Defaults are safe for existing manifests.
    scheduler: SchedulerPolicy = Field(default_factory=SchedulerPolicy)
    # v0.4 transactional software workspace. Disabled by default for old manifests.
    git: GitIsolationPolicy = Field(default_factory=GitIsolationPolicy)
    escalation: EscalationPolicy = Field(default_factory=EscalationPolicy)
    max_steps_per_run: int = 25
    max_minutes_per_run: int = 60
    max_task_attempts: int = 3
    max_tool_calls_per_task: int = 12
    continuous_session_steps: int = 50
    continuous_session_minutes: int = 30
    checkpoint_pause_seconds: float = 2.0
    pause_seconds: float = 0.0


class ProjectManifest(BaseModel):
    name: str
    type: str = 'custom'
    goal: str
    description: str = ''
    agents: list[AgentSpec]
    workflow: WorkflowSpec = Field(default_factory=WorkflowSpec)
    gates: list[Gate]
    validators: dict[str, str] = Field(default_factory=dict)
    tools: list[ToolSpec] = Field(default_factory=list)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)


class Task(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.OPEN
    created_by: str = 'system'
    priority: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Review(BaseModel):
    approved: bool
    critical_objections: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class IterationResult(BaseModel):
    task: Task
    work_output: str
    review: Review
    verification_passed: bool
    verification_detail: str
    next_task: Task | None = None


class Workspace(BaseModel):
    root: Path
    manifest: ProjectManifest
