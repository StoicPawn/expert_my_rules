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
        attempts=int(task.metadata.get('attempts',0))
        return attempts>=p.after_local_failures or task.priority>=p.priority_threshold

    def _route_meta(self,role,node_id,kind,model,source):
        return {'role':role,'node':node_id,'kind':kind,'model':model,'source':source}

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
                calls.append((self.router.provider(route),route,self._route_meta(role,route.node_id,route.kind,route.model,route.source)))
        last_exc=None
        for index,(provider,route,meta) in enumerate(calls):
            started=time.monotonic()
            self.ledger.event('model_call_started',meta,task_id)
            try:
                context=self.router.slot(route) if route is not None else nullcontext()
                with context:
                    result=provider.generate(system,user)
            except Exception as exc:
                last_exc=exc
                self.ledger.event('model_call_failed',{**meta,'error':f'{type(exc).__name__}: {exc}','seconds':round(time.monotonic()-started,3)},task_id)
                if index+1<len(calls):
                    self.ledger.event('model_route_failover',{'role':role,'failed_node':meta['node'],'next_node':calls[index+1][2]['node']},task_id)
                    continue
                raise
            self.ledger.event('model_call_finished',{**meta,'seconds':round(time.monotonic()-started,3),'chars':len(result)},task_id)
            self._attempt_routes.append(meta)
            return result
        if last_exc:
            raise last_exc
        raise RuntimeError(f'No model route available for role {role}')

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
        blocked=self.ledger.list_tasks([TaskStatus.BLOCKED])
        retryable=[t for t in blocked if int(t.metadata.get('attempts',0))<self.workspace.manifest.runtime.max_task_attempts]
        if retryable:
            task=retryable[0]
            task.status=TaskStatus.OPEN
            self.ledger.upsert_task(task)
            self.ledger.event('task_reopened_for_retry',{'attempts':task.metadata.get('attempts',0)},task.id)
            return task
        selector=self.workflow.first('select_task')
        role=selector.role if selector and selector.role else 'director'
        context=self.snapshot()
        if blocked:
            context+='\nBLOCKED/EXHAUSTED TASKS:\n'+json.dumps([t.model_dump(mode='json') for t in blocked[:10]],indent=2)
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
        attempts=int(task.metadata.get('attempts',0))+1
        task.metadata['attempts']=attempts
        task.status=TaskStatus.IN_PROGRESS
        self.ledger.upsert_task(task)
        self.ledger.event('task_started',{'attempt':attempts},task.id)
        attempt_id=self.ledger.start_attempt(task.id,attempts)
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

            patch=self.git.patch(task.id) if self.git.enabled else ''
            patch_summary=self.git.diff_summary(task.id) if self.git.enabled else ''
            patch_artifact=self._save_patch(task,patch)

            if aggregate_review.approved and verified:
                task.status=TaskStatus.DONE
                task.metadata.pop('critical_objections',None)
            elif attempts>=self.workspace.manifest.runtime.max_task_attempts:
                task.status=TaskStatus.REJECTED
                task.metadata['critical_objections']=aggregate_review.critical_objections or [verification_detail]
            else:
                task.status=TaskStatus.BLOCKED
                task.metadata['critical_objections']=aggregate_review.critical_objections or [verification_detail]

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
                self.ledger.event('git_candidate_discarded',{'reason':'max attempts exhausted'},task.id)
            elif self.git.enabled and task.status==TaskStatus.BLOCKED:
                self.ledger.event('git_candidate_preserved',{'reason':'retryable rejection'},task.id)

            self.ledger.upsert_task(task)
            self.evaluate_gates()
            self.ledger.finish_attempt(
                attempt_id,
                status=task.status.value,
                route={'calls':self._attempt_routes},
                review={'aggregate':aggregate_review.model_dump(mode='json'),'stages':review_payload},
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
            task.status=TaskStatus.BLOCKED
            task.metadata['last_error']=f'{type(exc).__name__}: {exc}'
            self.ledger.upsert_task(task)
            self.ledger.event('task_failed',{'error':task.metadata['last_error']},task.id)
            self.ledger.finish_attempt(attempt_id,status='FAILED',route={'calls':self._attempt_routes},error=task.metadata['last_error'])
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
        self.ledger.event('run_finished',{'steps':len(results),'complete':self.is_complete(),'reason':reason or 'step_budget','cloud_calls':self._cloud_calls})
        return results
