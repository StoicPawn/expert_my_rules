from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    REJECTED = "REJECTED"


class ProviderSpec(BaseModel):
    kind: str = "mock"
    model: str | None = None


class Gate(BaseModel):
    id: str
    description: str
    required: bool = True
    validator: str | None = None
    manual: bool = False


class AgentSpec(BaseModel):
    id: str
    role: str
    instructions: str
    provider: ProviderSpec | None = None


class RuntimePolicy(BaseModel):
    default_provider: ProviderSpec = Field(default_factory=ProviderSpec)
    max_steps_per_run: int = 25
    max_minutes_per_run: int = 60
    max_task_attempts: int = 3
    pause_seconds: float = 0.0


class ProjectManifest(BaseModel):
    name: str
    type: str = "custom"
    goal: str
    description: str = ""
    agents: list[AgentSpec]
    gates: list[Gate]
    validators: dict[str, str] = Field(default_factory=dict)
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)


class Task(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.OPEN
    created_by: str = "system"
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
