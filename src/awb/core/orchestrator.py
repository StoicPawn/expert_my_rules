from __future__ import annotations
import json, os, subprocess, time, uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from awb.providers.base import ModelProvider
from awb.providers.providers import make_provider
from .git_workspace import GitWorkspaceManager
from .models import IterationResult, Review, Task, TaskStatus, WorkflowStageSpec, Workspace
from .routing import ModelRouter
from .storage import Ledger
from .tools import ToolRunner, ToolError, parse_tool_message
from .workflow import WorkflowGraph

DIRECTOR_SYSTEM="""You are the Director of an autonomous project workbench. Choose exactly ONE next task that maximally advances the north-star goal. Prefer falsification, blockers, failed gates and high-information work over cosmetics. Never redefine the goal or weaken completion criteria merely to finish. Return DIRECTOR_JSON only as JSON with: title, description, priority."""
WORKER_SYSTEM="""You are an execution agent in an autonomous project workbench. Execute the assigned task rigorously. Produce inspectable evidence. Separate evidence, assumptions, uncertainty and conclusions. When tools are available you may call them by returning only {\"tool\":\"tool_id\",\"arguments\":{...}}. Never invent tool results. For software work, inspect the actual Git status/diff and run available checks before claiming success."""
REVIEW_SYSTEM="""You are an independent adversarial Reviewer. Try to reject the candidate. Look for logical gaps, missing cases, non-reproducibility, unsafe changes, circular reasoning, goalpost shifting, regressions and unsupported claims. For software, treat the actual Git patch as primary evidence. Return REVIEW_JSON only as JSON: approved, critical_objections, recommendations."""
GATE_SYSTEM="""You are the independent completion gatekeeper. Evaluate ONE completion condition conservatively from the recorded project evidence. Never pass a gate because progress merely looks promising. Never infer missing literature checks, tests, proofs, artifacts or external verification. If evidence is insufficient, keep it open. Return GATE_JSON only as JSON with: passed (bool), detail (str)."""
RECOVERY_SYSTEM="""You are the Director repairing a scientifically BLOCKED task. A candidate was actually produced and challenged by an independent Reviewer/Verifier. Make progress without mechanically repeating the failed approach. Return RECOVERY_JSON only as JSON with: action (retry|decompose|reframe|reject), strategy (concrete materially different next approach), rationale, resolution_type (empty unless reject; one of false|ill_posed|superseded|not_required), replacement_title, replacement_description, subtasks (list of objects with title, description, priority). A retry must directly address the listed objections and must not merely ask the same question again. Decompose when prerequisite work or separate falsification checks are needed. Reframe when the original formulation should be replaced by a stronger/true/useful formulation. Reject only when evidence shows the original task/claim is false, ill-posed, superseded, or no longer required; NEVER reject merely because a numeric attempt threshold was reached. After several scientific blocks, prefer a structural change unless a genuinely new route is available."""


class Orchestrator:
    def __init__(self, workspace:Workspace, provider:ModelProvider|None=None):
        self.workspace=workspace
        self.provider_override=provider
        self.ledger=Ledger(workspace.root/'ledger.sqlite3')
        self.artifacts=workspace.root/'artifacts'
        self.artifacts.mkdir(parents=True,exist_ok=True)
        self._cloud_calls=0
        self.router=ModelRouter(workspace.manifest)
        self.workflow=WorkflowGraph(workspace.manifest)
        self.git=GitWorkspaceManager(workspace)
        self._attempt_routes=[]
        recovered=self.ledger.recover_interrupted_tasks()
        if recovered:
            self.ledger.event('interrupted_tasks_recovered',{'task_ids':recovered})
        reconciled=self.ledger.reconcile_task_counters()
        if reconciled:
            self.ledger.event('task_counters_reconciled',{'task_ids':reconciled})
        for gate in workspace.manifest.gates:
            if gate.id not in self.ledger.gate_state():
                self.ledger.set_gate(gate.id,False,'not evaluated')

    def _agent(self,role):
        return next((a for a in self.workspace.manifest.agents if a.role==role),None)

    def _instructions(self,role):
        a=self._agent(role)
        return a.instructions if a else ''

    def _should_escalate(self,role,task:Task|None):
        p=self.workspace.manifest.runtime.escalation
        if not p.enabled or p.daily_budget_eur<=0 or p.max_cloud_calls_per_run<=0 or self._cloud_calls>=p.max_cloud_calls_per_run:
            return False
        if not os.getenv('OPENAI_API_KEY'):
            return False
        if role not in p.roles or task is None:
            return False
        difficulty=max(
            int(task.metadata.get('scientific_attempts',task.metadata.get('attempts',0))),
            int(task.metadata.get('technical_failures',0)),
        )
        return difficulty>=p.after_local_failures or task.priority>=p.priority_threshold

    def _route_meta(self,role,node_id,kind,model,source):
        return {'role':role,'node':node_id,'kind':kind,'model':model,'source':source}

    def _persist_model_call(self,meta,task_id,*,success,seconds,chars=0,error=''):
        self.ledger.record_model_call(
            task_id=task_id,
            role=str(meta.get('role') or ''),
            node=str(meta.get('node') or ''),
            kind=str(meta.get('kind') or ''),
            model=meta.get('model'),
            source=str(meta.get('source') or ''),
            success=success,
            seconds=seconds,
            chars=chars,
            error=error,
        )

    def _call_model(self,role,system,user,task:Task|None=None):
        task_id=task.id if task else None
        calls=[]
        if self.provider_override is not None:
            calls=[(self.provider_override,None,self._route_meta(role,'override',type(self.provider_override).__name__,None,'override'))]
        elif self._should_escalate(role,task):
            esc=self.workspace.manifest.runtime.escalation
            self._cloud_calls+=1
            provider=make_provider(esc.cloud_provider.kind,esc.cloud_provider.model)
            meta=self._route_meta(role,'cloud-escalation',esc.cloud_provider.kind,esc.cloud_provider.model,'escalation')
            self.ledger.event('model_escalated',{'role':role,'task_id':task_id,'to':meta,'cloud_call':self._cloud_calls,'budget_eur':esc.daily_budget_eur},task_id)
            calls=[(provider,None,meta)]
        else:
            for route in self.router.candidates(role):
                # Instantiate the provider lazily inside the protected call below.
                # A broken provider configuration must be able to fail over too.
                calls.append((None,route,self._route_meta(role,route.node_id,route.kind,route.model,route.source)))
        if not calls:
            raise RuntimeError(f'No healthy model route available for role {role}')

        last_exc=None
        for index,(provider,route,meta) in enumerate(calls):
            started=time.monotonic()
            self.ledger.event('model_call_started',meta,task_id)
            try:
                if provider is None and route is not None:
                    provider=self.router.provider(route)
                context=self.router.slot(route) if route is not None else nullcontext()
                with context:
                    result=provider.generate(system,user)
            except Exception as exc:
                last_exc=exc
                elapsed=time.monotonic()-started
                error=f'{type(exc).__name__}: {exc}'
                if route is not None:
                    self.router.record_failure(route)
                self._persist_model_call(meta,task_id,success=False,seconds=elapsed,error=error)
                self._attempt_routes.append({**meta,'success':False,'seconds':round(elapsed,3),'error':error})
                self.ledger.event('model_call_failed',{**meta,'error':error,'seconds':round(elapsed,3)},task_id)
                if index+1<len(calls):
                    self.ledger.event('model_route_failover',{'role':role,'failed_node':meta['node'],'next_node':calls[index+1][2]['node']},task_id)
                    continue
                raise
            elapsed=time.monotonic()-started
            chars=len(result)
            if route is not None:
                self.router.record_success(route,seconds=elapsed,chars=chars)
            self._persist_model_call(meta,task_id,success=True,seconds=elapsed,chars=chars)
            throughput=round(chars/elapsed,1) if elapsed>0 else None
            call_meta={**meta,'success':True,'seconds':round(elapsed,3),'chars':chars,'chars_per_second':throughput}
            self.ledger.event('model_call_finished',call_meta,task_id)
            self._attempt_routes.append(call_meta)
            return result
        if last_exc:
            raise last_exc
        raise RuntimeError(f'No model route available for role {role}')

    def runtime_status(self):
        return {
            'nodes': self.router.snapshot(),
            'historical_model_stats': self.ledger.model_stats(),
        }

    def snapshot(self):
        return json.dumps({
            'goal':self.workspace.manifest.goal,
            'description':self.workspace.manifest.description,
            'tasks':[t.model_dump(mode='json') for t in self.ledger.list_tasks()][-40:],
            'gates':self.ledger.gate_state(),
            'recent_events':self.ledger.recent_events(30),
        },indent=2)

    def choose_next_task(self):
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

    def _execute_with_tools(self,task:Task,stage:WorkflowStageSpec,prompt:str,execution_root:Path):
        role=stage.role or 'worker'
        agent=self._agent(role)
        allowed=agent.tools if agent else []
        runner=ToolRunner(self.workspace,execution_root=execution_root)
        desc=runner.describe(allowed)
        system=WORKER_SYSTEM+'\n'+self._instructions(role)
        if not desc:
            return self._call_model(role,system,prompt,task)
        system+='\nAVAILABLE TOOLS:\n'+json.dumps(desc,indent=2)
        conversation=prompt
        for _ in range(self.workspace.manifest.runtime.max_tool_calls_per_task+1):
            raw=self._call_model(role,system,conversation,task)
            parsed=parse_tool_message(raw)
            if not parsed:
                return raw
            tool_id,args=parsed
            try:
                result=runner.execute(tool_id,args) if tool_id in allowed else {'ok':False,'error':f'Tool not allowed: {tool_id}'}
            except (ToolError,subprocess.TimeoutExpired,OSError) as exc:
                result={'ok':False,'error':f'{type(exc).__name__}: {exc}'}
            self.ledger.event('tool_call',{'stage':stage.id,'role':role,'tool':tool_id,'arguments':args,'result':result},task.id)
            conversation+='\nTOOL RESULT:\n'+json.dumps(result,indent=2)+'\nContinue working or return the final result.'
        return 'Tool-call budget exhausted.'

    def _review_stage(self,task:Task,stage:WorkflowStageSpec,candidate:str,patch:str):
        role=stage.role or 'reviewer'
        max_chars=max(1000,self.workspace.manifest.runtime.git.patch_context_chars)
        patch_context=patch[-max_chars:] if patch else '(no Git patch available)'
        prompt=(
            f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.model_dump_json()}\n\n'
            f'CANDIDATE RESULT:\n{candidate}\n\nACTUAL GIT PATCH:\n{patch_context}'
        )
        raw=self._call_model(role,REVIEW_SYSTEM+'\n'+self._instructions(role),prompt,task)
        try:
            return Review.model_validate(json.loads(raw))
        except Exception:
            return Review(approved=False,critical_objections=[f'{stage.id} returned invalid structured review output.'],recommendations=[raw])

    def verify(self,task:Task,validator_names:list[str]|None=None,cwd:Path|None=None):
        validators=self.workspace.manifest.validators
        if not validators:
            return True,'No external validator configured.'
        names=validator_names or list(validators)
        failures=[]
        passed=[]
        run_root=cwd or self.workspace.root
        for name in names:
            command=validators.get(name)
            if not command:
                failures.append(f'{name}: UNKNOWN VALIDATOR')
                continue
            proc=subprocess.run(command,cwd=run_root,shell=True,text=True,capture_output=True)
            detail=(proc.stdout+'\n'+proc.stderr).strip()
            if proc.returncode:
                failures.append(f'{name}: FAILED\n{detail[-4000:]}')
                self.ledger.event('validator_failed',{'name':name,'detail':detail[-1500:]},task.id)
            else:
                passed.append(name)
                self.ledger.event('validator_passed',{'name':name,'detail':detail[-1500:]},task.id)
        return (False,'\n\n'.join(failures)) if failures else (True,'Validators passed: '+', '.join(passed))

    def evaluate_gates(self):
        done=self.ledger.list_tasks([TaskStatus.DONE])
        for gate in self.workspace.manifest.gates:
            if gate.validator and gate.validator in self.workspace.manifest.validators:
                proc=subprocess.run(self.workspace.manifest.validators[gate.validator],cwd=self.workspace.root,shell=True,text=True,capture_output=True)
                self.ledger.set_gate(gate.id,proc.returncode==0,(proc.stdout+'\n'+proc.stderr).strip()[-3000:])
                continue
            if gate.manual or not done:
                continue
            prompt=f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nGATE:\n{gate.id}: {gate.description}\n\nPROJECT EVIDENCE:\n{self.snapshot()}'
            raw=self._call_model('verifier',GATE_SYSTEM+'\n'+self._instructions('verifier'),prompt)
            try:
                d=json.loads(raw)
                passed=bool(d.get('passed',False))
                detail=str(d.get('detail',''))
            except Exception:
                passed=False
                detail='Gatekeeper returned invalid structured output.'
            self.ledger.set_gate(gate.id,passed,detail)
            self.ledger.event('gate_evaluated',{'gate':gate.id,'passed':passed,'detail':detail})
        return self.ledger.gate_state()

    def is_complete(self):
        state=self.ledger.gate_state()
        required=[g for g in self.workspace.manifest.gates if g.required]
        return bool(required) and all(state.get(g.id,{}).get('passed',False) for g in required)

    def _save_patch(self,task:Task,patch:str):
        if not patch:
            return ''
        ts=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path=self.artifacts/f'{ts}_{task.id}.patch'
        path.write_text(patch,encoding='utf-8')
        return str(path.relative_to(self.workspace.root))

    def _save_artifact(self,task:Task,output:str,review:Review,reviews:dict,verification:str,patch_artifact:str,patch_summary:str):
        ts=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path=self.artifacts/f'{ts}_{task.id}.md'
        path.write_text(
            f'# {task.title}\n\n**Task:** {task.description}\n\n'
            f'## Candidate result\n\n{output}\n\n'
            f'## Adversarial review aggregate\n\nApproved: **{review.approved}**\n\n'
            f'Critical objections: {json.dumps(review.critical_objections,indent=2)}\n\n'
            f'Recommendations: {json.dumps(review.recommendations,indent=2)}\n\n'
            f'## Review stages\n\n```json\n{json.dumps(reviews,indent=2)}\n```\n\n'
            f'## External verification\n\n{verification}\n\n'
            f'## Git candidate\n\nPatch artifact: `{patch_artifact or "none"}`\n\n{patch_summary or "No Git diff."}\n',
            encoding='utf-8',
        )
        return path

    def _aggregate_reviews(self,reviews:list[tuple[WorkflowStageSpec,Review]]):
        required=[(s,r) for s,r in reviews if s.required]
        considered=required or reviews
        if not considered:
            return Review(approved=True,critical_objections=[],recommendations=[])
        approvals=[r.approved for _,r in considered]
        approved=all(approvals) if self.workflow.review_policy=='all' else any(approvals)
        objections=[]
        recommendations=[]
        for stage,review in reviews:
            objections.extend([f'[{stage.id}] {item}' for item in review.critical_objections])
            recommendations.extend([f'[{stage.id}] {item}' for item in review.recommendations])
        return Review(approved=approved,critical_objections=objections,recommendations=recommendations)

    def step(self):
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

    def run(self,max_steps=None,max_minutes=None,control=None,on_step=None):
        max_steps=max_steps or self.workspace.manifest.runtime.max_steps_per_run
        max_minutes=max_minutes if max_minutes is not None else self.workspace.manifest.runtime.max_minutes_per_run
        deadline=time.monotonic()+max_minutes*60 if max_minutes and max_minutes>0 else None
        results=[]
        self.ledger.event('run_started',{'max_steps':max_steps,'max_minutes':max_minutes})
        reason=None
        for _ in range(max_steps):
            if control:
                action=control()
                while action=='pause':
                    time.sleep(.5)
                    action=control()
                if action=='cancel':
                    reason='cancelled'
                    break
            if self.is_complete():
                reason='complete'
                break
            if deadline and time.monotonic()>=deadline:
                reason='time_budget'
                break
            result=self.step()
            results.append(result)
            if on_step:
                on_step(len(results),result)
            if self.workspace.manifest.runtime.pause_seconds>0:
                time.sleep(self.workspace.manifest.runtime.pause_seconds)
        self.ledger.event('run_finished',{'steps':len(results),'complete':self.is_complete(),'reason':reason or 'step_budget','cloud_calls':self._cloud_calls,'runtime':self.runtime_status()})
        return results
