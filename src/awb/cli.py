from __future__ import annotations

import argparse
import json
from pathlib import Path

from awb.core.models import Task
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.providers.providers import make_provider
from awb.templates.templates import get_template


def cmd_init(args):
    root = Path(args.path or f"workspaces/{args.name}")
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"Workspace already exists and is not empty: {root}")
    manifest = get_template(args.kind, args.name, args.goal)
    if args.provider:
        manifest["runtime"]["default_provider"] = {"kind": args.provider, "model": args.model}
    write_workspace(root, manifest)
    print(root.resolve())


def _provider_override(args):
    if getattr(args, "provider", None):
        return make_provider(args.provider, getattr(args, "model", None))
    return None


def cmd_run(args):
    ws = load_workspace(Path(args.workspace))
    orch = Orchestrator(ws, _provider_override(args))
    results = orch.run(args.max_steps, args.max_minutes)
    summary = [{
        "task": r.task.id,
        "title": r.task.title,
        "status": r.task.status.value,
        "approved": r.review.approved,
        "verified": r.verification_passed,
        "artifact": r.task.metadata.get("artifact"),
    } for r in results]
    print(json.dumps(summary, indent=2))
    print("COMPLETE=" + str(orch.is_complete()))


def cmd_overnight(args):
    ws = load_workspace(Path(args.workspace))
    orch = Orchestrator(ws, _provider_override(args))
    max_steps = args.max_steps or max(ws.manifest.runtime.max_steps_per_run, 100)
    results = orch.run(max_steps=max_steps, max_minutes=args.hours * 60)
    print(json.dumps({
        "workspace": ws.manifest.name,
        "steps_completed": len(results),
        "complete": orch.is_complete(),
        "last_task": results[-1].task.id if results else None,
    }, indent=2))


def cmd_status(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / "ledger.sqlite3")
    print(json.dumps({
        "name": ws.manifest.name,
        "type": ws.manifest.type,
        "goal": ws.manifest.goal,
        "provider": ws.manifest.runtime.default_provider.model_dump(),
        "tasks": [t.model_dump(mode="json") for t in ledger.list_tasks()],
        "gates": ledger.gate_state(),
        "events": ledger.recent_events(20),
    }, indent=2, default=str))


def cmd_task_add(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / "ledger.sqlite3")
    task = Task(
        id=args.id or f"USER-{len(ledger.list_tasks()) + 1:04d}",
        title=args.title,
        description=args.description or args.title,
        priority=args.priority,
        created_by="user",
    )
    ledger.upsert_task(task)
    ledger.event("task_created", task.model_dump(mode="json"), task.id)
    print(task.id)


def cmd_gate(args):
    ws = load_workspace(Path(args.workspace))
    gate_ids = {g.id for g in ws.manifest.gates}
    if args.gate_id not in gate_ids:
        raise SystemExit(f"Unknown gate: {args.gate_id}")
    Ledger(ws.root / "ledger.sqlite3").set_gate(args.gate_id, args.state == "pass", args.detail or "set by user")
    print(f"{args.gate_id}={args.state.upper()}")


def cmd_provider(args):
    ws = load_workspace(Path(args.workspace))
    ws.manifest.runtime.default_provider.kind = args.provider
    ws.manifest.runtime.default_provider.model = args.model
    save_manifest(ws)
    print(json.dumps(ws.manifest.runtime.default_provider.model_dump(), indent=2))


def cmd_serve(args):
    import os
    import uvicorn
    os.environ["AWB_WORKSPACES_DIR"] = str(Path(args.workspaces).resolve())
    uvicorn.run("awb.web.app:app", host=args.host, port=args.port, reload=False)


def build_parser():
    p = argparse.ArgumentParser(prog="awb", description="Expert My Rules autonomous workbench")
    sub = p.add_subparsers(required=True)

    x = sub.add_parser("init", help="Create a workspace")
    x.add_argument("kind", choices=["research", "software", "custom"])
    x.add_argument("name")
    x.add_argument("--goal", required=True)
    x.add_argument("--path")
    x.add_argument("--provider", choices=["mock", "ollama", "openai"])
    x.add_argument("--model")
    x.set_defaults(func=cmd_init)

    x = sub.add_parser("run", help="Run a bounded autonomous session")
    x.add_argument("workspace")
    x.add_argument("--provider", choices=["mock", "ollama", "openai"])
    x.add_argument("--model")
    x.add_argument("--max-steps", type=int)
    x.add_argument("--max-minutes", type=float)
    x.set_defaults(func=cmd_run)

    x = sub.add_parser("overnight", help="Run for a bounded number of hours, stopping early if all gates pass")
    x.add_argument("workspace")
    x.add_argument("--hours", type=float, default=8.0)
    x.add_argument("--max-steps", type=int)
    x.add_argument("--provider", choices=["mock", "ollama", "openai"])
    x.add_argument("--model")
    x.set_defaults(func=cmd_overnight)

    x = sub.add_parser("status")
    x.add_argument("workspace")
    x.set_defaults(func=cmd_status)

    x = sub.add_parser("task-add", help="Inject a human-defined task")
    x.add_argument("workspace")
    x.add_argument("title")
    x.add_argument("--description")
    x.add_argument("--priority", type=float, default=10.0)
    x.add_argument("--id")
    x.set_defaults(func=cmd_task_add)

    x = sub.add_parser("gate", help="Pass or reopen a manual completion gate")
    x.add_argument("workspace")
    x.add_argument("gate_id")
    x.add_argument("state", choices=["pass", "open"])
    x.add_argument("--detail")
    x.set_defaults(func=cmd_gate)

    x = sub.add_parser("provider", help="Set the default model provider stored in a workspace")
    x.add_argument("workspace")
    x.add_argument("provider", choices=["mock", "ollama", "openai"])
    x.add_argument("--model")
    x.set_defaults(func=cmd_provider)

    x = sub.add_parser("serve")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8000)
    x.add_argument("--workspaces", default="workspaces")
    x.set_defaults(func=cmd_serve)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
