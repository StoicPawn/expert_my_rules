from __future__ import annotations

from collections import deque

from .models import ProjectManifest, WorkflowStageSpec


LEGACY_STAGES = [
    WorkflowStageSpec(id='select', kind='select_task', role='director'),
    WorkflowStageSpec(id='execute', kind='execute', role='worker', depends_on=['select']),
    WorkflowStageSpec(id='review', kind='review', role='reviewer', depends_on=['execute']),
    WorkflowStageSpec(id='validate', kind='validate', depends_on=['review']),
]

SUPPORTED_KINDS = {'select_task', 'execute', 'review', 'validate'}


class WorkflowConfigurationError(ValueError):
    pass


class WorkflowGraph:
    """Validate and topologically order a configurable autonomous workflow.

    The v0.3 Director -> Worker -> Reviewer -> Validator loop remains the fallback
    when a workspace has no explicit workflow stages.
    """

    def __init__(self, manifest: ProjectManifest):
        self.review_policy = manifest.workflow.review_policy or 'all'
        stages = [s for s in manifest.workflow.stages if s.enabled]
        self.stages = stages or [s.model_copy(deep=True) for s in LEGACY_STAGES]
        self._by_id = {s.id: s for s in self.stages}
        self._validate()
        self.ordered = self._topological_order()

    def _validate(self) -> None:
        if len(self._by_id) != len(self.stages):
            raise WorkflowConfigurationError('Workflow stage ids must be unique')
        unknown = [s.kind for s in self.stages if s.kind not in SUPPORTED_KINDS]
        if unknown:
            raise WorkflowConfigurationError(f'Unsupported workflow stage kind(s): {sorted(set(unknown))}')
        for stage in self.stages:
            missing = [dep for dep in stage.depends_on if dep not in self._by_id]
            if missing:
                raise WorkflowConfigurationError(f'Stage {stage.id} depends on unknown stage(s): {missing}')
        if not any(s.kind == 'execute' for s in self.stages):
            raise WorkflowConfigurationError('Workflow requires at least one execute stage')
        if self.review_policy not in {'all', 'any'}:
            raise WorkflowConfigurationError("review_policy must be 'all' or 'any'")

    def _topological_order(self) -> list[WorkflowStageSpec]:
        indegree = {s.id: 0 for s in self.stages}
        children: dict[str, list[str]] = {s.id: [] for s in self.stages}
        for stage in self.stages:
            for dep in stage.depends_on:
                indegree[stage.id] += 1
                children[dep].append(stage.id)
        ready = deque(s.id for s in self.stages if indegree[s.id] == 0)
        ordered: list[WorkflowStageSpec] = []
        while ready:
            stage_id = ready.popleft()
            ordered.append(self._by_id[stage_id])
            for child in children[stage_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(ordered) != len(self.stages):
            raise WorkflowConfigurationError('Workflow contains a dependency cycle')
        return ordered

    def by_kind(self, kind: str) -> list[WorkflowStageSpec]:
        return [s for s in self.ordered if s.kind == kind]

    def first(self, kind: str) -> WorkflowStageSpec | None:
        return next((s for s in self.ordered if s.kind == kind), None)
