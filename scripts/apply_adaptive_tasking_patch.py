from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {text.count(old)}")
    return text.replace(old, new, 1)


def splice_method(text: str, start_sig: str, next_sig: str, replacement: str, label: str) -> str:
    start = text.index(start_sig)
    end = text.index(next_sig, start)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:]


def patch_models() -> None:
    path = "src/awb/core/models.py"
    text = read(path)
    text = replace_once(
        text,
        """class TaskStatus(str, Enum):\n    OPEN = 'OPEN'\n    IN_PROGRESS = 'IN_PROGRESS'\n    BLOCKED = 'BLOCKED'\n    DONE = 'DONE'\n    REJECTED = 'REJECTED'\n""",
        """class TaskStatus(str, Enum):\n    OPEN = 'OPEN'\n    IN_PROGRESS = 'IN_PROGRESS'\n    # Scientific/evidentiary blocker after a candidate was actually reviewed.\n    BLOCKED = 'BLOCKED'\n    # Technical/runtime failure. This is retryable and never consumes a scientific attempt.\n    ERROR = 'ERROR'\n    DONE = 'DONE'\n    # Deliberate epistemic resolution only (false/ill-posed/superseded/reframed), never an attempt-limit timeout.\n    REJECTED = 'REJECTED'\n""",
        "TaskStatus",
    )
    text = replace_once(
        text,
        """    max_steps_per_run: int = 25\n    max_minutes_per_run: int = 60\n    max_task_attempts: int = 3\n    max_tool_calls_per_task: int = 12\n    continuous_session_steps: int = 50\n    continuous_session_minutes: int = 30\n    checkpoint_pause_seconds: float = 2.0\n    pause_seconds: float = 0.0\n""",
        """    max_steps_per_run: int = 25\n    max_minutes_per_run: int = 60\n    # Kept for manifest compatibility. It is now a soft re-planning signal, not a hard rejection cap.\n    max_task_attempts: int = 3\n    adaptive_replan_after_scientific_attempts: int = 3\n    # 0 means unlimited autonomous technical retries; they use bounded exponential backoff.\n    technical_retry_limit: int = 0\n    technical_retry_backoff_max_seconds: float = 300.0\n    recovery_history_limit: int = 8\n    max_tool_calls_per_task: int = 12\n    continuous_session_steps: int = 50\n    continuous_session_minutes: int = 30\n    checkpoint_pause_seconds: float = 2.0\n    pause_seconds: float = 0.0\n""",
        "RuntimePolicy",
    )
    write(path, text)


def patch_storage() -> None:
    path = "src/awb/core/storage.py"
    text = read(path)
    start = text.index("    def recover_interrupted_tasks(self):")
    end = text.index("    def event(", start)
    recovery = '''    def recover_interrupted_tasks(self):
        rows = self.conn.execute("SELECT id,metadata_json FROM tasks WHERE status='IN_PROGRESS'").fetchall()
        recovered = []
        now = self._now()
        for r in rows:
            metadata = json.loads(r['metadata_json'])
            count = int(metadata.get('interrupt_recoveries', metadata.get('interrupted_recovery_count', 0))) + 1
            metadata['interrupt_recoveries'] = count
            metadata['interrupted_recovery_count'] = count  # legacy alias
            self.conn.execute(
                'UPDATE tasks SET status=?,metadata_json=?,updated_at=? WHERE id=?',
                (TaskStatus.OPEN.value, json.dumps(metadata), now, r['id']),
            )
            self.conn.execute(
                """UPDATE attempts
                   SET status='INTERRUPTED',
                       error=CASE WHEN error='' THEN 'interrupted by process stop or restart' ELSE error END,
                       finished_at=?
                   WHERE task_id=? AND status='RUNNING'""",
                (now, r['id']),
            )
            recovered.append(r['id'])
        if recovered:
            self.conn.commit()
            for task_id in recovered:
                self.event(
                    'task_recovered',
                    {'reason': 'task was interrupted and reopened without consuming a scientific attempt'},
                    task_id,
                )
        return recovered

'''
    text = text[:start] + recovery + text[end:]

    anchor = "    def record_model_call(self,*,task_id=None,role:str,node:str,kind:str,model:str|None,source:str,success:bool,seconds:float,chars:int=0,error:str=''):\n"
    reconcile = '''    def reconcile_task_counters(self):
        """Rebuild task counters from the durable attempt ledger.

        Old versions mixed scientific rejections, timeouts and restarts in one
        `attempts` counter. This migration is intentionally local to each workspace:
        failed runtime attempts become technical failures, interrupted attempts are
        recoveries, and only reviewed outcomes count as scientific attempts.
        """
        task_rows = self.conn.execute('SELECT * FROM tasks').fetchall()
        changed = []
        events = []
        for row in task_rows:
            metadata = json.loads(row['metadata_json'])
            attempts = self.conn.execute(
                'SELECT status,error FROM attempts WHERE task_id=? ORDER BY started_at ASC',
                (row['id'],),
            ).fetchall()
            if attempts:
                scientific = sum(1 for a in attempts if a['status'] in {'BLOCKED', 'DONE', 'REJECTED'})
                technical = sum(1 for a in attempts if a['status'] in {'FAILED', 'TECHNICAL_ERROR'})
                interrupted = sum(1 for a in attempts if a['status'] == 'INTERRUPTED')
                execution = len(attempts)
                latest_status = attempts[-1]['status']
            else:
                scientific = int(metadata.get('scientific_attempts', metadata.get('attempts', 0)))
                technical = int(metadata.get('technical_failures', 0))
                interrupted = int(metadata.get('interrupt_recoveries', metadata.get('interrupted_recovery_count', 0)))
                execution = max(int(metadata.get('execution_attempts', 0)), scientific + technical + interrupted)
                latest_status = ''

            metadata['scientific_attempts'] = scientific
            metadata['technical_failures'] = technical
            metadata['execution_attempts'] = execution
            metadata['interrupt_recoveries'] = max(
                interrupted,
                int(metadata.get('interrupt_recoveries', metadata.get('interrupted_recovery_count', 0))),
            )
            metadata['interrupted_recovery_count'] = metadata['interrupt_recoveries']
            metadata['attempts'] = scientific  # compatibility alias used by older clients

            status = row['status']
            if status == TaskStatus.REJECTED.value and not metadata.get('rejection_reason') and not metadata.get('resolution'):
                status = TaskStatus.BLOCKED.value
                metadata['legacy_rejection_reopened'] = True
                events.append(('legacy_rejection_reopened', {'scientific_attempts': scientific}, row['id']))
            if status == TaskStatus.BLOCKED.value and (
                latest_status in {'FAILED', 'TECHNICAL_ERROR'} or (not attempts and metadata.get('last_error') and scientific == 0)
            ):
                status = TaskStatus.ERROR.value
                events.append(('legacy_technical_block_reclassified', {'technical_failures': technical}, row['id']))

            old_metadata = json.loads(row['metadata_json'])
            if status != row['status'] or metadata != old_metadata:
                self.conn.execute(
                    'UPDATE tasks SET status=?,metadata_json=?,updated_at=? WHERE id=?',
                    (status, json.dumps(metadata), self._now(), row['id']),
                )
                changed.append(row['id'])
        if changed:
            self.conn.commit()
        for kind, payload, task_id in events:
            self.event(kind, payload, task_id)
        return changed

'''
    if anchor not in text:
        raise RuntimeError("storage reconcile anchor missing")
    text = text.replace(anchor, reconcile + anchor, 1)
    write(path, text)


def patch_orchestrator() -> None:
    path = "src/awb/core/orchestrator.py"
    text = read(path)
    gate_line = 'GATE_SYSTEM="""You are the independent completion gatekeeper. Evaluate ONE completion condition conservatively from the recorded project evidence. Never pass a gate because progress merely looks promising. Never infer missing literature checks, tests, proofs, artifacts or external verification. If evidence is insufficient, keep it open. Return GATE_JSON only as JSON with: passed (bool), detail (str)."""\n'
    recovery_system = '''RECOVERY_SYSTEM="""You are the Director repairing a scientifically BLOCKED task. A candidate was actually produced and challenged by an independent Reviewer/Verifier. Make progress without mechanically repeating the failed approach. Return RECOVERY_JSON only as JSON with: action (retry|decompose|reframe|reject), strategy (concrete materially different next approach), rationale, resolution_type (empty unless reject; one of false|ill_posed|superseded|not_required), replacement_title, replacement_description, subtasks (list of objects with title, description, priority). A retry must directly address the listed objections and must not merely ask the same question again. Decompose when prerequisite work or separate falsification checks are needed. Reframe when the original formulation should be replaced by a stronger/true/useful formulation. Reject only when evidence shows the original task/claim is false, ill-posed, superseded, or no longer required; NEVER reject merely because a numeric attempt threshold was reached. After several scientific blocks, prefer a structural change unless a genuinely new route is available."""\n'''
    text = replace_once(text, gate_line, gate_line + recovery_system, "recovery system")

    old_init = '''        recovered=self.ledger.recover_interrupted_tasks()\n        if recovered:\n            self.ledger.event('interrupted_tasks_recovered',{'task_ids':recovered})\n        for gate in workspace.manifest.gates:\n'''
    new_init = '''        recovered=self.ledger.recover_interrupted_tasks()\n        if recovered:\n            self.ledger.event('interrupted_tasks_recovered',{'task_ids':recovered})\n        reconciled=self.ledger.reconcile_task_counters()\n        if reconciled:\n            self.ledger.event('task_counters_reconciled',{'task_ids':reconciled})\n        for gate in workspace.manifest.gates:\n'''
    text = replace_once(text, old_init, new_init, "orchestrator init reconciliation")

    text = replace_once(
        text,
        """        attempts=int(task.metadata.get('attempts',0))\n        return attempts>=p.after_local_failures or task.priority>=p.priority_threshold\n""",
        """        difficulty=max(\n            int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0))),\n            int(task.metadata.get('technical_failures',0)),\n        )\n        return difficulty>=p.after_local_failures or task.priority>=p.priority_threshold\n""",
        "escalation counters",
    )

    choose_and_helpers = r'''    def choose_next_task(self):
        existing=self.ledger.list_tasks([TaskStatus.OPEN])
        if existing:
            return existing[0]

        technical=self.ledger.list_tasks([TaskStatus.ERROR])
        if technical:
            task=technical[0]
            task.status=TaskStatus.OPEN
            self.ledger.upsert_task(task)
            self.ledger.event('task_reopened_after_technical_error',{
                'technical_failures':task.metadata.get('technical_failures',0),
                'last_error':task.metadata.get('last_error',''),
            },task.id)
            return task

        blocked=self.ledger.list_tasks([TaskStatus.BLOCKED])
        if blocked:
            task=blocked[0]
            task.status=TaskStatus.OPEN
            self.ledger.upsert_task(task)
            self.ledger.event('task_reopened_after_review',{
                'scientific_attempts':task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)),
                'next_strategy':task.metadata.get('next_strategy',''),
                'critical_objections':task.metadata.get('critical_objections',[])[:6],
            },task.id)
            return task

        selector=self.workflow.first('select_task')
        role=selector.role if selector and selector.role else 'director'
        context=self.snapshot()
        rejected=self.ledger.list_tasks([TaskStatus.REJECTED])
        if rejected:
            context+='\nRESOLVED/REJECTED TASKS (do not blindly recreate them):\n'+json.dumps([t.model_dump(mode='json') for t in rejected[:10]],indent=2)
        raw=self._call_model(role,DIRECTOR_SYSTEM+'\n'+self._instructions(role),context)
        try:
            d=json.loads(raw)
        except json.JSONDecodeError:
            d={'title':'Resolve next project gap','description':raw,'priority':1.0}
        task=Task(
            id=f'TASK-{uuid.uuid4().hex[:8].upper()}',
            title=str(d.get('title','Next task')),
            description=str(d.get('description','Advance the project goal.')),
            priority=float(d.get('priority',1.0)),
            created_by=role,
        )
        self.ledger.upsert_task(task)
        self.ledger.event('task_created',task.model_dump(mode='json'),task.id)
        return task

    @staticmethod
    def _clip(value,limit=1200):
        text=str(value or '').strip()
        return text if len(text)<=limit else text[:limit]+'…'

    def _recovery_context(self,task:Task):
        strategy=self._clip(task.metadata.get('next_strategy',''),1600)
        objections=[self._clip(x,800) for x in task.metadata.get('critical_objections',[])[:8]]
        history=task.metadata.get('recovery_history',[])[-3:]
        if not strategy and not objections and not history:
            return 'none; this is the first scientific attempt'
        payload={
            'scientific_attempts_completed':int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0))),
            'strategy_for_this_attempt':strategy,
            'unresolved_objections':objections,
            'recent_recovery_history':history,
        }
        return json.dumps(payload,indent=2,ensure_ascii=False)

    def _spawn_recovery_task(self,parent:Task,title,description,priority,kind):
        title=str(title or '').strip()
        if not title:
            return None
        for existing in self.ledger.list_tasks():
            if existing.metadata.get('parent_task_id')==parent.id and existing.title==title and existing.status!=TaskStatus.REJECTED:
                return existing
        child=Task(
            id=f'TASK-{uuid.uuid4().hex[:8].upper()}',
            title=title,
            description=str(description or title).strip(),
            priority=float(priority if priority is not None else parent.priority),
            created_by='director-recovery',
            metadata={
                'parent_task_id':parent.id,
                'origin':'blocked_recovery',
                'recovery_kind':kind,
            },
        )
        self.ledger.upsert_task(child)
        self.ledger.event('task_created',child.model_dump(mode='json'),child.id)
        return child

    def _plan_blocked_recovery(self,task:Task,review:Review,verification_detail:str):
        scientific=int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)))
        threshold=max(1,int(self.workspace.manifest.runtime.adaptive_replan_after_scientific_attempts))
        objections=review.critical_objections or task.metadata.get('critical_objections',[]) or [verification_detail]
        recommendations=review.recommendations or []
        prompt=(
            f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nBLOCKED TASK:\n{task.model_dump_json()}\n\n'
            f'SCIENTIFIC ATTEMPTS COMPLETED: {scientific}\nSOFT STRUCTURAL-REPLAN THRESHOLD: {threshold}\n\n'
            f'CRITICAL OBJECTIONS:\n{json.dumps(objections,indent=2,ensure_ascii=False)}\n\n'
            f'REVIEW RECOMMENDATIONS:\n{json.dumps(recommendations,indent=2,ensure_ascii=False)}\n\n'
            f'VERIFICATION:\n{verification_detail}\n\n'
            'Choose the next epistemically useful move. A numeric attempt count is never a reason to give up.'
        )
        role='director'
        fallback={
            'action':'retry',
            'strategy':'Attack the unresolved objections explicitly with a materially different route; first try to falsify the previous candidate, then rebuild only what survives.',
            'rationale':'Recovery planner unavailable; use conservative objection-driven fallback.',
            'resolution_type':'',
            'replacement_title':'',
            'replacement_description':'',
            'subtasks':[],
        }
        try:
            raw=self._call_model(role,RECOVERY_SYSTEM+'\n'+self._instructions(role),prompt,task)
            plan=json.loads(raw)
            if not isinstance(plan,dict):
                raise ValueError('RECOVERY_JSON must be an object')
        except Exception as exc:
            task.metadata['recovery_planner_failures']=int(task.metadata.get('recovery_planner_failures',0))+1
            self.ledger.event('recovery_planner_error',{
                'error':f'{type(exc).__name__}: {exc}',
                'fallback':'retry with materially different objection-driven strategy',
            },task.id)
            plan=fallback

        action=str(plan.get('action','retry')).strip().lower()
        if action not in {'retry','decompose','reframe','reject'}:
            action='retry'
        strategy=self._clip(plan.get('strategy') or fallback['strategy'],2200)
        rationale=self._clip(plan.get('rationale') or '',1800)
        resolution_type=str(plan.get('resolution_type') or '').strip().lower()
        if action=='reject' and resolution_type not in {'false','ill_posed','superseded','not_required'}:
            action='retry'
            rationale=(rationale+' Reject was downgraded because no admissible evidence-based resolution_type was supplied.').strip()

        history=list(task.metadata.get('recovery_history',[]))
        history.append({
            'scientific_attempt':scientific,
            'action':action,
            'strategy':strategy,
            'rationale':rationale,
            'objections':[self._clip(x,500) for x in objections[:6]],
        })
        limit=max(1,int(self.workspace.manifest.runtime.recovery_history_limit))
        task.metadata['recovery_history']=history[-limit:]
        task.metadata['next_strategy']=strategy
        task.metadata['last_recovery_action']=action
        task.metadata['last_recovery_rationale']=rationale

        subtasks=plan.get('subtasks') if isinstance(plan.get('subtasks'),list) else []
        created=[]
        if action=='decompose':
            for item in subtasks[:6]:
                if not isinstance(item,dict):
                    continue
                child=self._spawn_recovery_task(
                    task,
                    item.get('title'),
                    item.get('description'),
                    item.get('priority',task.priority+0.1),
                    'decompose',
                )
                if child:
                    created.append(child.id)
            if not created:
                child=self._spawn_recovery_task(
                    task,
                    f'Falsify blocker for {task.title}',
                    'Independently isolate and attack the strongest unresolved objection before retrying the parent task.',
                    task.priority+0.1,
                    'decompose-fallback',
                )
                if child:
                    created.append(child.id)
            task.metadata['waiting_on_recovery_tasks']=created
            task.status=TaskStatus.BLOCKED
            self.ledger.event('task_decomposed',{'strategy':strategy,'subtask_ids':created},task.id)
        elif action=='reframe':
            replacement_title=str(plan.get('replacement_title') or f'Reframe: {task.title}').strip()
            replacement_description=str(plan.get('replacement_description') or strategy or task.description).strip()
            child=self._spawn_recovery_task(task,replacement_title,replacement_description,task.priority,'reframe')
            task.status=TaskStatus.REJECTED
            task.metadata['resolution']='reframed'
            task.metadata['rejection_reason']=rationale or 'Original formulation was superseded by an explicit replacement.'
            task.metadata['replacement_task_id']=child.id if child else None
            self.ledger.event('task_reframed',{
                'replacement_task_id':child.id if child else None,
                'strategy':strategy,
                'rationale':task.metadata['rejection_reason'],
            },task.id)
        elif action=='reject':
            task.status=TaskStatus.REJECTED
            task.metadata['resolution']=resolution_type
            task.metadata['rejection_reason']=rationale or f'Evidence-based resolution: {resolution_type}'
            task.metadata['rejection_evidence_basis']=self._clip(plan.get('evidence_basis') or '',1800)
            self.ledger.event('task_rejected_by_evidence',{
                'resolution_type':resolution_type,
                'rationale':task.metadata['rejection_reason'],
            },task.id)
        else:
            task.status=TaskStatus.BLOCKED
            self.ledger.event('task_recovery_planned',{
                'scientific_attempts':scientific,
                'strategy':strategy,
                'rationale':rationale,
                'structural_replan_recommended':scientific>=threshold,
            },task.id)
        return plan
'''
    text = splice_method(text, "    def choose_next_task(self):", "    def _execute_with_tools", choose_and_helpers, "choose_next_task")

    old_prompt = '''                    prompt=(\n                        f'NORTH STAR:\\n{self.workspace.manifest.goal}\\n\\nTASK:\\n{task.title}\\n{task.description}\\n\\n'\n                        f'STATE:\\n{self.snapshot()}\\n\\nPRIOR STAGE OUTPUTS:\\n{prior or "none"}'\n                    )\n'''
    new_prompt = '''                    prompt=(\n                        f'NORTH STAR:\\n{self.workspace.manifest.goal}\\n\\nTASK:\\n{task.title}\\n{task.description}\\n\\n'\n                        f'RECOVERY CONTEXT:\\n{self._recovery_context(task)}\\n\\n'\n                        'If recovery context contains prior objections, do not repeat the same approach mechanically. '\n                        'Explicitly explain how this attempt differs and how it resolves or falsifies those objections.\\n\\n'\n                        f'STATE:\\n{self.snapshot()}\\n\\nPRIOR STAGE OUTPUTS:\\n{prior or "none"}'\n                    )\n'''
    text = replace_once(text, old_prompt, new_prompt, "worker recovery prompt")

    new_step = r'''    def step(self):
        task=self.choose_next_task()
        execution_attempt=int(task.metadata.get('execution_attempts',0))+1
        task.metadata['execution_attempts']=execution_attempt
        task.status=TaskStatus.IN_PROGRESS
        task.metadata.pop('last_error',None)
        active_strategy=task.metadata.get('next_strategy','')
        self.ledger.upsert_task(task)
        self.ledger.event('task_started',{
            'execution_attempt':execution_attempt,
            'scientific_attempt_next':int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)))+1,
            'strategy':active_strategy,
        },task.id)
        attempt_id=self.ledger.start_attempt(task.id,execution_attempt)
        self._attempt_routes=[]
        task_workspace=None
        try:
            task_workspace=self.git.prepare(task.id) if self.git.enabled else None
            execution_root=task_workspace.path if task_workspace else self.workspace.root
            if task_workspace:
                self.ledger.event('git_worktree_ready',{'branch':task_workspace.branch,'path':str(task_workspace.path)},task.id)

            outputs:dict[str,str]={}
            stage_reviews:list[tuple[WorkflowStageSpec,Review]]=[]
            verification_records=[]

            for stage in self.workflow.ordered:
                if stage.kind=='select_task':
                    continue
                self.ledger.event('workflow_stage_started',{'stage':stage.id,'kind':stage.kind,'role':stage.role},task.id)
                if stage.kind=='execute':
                    prior='\n\n'.join(f'{k}:\n{v}' for k,v in outputs.items())
                    prompt=(
                        f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.title}\n{task.description}\n\n'
                        f'RECOVERY CONTEXT:\n{self._recovery_context(task)}\n\n'
                        'If recovery context contains prior objections, do not repeat the same approach mechanically. '
                        'Explicitly explain how this attempt differs and how it resolves or falsifies those objections.\n\n'
                        f'STATE:\n{self.snapshot()}\n\nPRIOR STAGE OUTPUTS:\n{prior or "none"}'
                    )
                    outputs[stage.id]=self._execute_with_tools(task,stage,prompt,execution_root)
                    self.ledger.event('work_output',{'stage':stage.id,'text':outputs[stage.id]},task.id)
                elif stage.kind=='review':
                    candidate='\n\n'.join(outputs.values())
                    patch=self.git.patch(task.id) if self.git.enabled else ''
                    review=self._review_stage(task,stage,candidate,patch)
                    stage_reviews.append((stage,review))
                    self.ledger.event('review',{'stage':stage.id,'role':stage.role,**review.model_dump(mode='json')},task.id)
                elif stage.kind=='validate':
                    ok,detail=self.verify(task,stage.validators or None,cwd=execution_root)
                    verification_records.append((stage,ok,detail))
                    self.ledger.event('verification',{'stage':stage.id,'passed':ok,'detail':detail},task.id)
                self.ledger.event('workflow_stage_finished',{'stage':stage.id,'kind':stage.kind},task.id)

            output='\n\n'.join(outputs.values())
            if not output:
                raise RuntimeError('Workflow produced no execution output')
            aggregate_review=self._aggregate_reviews(stage_reviews)
            required_verifications=[(s,ok,detail) for s,ok,detail in verification_records if s.required]
            considered_verifications=required_verifications or verification_records
            verified=all(ok for _,ok,_ in considered_verifications) if considered_verifications else True
            verification_detail='\n\n'.join(f'[{s.id}] {detail}' for s,_,detail in verification_records) or 'No workflow validation stage configured.'

            scientific_attempts=int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)))+1
            task.metadata['scientific_attempts']=scientific_attempts
            task.metadata['attempts']=scientific_attempts
            task.metadata['last_completed_strategy']=active_strategy
            task.metadata['last_review_recommendations']=aggregate_review.recommendations[:12]
            task.metadata.pop('last_error',None)

            patch=self.git.patch(task.id) if self.git.enabled else ''
            patch_summary=self.git.diff_summary(task.id) if self.git.enabled else ''
            patch_artifact=self._save_patch(task,patch)

            if aggregate_review.approved and verified:
                task.status=TaskStatus.DONE
                task.metadata.pop('critical_objections',None)
                task.metadata.pop('next_strategy',None)
                task.metadata.pop('waiting_on_recovery_tasks',None)
                self.ledger.event('task_scientifically_closed',{'scientific_attempts':scientific_attempts},task.id)
            else:
                task.status=TaskStatus.BLOCKED
                task.metadata['critical_objections']=aggregate_review.critical_objections or [verification_detail]
                task.metadata['last_verification_detail']=verification_detail
                self.ledger.event('task_blocked',{
                    'scientific_attempts':scientific_attempts,
                    'critical_objections':task.metadata['critical_objections'][:8],
                    'verification_passed':verified,
                },task.id)
                self._plan_blocked_recovery(task,aggregate_review,verification_detail)

            review_payload={stage.id:review.model_dump(mode='json') for stage,review in stage_reviews}
            artifact=self._save_artifact(task,output,aggregate_review,review_payload,verification_detail,patch_artifact,patch_summary)
            task.metadata['artifact']=str(artifact.relative_to(self.workspace.root))
            if patch_artifact:
                task.metadata['patch_artifact']=patch_artifact

            if self.git.enabled and task.status==TaskStatus.DONE:
                merge_result=self.git.merge(task.id,f'AWB: {task.title}')
                task.metadata['git_merge']=merge_result
                self.ledger.event('git_candidate_merged',merge_result,task.id)
            elif self.git.enabled and task.status==TaskStatus.REJECTED:
                self.git.discard(task.id)
                self.ledger.event('git_candidate_discarded',{'reason':task.metadata.get('rejection_reason','evidence-based resolution')},task.id)
            elif self.git.enabled and task.status==TaskStatus.BLOCKED:
                self.ledger.event('git_candidate_preserved',{'reason':'scientific review blocker; recovery planned'},task.id)

            self.ledger.upsert_task(task)
            self.evaluate_gates()
            attempt_review={**aggregate_review.model_dump(mode='json'),'stages':review_payload}
            self.ledger.finish_attempt(
                attempt_id,
                status=task.status.value,
                route={'calls':self._attempt_routes},
                review=attempt_review,
                verification={'passed':verified,'detail':verification_detail},
                artifact=task.metadata['artifact'],
            )
            next_task=None if self.is_complete() else self.choose_next_task()
            return IterationResult(
                task=task,
                work_output=output,
                review=aggregate_review,
                verification_passed=verified,
                verification_detail=verification_detail,
                next_task=next_task,
            )
        except Exception as exc:
            task.status=TaskStatus.ERROR
            task.metadata['technical_failures']=int(task.metadata.get('technical_failures',0))+1
            task.metadata['last_error']=f'{type(exc).__name__}: {exc}'
            task.metadata['attempts']=int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)))
            self.ledger.upsert_task(task)
            self.ledger.event('task_technical_error',{
                'error':task.metadata['last_error'],
                'technical_failures':task.metadata['technical_failures'],
                'scientific_attempts':task.metadata.get('scientific_attempts',task.metadata.get('attempts',0)),
            },task.id)
            self.ledger.finish_attempt(attempt_id,status='TECHNICAL_ERROR',route={'calls':self._attempt_routes},error=task.metadata['last_error'])
            raise
'''
    text = splice_method(text, "    def step(self):", "    def run(", new_step, "step")
    write(path, text)


def patch_templates() -> None:
    path = "src/awb/templates/templates.py"
    text = read(path)
    text = replace_once(
        text,
        """        'max_steps_per_run': 25,\n        'max_minutes_per_run': 60,\n        'max_task_attempts': 3,\n        'max_tool_calls_per_task': 12,\n""",
        """        'max_steps_per_run': 25,\n        'max_minutes_per_run': 60,\n        'max_task_attempts': 3,\n        'adaptive_replan_after_scientific_attempts': 3,\n        'technical_retry_limit': 0,\n        'technical_retry_backoff_max_seconds': 300.0,\n        'recovery_history_limit': 8,\n        'max_tool_calls_per_task': 12,\n""",
        "template runtime",
    )
    write(path, text)


def patch_resilience() -> None:
    path = "src/awb/web/resilience.py"
    text = read(path)
    text = replace_once(
        text,
        """    consecutive_errors = 0\n    max_errors = max(6, ws.manifest.runtime.max_task_attempts * 3)\n""",
        """    consecutive_errors = 0\n    retry_limit = max(0, int(ws.manifest.runtime.technical_retry_limit))\n    max_backoff = max(2.0, float(ws.manifest.runtime.technical_retry_backoff_max_seconds))\n""",
        "resilience counters",
    )
    old = """            ledger.event('continuous_runtime_retry', {\n                'error_type': error_type,\n                'consecutive_errors': consecutive_errors,\n                'max_errors': max_errors,\n            })\n            if consecutive_errors >= max_errors:\n                ledger.update_job(\n                    job_id,\n                    status=JobStatus.FAILED,\n                    detail=f'persistent runtime failure after {consecutive_errors} retries: {error_type}',\n                )\n                return\n            delay = min(60.0, 2.0 ** min(consecutive_errors, 5))\n            ledger.update_job(\n                job_id,\n                status=JobStatus.RUNNING,\n                detail=f'transient runtime failure ({error_type}); retry {consecutive_errors}/{max_errors} in {delay:.0f}s',\n            )\n            time.sleep(delay)\n"""
    new = """            ledger.event('continuous_runtime_retry', {\n                'error_type': error_type,\n                'consecutive_errors': consecutive_errors,\n                'retry_limit': retry_limit or 'unlimited',\n                'scientific_attempt_consumed': False,\n            })\n            if retry_limit > 0 and consecutive_errors >= retry_limit:\n                ledger.update_job(\n                    job_id,\n                    status=JobStatus.FAILED,\n                    detail=f'persistent technical failure after {consecutive_errors} retries: {error_type}',\n                )\n                return\n            delay = min(max_backoff, 2.0 ** min(consecutive_errors, 8))\n            limit_label = str(retry_limit) if retry_limit > 0 else '∞'\n            ledger.update_job(\n                job_id,\n                status=JobStatus.RUNNING,\n                detail=f'technical retry {consecutive_errors}/{limit_label} after {error_type}; next in {delay:.0f}s; scientific work preserved',\n            )\n            time.sleep(delay)\n"""
    text = replace_once(text, old, new, "resilience retry block")
    write(path, text)


def patch_app() -> None:
    path = "src/awb/web/app.py"
    text = read(path)
    text = replace_once(
        text,
        "from awb.core.models import Gate, JobStatus, Task\n",
        "from awb.core.models import Gate, JobStatus, Task, TaskStatus\n",
        "app imports",
    )
    old_line = "    tasks=''.join(f\"<tr><td>{html.escape(t.id)}</td><td>{html.escape(t.title)}</td><td>{t.status.value}</td><td>{t.metadata.get('attempts',0)}</td></tr>\" for t in l.list_tasks()) or \"<tr><td colspan='4'>The Director will create the first task after launch.</td></tr>\"\n"
    new_block = '''    l.reconcile_task_counters()\n    def task_row(t):\n        scientific=int(t.metadata.get('scientific_attempts',t.metadata.get('attempts',0)))\n        technical=int(t.metadata.get('technical_failures',0))\n        recoveries=int(t.metadata.get('interrupt_recoveries',t.metadata.get('interrupted_recovery_count',0)))\n        status=t.status.value\n        if t.status==TaskStatus.BLOCKED: status='BLOCKED (review)'\n        elif t.status==TaskStatus.ERROR: status='ERROR (technical)'\n        reason=''\n        if t.status==TaskStatus.ERROR:\n            reason=str(t.metadata.get('last_error',''))\n        elif t.status==TaskStatus.BLOCKED:\n            objections=t.metadata.get('critical_objections',[])\n            reason=str(objections[0] if objections else t.metadata.get('last_verification_detail',''))\n        elif t.status==TaskStatus.REJECTED:\n            reason=str(t.metadata.get('rejection_reason',''))\n        strategy=str(t.metadata.get('next_strategy',''))\n        if len(reason)>180: reason=reason[:180]+'…'\n        if len(strategy)>180: strategy=strategy[:180]+'…'\n        return f\"<tr><td>{html.escape(t.id)}</td><td>{html.escape(t.title)}</td><td>{html.escape(status)}</td><td>{scientific}</td><td>{technical}</td><td>{recoveries}</td><td>{html.escape(reason)}</td><td>{html.escape(strategy)}</td></tr>\"\n    tasks=''.join(task_row(t) for t in l.list_tasks()) or \"<tr><td colspan='8'>The Director will create the first task after launch.</td></tr>\"\n'''
    text = replace_once(text, old_line, new_block, "task table rows")
    text = replace_once(
        text,
        "<div class='panel'><h2>Task ledger</h2><table><tr><th>ID</th><th>Task</th><th>Status</th><th>Attempts</th></tr>{tasks}</table></div>",
        "<div class='panel'><h2>Task ledger</h2><p class='muted'>Scientific attempts count only completed candidate→review cycles. Technical failures and restart recoveries are tracked separately and do not consume scientific attempts.</p><table><tr><th>ID</th><th>Task</th><th>Status</th><th>Scientific</th><th>Technical</th><th>Recoveries</th><th>Why</th><th>Next strategy</th></tr>{tasks}</table></div>",
        "task table header",
    )
    write(path, text)


def patch_live_activity() -> None:
    path = "src/awb/web/live_app.py"
    text = read(path)
    old = '''    if kind == "task_failed":\n        return "error", f"Task failed: {task}", str(payload.get("error", "Unknown task error"))\n'''
    new = '''    if kind == "task_technical_error":\n        return "error", f"Technical error: {task}", f"{payload.get('error', 'Unknown runtime error')} · scientific attempts unchanged; technical failures: {payload.get('technical_failures', '?')}."\n    if kind == "task_blocked":\n        objections = payload.get("critical_objections") or []\n        detail = str(objections[0]) if objections else "The candidate did not yet satisfy adversarial review/verification."\n        return "warn", f"Scientific review blocked: {task}", f"Scientific attempt {payload.get('scientific_attempts', '?')}: {detail}"\n    if kind == "task_recovery_planned":\n        return "info", "Director planned a different approach", str(payload.get("strategy", "Retry by explicitly resolving the objections."))[:350]\n    if kind == "task_decomposed":\n        return "info", "Blocked task decomposed", f"Created {len(payload.get('subtask_ids') or [])} prerequisite/falsification task(s). {str(payload.get('strategy', ''))[:260]}"\n    if kind == "task_reframed":\n        return "info", "Task reframed", f"The original formulation was superseded by {payload.get('replacement_task_id') or 'a replacement task'}. {str(payload.get('rationale', ''))[:260]}"\n    if kind == "task_rejected_by_evidence":\n        return "warn", "Task resolved as rejected by evidence", f"{payload.get('resolution_type', 'evidence')}: {str(payload.get('rationale', ''))[:280]}"\n    if kind == "task_reopened_after_review":\n        return "active", f"Retrying scientifically blocked task: {task}", str(payload.get("next_strategy", "Use a materially different route that addresses the objections."))[:350]\n    if kind == "task_reopened_after_technical_error":\n        return "active", f"Retrying after technical error: {task}", "The scientific attempt counter was not consumed."\n    if kind == "task_scientifically_closed":\n        return "ok", f"Task scientifically closed: {task}", f"Accepted after {payload.get('scientific_attempts', '?')} scientific attempt(s)."\n    if kind == "task_failed":\n        return "error", f"Legacy task failure: {task}", str(payload.get("error", "Unknown task error"))\n'''
    text = replace_once(text, old, new, "live activity task semantics")
    write(path, text)


def add_tests() -> None:
    path = ROOT / "tests/test_adaptive_tasking.py"
    path.write_text(r'''import json
import tempfile
import unittest
from pathlib import Path

from awb.core.models import Task, TaskStatus
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.base import ModelProvider
from awb.templates.templates import custom_manifest


class AdaptiveProvider(ModelProvider):
    def __init__(self, *, fail_worker_once=False, approvals=None, recovery_action='retry'):
        self.fail_worker_once = fail_worker_once
        self.approvals = list(approvals or [True])
        self.recovery_action = recovery_action
        self.review_calls = 0
        self.worker_prompts = []

    def generate(self, system: str, user: str) -> str:
        if 'GATE_JSON' in system:
            return json.dumps({'passed': False, 'detail': 'gate remains open in test'})
        if 'RECOVERY_JSON' in system:
            if self.recovery_action == 'reframe':
                return json.dumps({
                    'action': 'reframe',
                    'strategy': 'Replace the over-strong formulation with the strongest claim surviving the counterexample.',
                    'rationale': 'The current formulation is too strong but the core mechanism remains useful.',
                    'resolution_type': '',
                    'replacement_title': 'Prove the strongest surviving claim',
                    'replacement_description': 'Formulate and prove the strongest version consistent with the review evidence.',
                    'subtasks': [],
                })
            return json.dumps({
                'action': 'retry',
                'strategy': 'Use a counterexample-first route, then rebuild the candidate only on the surviving cases.',
                'rationale': 'The reviewer identified an untested edge case.',
                'resolution_type': '',
                'replacement_title': '',
                'replacement_description': '',
                'subtasks': [],
            })
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'Next', 'description': 'Continue', 'priority': 1})
        if 'REVIEW_JSON' in system:
            approved = self.approvals[min(self.review_calls, len(self.approvals) - 1)]
            self.review_calls += 1
            return json.dumps({
                'approved': approved,
                'critical_objections': [] if approved else ['edge case not ruled out'],
                'recommendations': [] if approved else ['try a counterexample-first analysis'],
            })
        self.worker_prompts.append(user)
        if self.fail_worker_once:
            self.fail_worker_once = False
            raise TimeoutError('simulated local model timeout')
        return 'candidate result with inspectable evidence'


class AdaptiveTaskingTests(unittest.TestCase):
    def _workspace(self, root: Path):
        manifest = custom_manifest('demo', 'Finish a rigorous result')
        manifest['runtime']['adaptive_replan_after_scientific_attempts'] = 2
        write_workspace(root, manifest)
        return load_workspace(root)

    def test_technical_failure_is_error_and_does_not_consume_scientific_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T1', title='Test claim', description='test rigorously', priority=10))
            provider = AdaptiveProvider(fail_worker_once=True, approvals=[True])
            orch = Orchestrator(ws, provider)
            with self.assertRaises(TimeoutError):
                orch.step()
            failed = ledger.get_task('T1')
            self.assertEqual(failed.status, TaskStatus.ERROR)
            self.assertEqual(failed.metadata['scientific_attempts'], 0)
            self.assertEqual(failed.metadata['technical_failures'], 1)
            self.assertEqual(failed.metadata['attempts'], 0)
            result = orch.step()
            self.assertEqual(result.task.status, TaskStatus.DONE)
            self.assertEqual(result.task.metadata['scientific_attempts'], 1)
            self.assertEqual(result.task.metadata['technical_failures'], 1)
            self.assertEqual(result.task.metadata['execution_attempts'], 2)

    def test_blocked_task_can_exceed_old_attempt_cap_without_auto_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ws.manifest.runtime.max_task_attempts = 2
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T2', title='Hard theorem', description='prove or falsify', priority=10))
            provider = AdaptiveProvider(approvals=[False], recovery_action='retry')
            orch = Orchestrator(ws, provider)
            last = None
            for _ in range(4):
                last = orch.step()
            self.assertEqual(last.task.status, TaskStatus.BLOCKED)
            self.assertEqual(last.task.metadata['scientific_attempts'], 4)
            self.assertNotEqual(last.task.status, TaskStatus.REJECTED)
            self.assertIn('counterexample-first', last.task.metadata['next_strategy'])

    def test_blocked_retry_carries_forward_review_objections_and_new_strategy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T3', title='Empirical claim', description='establish robust value', priority=10))
            provider = AdaptiveProvider(approvals=[False, True], recovery_action='retry')
            orch = Orchestrator(ws, provider)
            first = orch.step()
            self.assertEqual(first.task.status, TaskStatus.BLOCKED)
            second = orch.step()
            self.assertEqual(second.task.status, TaskStatus.DONE)
            self.assertGreaterEqual(len(provider.worker_prompts), 2)
            self.assertIn('edge case not ruled out', provider.worker_prompts[1])
            self.assertIn('counterexample-first', provider.worker_prompts[1])

    def test_reframe_is_explicit_resolution_and_creates_replacement_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T4', title='Overstrong claim', description='prove it', priority=10))
            provider = AdaptiveProvider(approvals=[False], recovery_action='reframe')
            result = Orchestrator(ws, provider).step()
            self.assertEqual(result.task.status, TaskStatus.REJECTED)
            self.assertEqual(result.task.metadata['resolution'], 'reframed')
            replacement_id = result.task.metadata['replacement_task_id']
            replacement = ledger.get_task(replacement_id)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.status, TaskStatus.OPEN)
            self.assertEqual(replacement.metadata['parent_task_id'], 'T4')

    def test_legacy_mixed_attempt_counter_is_reconciled_from_attempt_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            task = Task(id='T5', title='Legacy task', description='legacy', status=TaskStatus.BLOCKED, metadata={'attempts': 4, 'last_error': 'timeout'})
            ledger.upsert_task(task)
            a1 = ledger.start_attempt('T5', 1)
            ledger.finish_attempt(a1, status='BLOCKED', review={'approved': False})
            a2 = ledger.start_attempt('T5', 2)
            ledger.finish_attempt(a2, status='FAILED', error='timeout')
            a3 = ledger.start_attempt('T5', 3)
            ledger.finish_attempt(a3, status='TECHNICAL_ERROR', error='timeout')
            Orchestrator(ws, AdaptiveProvider())
            fixed = ledger.get_task('T5')
            self.assertEqual(fixed.metadata['scientific_attempts'], 1)
            self.assertEqual(fixed.metadata['technical_failures'], 2)
            self.assertEqual(fixed.metadata['attempts'], 1)
            self.assertEqual(fixed.status, TaskStatus.ERROR)

    def test_interrupted_attempt_is_recovery_not_scientific_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            task = Task(id='T6', title='Interrupted', description='resume', status=TaskStatus.IN_PROGRESS, metadata={'attempts': 9})
            ledger.upsert_task(task)
            ledger.start_attempt('T6', 1)
            Orchestrator(ws, AdaptiveProvider())
            recovered = ledger.get_task('T6')
            self.assertEqual(recovered.status, TaskStatus.OPEN)
            self.assertEqual(recovered.metadata['scientific_attempts'], 0)
            self.assertEqual(recovered.metadata['interrupt_recoveries'], 1)
            attempts = ledger.list_attempts('T6')
            self.assertEqual(attempts[0]['status'], 'INTERRUPTED')


if __name__ == '__main__':
    unittest.main()
''', encoding="utf-8")


def cleanup_patch_scaffold() -> None:
    for rel in [
        "scripts/apply_adaptive_tasking_patch.py",
        ".github/workflows/adaptive-tasking-patch.yml",
    ]:
        target = ROOT / rel
        if target.exists():
            target.unlink()
    scripts = ROOT / "scripts"
    if scripts.exists() and not any(scripts.iterdir()):
        scripts.rmdir()


def main() -> None:
    patch_models()
    patch_storage()
    patch_orchestrator()
    patch_templates()
    patch_resilience()
    patch_app()
    patch_live_activity()
    add_tests()
    cleanup_patch_scaffold()
    print("Adaptive task semantics patch applied.")


if __name__ == "__main__":
    main()
