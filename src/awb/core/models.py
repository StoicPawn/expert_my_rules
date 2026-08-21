from __future__ import annotations
from enum import Enum
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    OPEN='OPEN'; IN_PROGRESS='IN_PROGRESS'; BLOCKED='BLOCKED'; DONE='DONE'; REJECTED='REJECTED'
class JobStatus(str, Enum):
    QUEUED='QUEUED'; RUNNING='RUNNING'; PAUSED='PAUSED'; CANCEL_REQUESTED='CANCEL_REQUESTED'; CANCELLED='CANCELLED'; COMPLETE='COMPLETE'; BUDGET_FINISHED='BUDGET_FINISHED'; FAILED='FAILED'
class ProviderSpec(BaseModel):
    kind: str='mock'; model: str|None=None
class EscalationPolicy(BaseModel):
    enabled: bool=False
    cloud_provider: ProviderSpec=Field(default_factory=lambda: ProviderSpec(kind='openai', model='gpt-5'))
    daily_budget_eur: float=0.0
    max_cloud_calls_per_run: int=0
    after_local_failures: int=3
    priority_threshold: float=9.0
    roles: list[str]=Field(default_factory=lambda:['worker','reviewer'])
class Gate(BaseModel):
    id: str; description: str; required: bool=True; validator: str|None=None; manual: bool=False
class ToolSpec(BaseModel):
    id: str; type: str; description: str=''; command: str|None=None; writable: bool=False; enabled: bool=True; timeout_seconds: int=120
class AgentSpec(BaseModel):
    id: str; role: str; instructions: str; provider: ProviderSpec|None=None; tools: list[str]=Field(default_factory=list)
class RuntimePolicy(BaseModel):
    default_provider: ProviderSpec=Field(default_factory=lambda: ProviderSpec(kind='ollama', model='qwen3:8b'))
    escalation: EscalationPolicy=Field(default_factory=EscalationPolicy)
    max_steps_per_run: int=25
    max_minutes_per_run: int=60
    max_task_attempts: int=3
    max_tool_calls_per_task: int=12
    continuous_session_steps: int=50
    continuous_session_minutes: int=30
    checkpoint_pause_seconds: float=2.0
    pause_seconds: float=0.0
class ProjectManifest(BaseModel):
    name: str; type: str='custom'; goal: str; description: str=''
    agents: list[AgentSpec]; gates: list[Gate]
    validators: dict[str,str]=Field(default_factory=dict)
    tools: list[ToolSpec]=Field(default_factory=list)
    runtime: RuntimePolicy=Field(default_factory=RuntimePolicy)
class Task(BaseModel):
    id: str; title: str; description: str; status: TaskStatus=TaskStatus.OPEN; created_by: str='system'; priority: float=1.0; metadata: dict[str,Any]=Field(default_factory=dict)
class Review(BaseModel):
    approved: bool; critical_objections: list[str]=Field(default_factory=list); recommendations: list[str]=Field(default_factory=list)
class IterationResult(BaseModel):
    task: Task; work_output: str; review: Review; verification_passed: bool; verification_detail: str; next_task: Task|None=None
class Workspace(BaseModel):
    root: Path; manifest: ProjectManifest
