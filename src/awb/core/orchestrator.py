from __future__ import annotations
import json, os, subprocess, time, uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from awb.providers.base import ModelProvider
from awb.providers.providers import make_provider
from .models import IterationResult, Review, Task, TaskStatus, Workspace
from .routing import ModelRouter
from .storage import Ledger
from .tools import ToolRunner, ToolError, parse_tool_message

DIRECTOR_SYSTEM="""You are the Director of an autonomous project workbench. Choose exactly ONE next task that maximally advances the north-star goal. Prefer falsification, blockers, failed gates and high-information work over cosmetics. Never redefine the goal or weaken completion criteria merely to finish. Return DIRECTOR_JSON only as JSON with: title, description, priority."""
WORKER_SYSTEM="""You are the Worker. Execute the assigned task rigorously. Produce inspectable evidence. Separate evidence, assumptions, uncertainty and conclusions. When tools are available you may call them by returning only {\"tool\":\"tool_id\",\"arguments\":{...}}. Never invent tool results."""
REVIEW_SYSTEM="""You are an independent adversarial Reviewer. Try to reject the candidate. Look for logical gaps, missing cases, non-reproducibility, unsafe changes, circular reasoning, goalpost shifting and unsupported novelty. Return REVIEW_JSON only as JSON: approved, critical_objections, recommendations."""
GATE_SYSTEM="""You are the independent completion gatekeeper. Evaluate ONE completion condition conservatively from the recorded project evidence. Never pass a gate because progress merely looks promising. Never infer missing literature checks, tests, proofs, artifacts or external verification. If evidence is insufficient, keep it open. Return GATE_JSON only as JSON with: passed (bool), detail (str)."""

class Orchestrator:
    def __init__(self, workspace:Workspace, provider:ModelProvider|None=None):
        self.workspace=workspace; self.provider_override=provider; self.ledger=Ledger(workspace.root/'ledger.sqlite3'); self.artifacts=workspace.root/'artifacts'; self.artifacts.mkdir(parents=True,exist_ok=True); self._cloud_calls=0
        self.router=ModelRouter(workspace.manifest); self._attempt_routes=[]
        recovered=self.ledger.recover_interrupted_tasks()
        if recovered: self.ledger.event('interrupted_tasks_recovered',{'task_ids':recovered})
        for gate in workspace.manifest.gates:
            if gate.id not in self.ledger.gate_state(): self.ledger.set_gate(gate.id,False,'not evaluated')
    def _agent(self,role): return next((a for a in self.workspace.manifest.agents if a.role==role),None)
    def _instructions(self,role):
        a=self._agent(role); return a.instructions if a else ''
    def _should_escalate(self,role,task:Task|None):
        p=self.workspace.manifest.runtime.escalation
        if not p.enabled or p.daily_budget_eur<=0 or p.max_cloud_calls_per_run<=0 or self._cloud_calls>=p.max_cloud_calls_per_run: return False
        if not os.getenv('OPENAI_API_KEY'): return False
        if role not in p.roles or task is None: return False
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
            esc=self.workspace.manifest.runtime.escalation; self._cloud_calls+=1
            provider=make_provider(esc.cloud_provider.kind,esc.cloud_provider.model)
            meta=self._route_meta(role,'cloud-escalation',esc.cloud_provider.kind,esc.cloud_provider.model,'escalation')
            self.ledger.event('model_escalated',{'role':role,'task_id':task_id,'to':meta,'cloud_call':self._cloud_calls,'budget_eur':esc.daily_budget_eur},task_id)
            calls=[(provider,None,meta)]
        else:
            for route in self.router.candidates(role):
                calls.append((self.router.provider(route),route,self._route_meta(role,route.node_id,route.kind,route.model,route.source)))
        last_exc=None
        for index,(provider,route,meta) in enumerate(calls):
            started=time.monotonic(); self.ledger.event('model_call_started',meta,task_id)
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
        if last_exc: raise last_exc
        raise RuntimeError(f'No model route available for role {role}')
    def snapshot(self):
        return json.dumps({'goal':self.workspace.manifest.goal,'description':self.workspace.manifest.description,'tasks':[t.model_dump(mode='json') for t in self.ledger.list_tasks()][-40:],'gates':self.ledger.gate_state(),'recent_events':self.ledger.recent_events(30)},indent=2)
    def choose_next_task(self):
        existing=self.ledger.list_tasks([TaskStatus.OPEN])
        if existing: return existing[0]
        context=self.snapshot(); blocked=self.ledger.list_tasks([TaskStatus.BLOCKED])
        if blocked: context+='\nBLOCKED TASKS REQUIRE REMEDIATION:\n'+json.dumps([t.model_dump(mode='json') for t in blocked[:10]],indent=2)
        raw=self._call_model('director',DIRECTOR_SYSTEM+'\n'+self._instructions('director'),context)
        try:d=json.loads(raw)
        except json.JSONDecodeError:d={'title':'Resolve next project gap','description':raw,'priority':1.0}
        task=Task(id=f'TASK-{uuid.uuid4().hex[:8].upper()}',title=str(d.get('title','Next task')),description=str(d.get('description','Advance the project goal.')),priority=float(d.get('priority',1.0)),created_by='director'); self.ledger.upsert_task(task); self.ledger.event('task_created',task.model_dump(mode='json'),task.id); return task
    def _worker_with_tools(self,task,system,prompt):
        agent=self._agent('worker'); allowed=agent.tools if agent else []; runner=ToolRunner(self.workspace); desc=runner.describe(allowed)
        if not desc: return self._call_model('worker',system,prompt,task)
        system+='\nAVAILABLE TOOLS:\n'+json.dumps(desc,indent=2); conversation=prompt
        for _ in range(self.workspace.manifest.runtime.max_tool_calls_per_task+1):
            raw=self._call_model('worker',system,conversation,task); parsed=parse_tool_message(raw)
            if not parsed:return raw
            tool_id,args=parsed
            try: result=runner.execute(tool_id,args) if tool_id in allowed else {'ok':False,'error':f'Tool not allowed: {tool_id}'}
            except (ToolError,subprocess.TimeoutExpired,OSError) as exc: result={'ok':False,'error':f'{type(exc).__name__}: {exc}'}
            self.ledger.event('tool_call',{'tool':tool_id,'arguments':args,'result':result},task.id); conversation+='\nTOOL RESULT:\n'+json.dumps(result,indent=2)+'\nContinue or return final result.'
        return 'Tool-call budget exhausted.'
    def verify(self,task,work_output):
        if not self.workspace.manifest.validators:return True,'No external validator configured.'
        failures=[]; passed=[]
        for name,command in self.workspace.manifest.validators.items():
            proc=subprocess.run(command,cwd=self.workspace.root,shell=True,text=True,capture_output=True); detail=(proc.stdout+'\n'+proc.stderr).strip()
            if proc.returncode: failures.append(f'{name}: FAILED\n{detail[-4000:]}')
            else: passed.append(name); self.ledger.event('validator_passed',{'name':name,'detail':detail[-1500:]},task.id)
        return (False,'\n\n'.join(failures)) if failures else (True,'Validators passed: '+', '.join(passed))
    def evaluate_gates(self):
        done=self.ledger.list_tasks([TaskStatus.DONE]); state=self.ledger.gate_state()
        for gate in self.workspace.manifest.gates:
            if gate.validator and gate.validator in self.workspace.manifest.validators:
                proc=subprocess.run(self.workspace.manifest.validators[gate.validator],cwd=self.workspace.root,shell=True,text=True,capture_output=True); self.ledger.set_gate(gate.id,proc.returncode==0,(proc.stdout+'\n'+proc.stderr).strip()[-3000:]); continue
            if gate.manual or not done: continue
            prompt=f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nGATE:\n{gate.id}: {gate.description}\n\nPROJECT EVIDENCE:\n{self.snapshot()}'
            raw=self._call_model('verifier',GATE_SYSTEM+'\n'+self._instructions('verifier'),prompt)
            try:
                d=json.loads(raw); passed=bool(d.get('passed',False)); detail=str(d.get('detail',''))
            except Exception:
                passed=False; detail='Gatekeeper returned invalid structured output.'
            self.ledger.set_gate(gate.id,passed,detail); self.ledger.event('gate_evaluated',{'gate':gate.id,'passed':passed,'detail':detail})
        return self.ledger.gate_state()
    def is_complete(self):
        state=self.ledger.gate_state(); required=[g for g in self.workspace.manifest.gates if g.required]; return bool(required) and all(state.get(g.id,{}).get('passed',False) for g in required)
    def _save_artifact(self,task,output,review,verification):
        ts=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); path=self.artifacts/f'{ts}_{task.id}.md'; path.write_text(f'# {task.title}\n\n**Task:** {task.description}\n\n## Candidate result\n\n{output}\n\n## Adversarial review\n\nApproved: **{review.approved}**\n\nCritical objections: {json.dumps(review.critical_objections,indent=2)}\n\nRecommendations: {json.dumps(review.recommendations,indent=2)}\n\n## External verification\n\n{verification}\n'); return path
    def step(self):
        task=self.choose_next_task(); attempts=int(task.metadata.get('attempts',0))+1; task.metadata['attempts']=attempts; task.status=TaskStatus.IN_PROGRESS; self.ledger.upsert_task(task); self.ledger.event('task_started',{'attempt':attempts},task.id)
        attempt_id=self.ledger.start_attempt(task.id,attempts); self._attempt_routes=[]
        try:
            output=self._worker_with_tools(task,WORKER_SYSTEM+'\n'+self._instructions('worker'),f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.title}\n{task.description}\n\nSTATE:\n{self.snapshot()}'); self.ledger.event('work_output',{'text':output},task.id)
            raw=self._call_model('reviewer',REVIEW_SYSTEM+'\n'+self._instructions('reviewer'),f'NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.model_dump_json()}\n\nCANDIDATE RESULT:\n{output}',task)
            try: review=Review.model_validate(json.loads(raw))
            except Exception: review=Review(approved=False,critical_objections=['Reviewer returned invalid structured output.'],recommendations=[raw])
            self.ledger.event('review',review.model_dump(mode='json'),task.id); verified,detail=self.verify(task,output); self.ledger.event('verification',{'passed':verified,'detail':detail},task.id)
            if review.approved and verified: task.status=TaskStatus.DONE; task.metadata.pop('critical_objections',None)
            elif attempts>=self.workspace.manifest.runtime.max_task_attempts: task.status=TaskStatus.REJECTED; task.metadata['critical_objections']=review.critical_objections
            else: task.status=TaskStatus.BLOCKED; task.metadata['critical_objections']=review.critical_objections or [detail]
            artifact=self._save_artifact(task,output,review,detail); task.metadata['artifact']=str(artifact.relative_to(self.workspace.root)); self.ledger.upsert_task(task); self.evaluate_gates()
            self.ledger.finish_attempt(attempt_id,status=task.status.value,route={'calls':self._attempt_routes},review=review.model_dump(mode='json'),verification={'passed':verified,'detail':detail},artifact=task.metadata['artifact'])
            return IterationResult(task=task,work_output=output,review=review,verification_passed=verified,verification_detail=detail,next_task=None if self.is_complete() else self.choose_next_task())
        except Exception as exc:
            task.status=TaskStatus.BLOCKED; task.metadata['last_error']=f'{type(exc).__name__}: {exc}'; self.ledger.upsert_task(task); self.ledger.event('task_failed',{'error':task.metadata['last_error']},task.id)
            self.ledger.finish_attempt(attempt_id,status='FAILED',route={'calls':self._attempt_routes},error=task.metadata['last_error'])
            raise
    def run(self,max_steps=None,max_minutes=None,control=None,on_step=None):
        max_steps=max_steps or self.workspace.manifest.runtime.max_steps_per_run; max_minutes=max_minutes if max_minutes is not None else self.workspace.manifest.runtime.max_minutes_per_run; deadline=time.monotonic()+max_minutes*60 if max_minutes and max_minutes>0 else None; results=[]; self.ledger.event('run_started',{'max_steps':max_steps,'max_minutes':max_minutes}); reason=None
        for _ in range(max_steps):
            if control:
                action=control()
                while action=='pause': time.sleep(.5); action=control()
                if action=='cancel': reason='cancelled'; break
            if self.is_complete(): reason='complete'; break
            if deadline and time.monotonic()>=deadline: reason='time_budget'; break
            result=self.step(); results.append(result)
            if on_step:on_step(len(results),result)
            if self.workspace.manifest.runtime.pause_seconds>0: time.sleep(self.workspace.manifest.runtime.pause_seconds)
        self.ledger.event('run_finished',{'steps':len(results),'complete':self.is_complete(),'reason':reason or 'step_budget','cloud_calls':self._cloud_calls}); return results
