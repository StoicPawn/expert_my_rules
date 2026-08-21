from __future__ import annotations

import argparse
import json
from pathlib import Path

from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.providers import make_provider
from awb.templates.templates import get_template


def cmd_init(args):
    root = Path(args.path or f"workspaces/{args.name}")
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"Workspace already exists and is not empty: {root}")
    write_workspace(root, get_template(args.kind, args.name, args.goal))
    print(root.resolve())


def cmd_run(args):
    ws = load_workspace(Path(args.workspace))
    provider = make_provider(args.provider, args.model)
    orch = Orchestrator(ws, provider)
    results = orch.run(args.max_steps)
    print(json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str))
    print("COMPLETE=" + str(orch.is_complete()))


def cmd_status(args):
    ws = load_workspace(Path(args.workspace))
    ledger = Ledger(ws.root / "ledger.sqlite3")
    print(json.dumps({"name": ws.manifest.name, "goal": ws.manifest.goal, "tasks": [t.model_dump(mode="json") for t in ledger.list_tasks()], "gates": ledger.gate_state(), "events": ledger.recent_events(20)}, indent=2, default=str))


def cmd_serve(args):
    import uvicorn
    uvicorn.run("awb.web.app:app", host=args.host, port=args.port, reload=False)


def build_parser():
    p = argparse.ArgumentParser(prog="awb"); sub = p.add_subparsers(required=True)
    x=sub.add_parser("init"); x.add_argument("kind",choices=["research","software"]); x.add_argument("name"); x.add_argument("--goal",required=True); x.add_argument("--path"); x.set_defaults(func=cmd_init)
    x=sub.add_parser("run"); x.add_argument("workspace"); x.add_argument("--provider",choices=["mock","ollama","openai"],default="mock"); x.add_argument("--model"); x.add_argument("--max-steps",type=int,default=1); x.set_defaults(func=cmd_run)
    x=sub.add_parser("status"); x.add_argument("workspace"); x.set_defaults(func=cmd_status)
    x=sub.add_parser("serve"); x.add_argument("--host",default="127.0.0.1"); x.add_argument("--port",type=int,default=8000); x.set_defaults(func=cmd_serve)
    return p


def main():
    args=build_parser().parse_args(); args.func(args)

if __name__ == "__main__": main()
