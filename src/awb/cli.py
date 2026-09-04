from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from awb.core.benchmark import compare_reports, run_builtin_suite
from awb.core.models import JobStatus, Task
from awb.core.orchestrator import Orchestrator
from awb.core.planner import propose_manifest
from awb.core.routing import ModelRouter
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.providers.providers import make_provider
from awb.templates.templates import get_template


def cmd_create(args):
    manifest = propose_manifest(args.goal, args.name, use_local_ai=not args.no_ai_plan)
    name = manifest['name']
    root = Path(args.path or f'workspaces/{name}')
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f'Workspace exists: {root}')
    write_workspace(root, manifest)
    print(json.dumps({
        'workspace': str(root.resolve()),
        'type': manifest['type'],
        'goal': manifest['goal'],
        'gates': manifest['gates'],
        'agents': manifest['agents'],
    }, indent=2))


def cmd_init(args):
    root = Path(args.path or f'workspaces/{args.name}')
    manifest = get_template(args.kind, args.name, args.goal)
    if args.provider:
        manifest['runtime']['default_provider'] = {'kind': args.provider, 'model': args.model}
    write_workspace(root, manifest)
    print(root.resolve())


def _provider_override(args):
    return make_provider(args.provider, getattr(args, 'model', None)) if getattr(args, 'provider', None) else None


def cmd_run(args):
    ws = load_workspace(Path(args.workspace))
    orch = Orchestrator(ws, _provider_override(args))
    results = orch.run(args.max_steps, args.max_minutes)
    print(json.dumps([
        {
            'task': r.task.id,
            'status': r.task.status.value,
            'approved': r.review.approved,
            'verified': r.verification_passed,
        }
        for r in results
    ], indent=2))
    print('COMPLETE=' + str(orch.is_complete()))


def _continuous(ws, job_id, orch, ledger):
    session_steps = ws.manifest.runtime.continuous_session_steps
    session_minutes = ws.manifest.runtime.continuous_session_minutes

    def control():
        cur = ledger.get_job(job_id)
        s = cur['status']
        return 'pause' if s == JobStatus.PAUSED.value else 'cancel' if s == JobStatus.CANCEL_REQUESTED.value else 'run'

    while True:
        cur = ledger.get_job(job_id)
        if cur['status'] == JobStatus.CANCEL_REQUESTED.value:
            ledger.update_job(job_id, status=JobStatus.CANCELLED, detail='cancelled by user')
            return
        if orch.is_complete():
            ledger.update_job(job_id, status=JobStatus.COMPLETE, detail='all required gates passed')
            return
        before = cur['steps_done']

        def on_step(count, _):
            ledger.update_job(job_id, steps_done=before + count, detail='continuous project running')

        orch.run(session_steps, session_minutes, control=control, on_step=on_step)
        if orch.is_complete():
            ledger.update_job(job_id, status=JobStatus.COMPLETE, detail='all required gates passed')
            return
        time.sleep(ws.manifest.runtime.checkpoint_pause_seconds)


def cmd_launch(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / 'ledger.sqlite3')
    orch = Orchestrator(ws, _provider_override(args))
    jid = ledger.create_job(0, 0, continuous=True)
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='continuous project active')
    try:
        _continuous(ws, jid, orch, ledger)
    except KeyboardInterrupt:
        ledger.update_job(jid, status=JobStatus.PAUSED, detail='paused by keyboard interrupt')
    print(json.dumps(ledger.get_job(jid), indent=2))


def cmd_status(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / 'ledger.sqlite3')
    router = ModelRouter(ws.manifest)
    print(json.dumps({
        'name': ws.manifest.name,
        'goal': ws.manifest.goal,
        'provider': ws.manifest.runtime.default_provider.model_dump(),
        'scheduler': ws.manifest.runtime.scheduler.model_dump(),
        'compute_nodes': router.snapshot(),
        'historical_model_stats': ledger.model_stats(),
        'escalation': ws.manifest.runtime.escalation.model_dump(),
        'tasks': [t.model_dump(mode='json') for t in ledger.list_tasks()],
        'gates': ledger.gate_state(),
        'jobs': ledger.list_jobs(10),
    }, indent=2, default=str))


def cmd_task_add(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / 'ledger.sqlite3')
    task = Task(
        id=args.id or f'USER-{len(ledger.list_tasks()) + 1:04d}',
        title=args.title,
        description=args.description or args.title,
        priority=args.priority,
        created_by='user',
    )
    ledger.upsert_task(task)
    ledger.event('task_created', task.model_dump(mode='json'), task.id)
    print(task.id)


def cmd_gate(args):
    ws = load_workspace(Path(args.workspace))
    Ledger(ws.root / 'ledger.sqlite3').set_gate(args.gate_id, args.state == 'pass', args.detail or 'set by user')


def cmd_job(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / 'ledger.sqlite3')
    job = ledger.get_job(args.job_id) if args.job_id else ledger.latest_job()
    if not job:
        raise SystemExit('No job found')
    if args.action == 'status':
        print(json.dumps(job, indent=2))
        return
    mapping = {'pause': JobStatus.PAUSED, 'resume': JobStatus.RUNNING, 'cancel': JobStatus.CANCEL_REQUESTED}
    ledger.update_job(job['id'], status=mapping[args.action], detail=f'{args.action} requested')
    print(json.dumps(ledger.get_job(job['id']), indent=2))


def cmd_cloud(args):
    ws = load_workspace(Path(args.workspace))
    policy = ws.manifest.runtime.escalation
    policy.enabled = args.enabled
    policy.daily_budget_eur = args.daily_budget
    policy.max_cloud_calls_per_run = args.max_calls
    policy.cloud_provider.kind = 'openai'
    policy.cloud_provider.model = args.model
    save_manifest(ws)
    print(json.dumps(policy.model_dump(), indent=2))


def cmd_benchmark(args):
    if args.output:
        output = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = Path('benchmarks') / stamp
    report = run_builtin_suite(
        output,
        max_attempts=args.max_attempts,
        provider=_provider_override(args),
    )
    print(json.dumps(report, indent=2))
    print(f'BENCHMARK_REPORT={output.resolve() / "benchmark.json"}')


def cmd_benchmark_compare(args):
    rows = compare_reports([Path(p) for p in args.reports])
    print(json.dumps(rows, indent=2))


def cmd_serve(args):
    import os
    import uvicorn

    os.environ['AWB_WORKSPACES_DIR'] = str(Path(args.workspaces).resolve())
    uvicorn.run('awb.web.live_app:app', host=args.host, port=args.port, reload=False)


def build_parser():
    parser = argparse.ArgumentParser(prog='awb', description='Expert My Rules autonomous workbench')
    sub = parser.add_subparsers(required=True)

    x = sub.add_parser('create', help='Goal-first workspace creation')
    x.add_argument('--goal', required=True)
    x.add_argument('--name')
    x.add_argument('--path')
    x.add_argument('--no-ai-plan', action='store_true')
    x.set_defaults(func=cmd_create)

    x = sub.add_parser('init', help='Advanced template creation')
    x.add_argument('kind', choices=['research', 'software', 'custom'])
    x.add_argument('name')
    x.add_argument('--goal', required=True)
    x.add_argument('--path')
    x.add_argument('--provider', choices=['mock', 'ollama', 'openai'])
    x.add_argument('--model')
    x.set_defaults(func=cmd_init)

    x = sub.add_parser('run')
    x.add_argument('workspace')
    x.add_argument('--provider', choices=['mock', 'ollama', 'openai'])
    x.add_argument('--model')
    x.add_argument('--max-steps', type=int)
    x.add_argument('--max-minutes', type=float)
    x.set_defaults(func=cmd_run)

    x = sub.add_parser('launch', help='Continue autonomously until completion gates pass')
    x.add_argument('workspace')
    x.add_argument('--provider', choices=['mock', 'ollama', 'openai'])
    x.add_argument('--model')
    x.set_defaults(func=cmd_launch)

    x = sub.add_parser('status')
    x.add_argument('workspace')
    x.set_defaults(func=cmd_status)

    x = sub.add_parser('task-add')
    x.add_argument('workspace')
    x.add_argument('title')
    x.add_argument('--description')
    x.add_argument('--priority', type=float, default=10)
    x.add_argument('--id')
    x.set_defaults(func=cmd_task_add)

    x = sub.add_parser('gate')
    x.add_argument('workspace')
    x.add_argument('gate_id')
    x.add_argument('state', choices=['pass', 'open'])
    x.add_argument('--detail')
    x.set_defaults(func=cmd_gate)

    x = sub.add_parser('job')
    x.add_argument('workspace')
    x.add_argument('action', choices=['status', 'pause', 'resume', 'cancel'])
    x.add_argument('--job-id')
    x.set_defaults(func=cmd_job)

    x = sub.add_parser('cloud', help='Configure optional budget-gated cloud escalation')
    x.add_argument('workspace')
    x.add_argument('--enabled', action=argparse.BooleanOptionalAction, default=False)
    x.add_argument('--daily-budget', type=float, default=0)
    x.add_argument('--max-calls', type=int, default=0)
    x.add_argument('--model', default='gpt-5')
    x.set_defaults(func=cmd_cloud)

    x = sub.add_parser('benchmark', help='Run repeatable local coding tasks and save a hardware/model report')
    x.add_argument('--output')
    x.add_argument('--max-attempts', type=int, default=3)
    x.add_argument('--provider', choices=['mock', 'ollama', 'openai'])
    x.add_argument('--model')
    x.set_defaults(func=cmd_benchmark)

    x = sub.add_parser('benchmark-compare', help='Compare saved benchmark.json reports from different machines/models')
    x.add_argument('reports', nargs='+')
    x.set_defaults(func=cmd_benchmark_compare)

    x = sub.add_parser('serve')
    x.add_argument('--host', default='127.0.0.1')
    x.add_argument('--port', type=int, default=8000)
    x.add_argument('--workspaces', default='workspaces')
    x.set_defaults(func=cmd_serve)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
