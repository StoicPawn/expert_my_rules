from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.models import JobStatus, Task
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.templates.templates import get_template
from awb.web.app import app, base_dir, slug

MAX_PACK_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 250
BOOTSTRAP_NAME = "PROJECT_BOOTSTRAP.json"


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe archive path: {name}")
    return path


def _read_bootstrap(zf: zipfile.ZipFile) -> dict:
    try:
        raw = zf.read(BOOTSTRAP_NAME)
    except KeyError as exc:
        raise ValueError(f"missing {BOOTSTRAP_NAME}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid {BOOTSTRAP_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{BOOTSTRAP_NAME} must contain a JSON object")
    if str(data.get("kind", "research")) != "research":
        raise ValueError("private-pack import currently accepts research projects only")
    if not str(data.get("goal") or "").strip():
        raise ValueError("bootstrap goal is required")
    return data


def _validate_archive(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = [item for item in zf.infolist() if not item.is_dir()]
    if len(members) > MAX_MEMBERS:
        raise ValueError(f"too many files in archive ({len(members)} > {MAX_MEMBERS})")
    total = 0
    for item in members:
        _safe_member(item.filename)
        mode = (item.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"symlinks are not allowed in private packs: {item.filename}")
        if item.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"archive member too large: {item.filename}")
        total += item.file_size
        if total > MAX_PACK_BYTES:
            raise ValueError("archive expands beyond the private-pack size limit")
    return members


def _apply_bootstrap(manifest: dict, bootstrap: dict) -> dict:
    manifest["description"] = str(
        bootstrap.get("description") or "Private local research workspace imported from a project pack."
    )
    gates = bootstrap.get("gates")
    if isinstance(gates, list) and gates:
        manifest["gates"] = gates

    instructions = bootstrap.get("agent_instructions") or {}
    if isinstance(instructions, dict):
        for agent in manifest.get("agents", []):
            override = instructions.get(agent.get("id")) or instructions.get(agent.get("role"))
            if override:
                agent["instructions"] = str(override)

    return manifest


def import_private_pack(
    raw: bytes,
    *,
    workspace_base: Path | None = None,
    start_callback: Callable[[Path, str], None] | None = None,
) -> dict:
    if not raw:
        raise ValueError("empty private pack")
    if len(raw) > MAX_PACK_BYTES:
        raise ValueError("private pack exceeds size limit")

    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        members = _validate_archive(zf)
        bootstrap = _read_bootstrap(zf)

        requested_name = str(
            bootstrap.get("project_key") or bootstrap.get("project_name") or "private-research"
        ).strip()
        safe_name = slug(requested_name)
        if not safe_name:
            raise ValueError("project_key/project_name does not produce a safe workspace name")

        root = (workspace_base or base_dir()) / safe_name
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"workspace already exists: {safe_name}")

        goal = str(bootstrap["goal"]).strip()
        manifest = _apply_bootstrap(get_template("research", safe_name, goal), bootstrap)
        write_workspace(root, manifest)

        private_dir = root / "private"
        private_dir.mkdir(parents=True, exist_ok=True)
        try:
            for item in members:
                rel = _safe_member(item.filename)
                target = private_dir.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            (private_dir / "LOCAL_ONLY.txt").write_text(
                "This directory contains private project material. Do not publish it to platform repositories.\n",
                encoding="utf-8",
            )

            ws = load_workspace(root)
            ledger = Ledger(root / "ledger.sqlite3")
            for gate in ws.manifest.gates:
                ledger.set_gate(gate.id, False, "not evaluated; imported private project")

            for index, seed in enumerate(bootstrap.get("seed_tasks") or [], start=1):
                if not isinstance(seed, dict) or not str(seed.get("title") or "").strip():
                    continue
                task = Task(
                    id=f"SEED-{index:03d}",
                    title=str(seed["title"]).strip(),
                    description=str(seed.get("description") or seed["title"]).strip(),
                    priority=float(seed.get("priority", 10)),
                    created_by="private-pack",
                )
                ledger.upsert_task(task)
                ledger.event("task_created", task.model_dump(mode="json"), task.id)

            job_id = None
            if bool(bootstrap.get("auto_launch", False)) and start_callback is not None:
                job_id = ledger.create_job(0, 0, continuous=True)
                ledger.update_job(job_id, status=JobStatus.RUNNING, detail="private autonomous project active")
                start_callback(root, job_id)

            return {
                "workspace": safe_name,
                "root": str(root),
                "project_key": str(bootstrap.get("project_key") or safe_name),
                "files": len(members),
                "seed_tasks": len(bootstrap.get("seed_tasks") or []),
                "auto_launched": job_id is not None,
                "job_id": job_id,
            }
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise


@app.get("/private-pack", response_class=HTMLResponse)
def private_pack_form():
    return HTMLResponse(
        """<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Private project pack</title><style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:760px;margin:35px auto;padding:0 18px;background:#f5f5f7;color:#161616}
.card{background:white;padding:22px;border-radius:18px;box-shadow:0 1px 6px #0001}input,button{font:inherit;padding:12px;border-radius:10px;box-sizing:border-box}input{width:100%;border:1px solid #ccc;margin:10px 0 16px}button{border:0;background:#111;color:#fff}.muted{color:#666;line-height:1.45}
</style></head><body><a href='/'>← Expert My Rules</a><div class='card'><h1>Import private research pack</h1>
<p class='muted'>The ZIP is unpacked only into the local workspace volume on this machine. Project sources are not committed to the Expert My Rules repository. If the pack requests auto-launch, the autonomous job starts immediately after a successful import.</p>
<form method='post' action='/private-pack/import' enctype='multipart/form-data'><input type='file' name='file' accept='.zip,application/zip' required><button>Import and start</button></form></div></body></html>"""
    )


@app.post("/private-pack/import")
async def private_pack_import(file: UploadFile = File(...)):
    raw = await file.read(MAX_PACK_BYTES + 1)
    if len(raw) > MAX_PACK_BYTES:
        raise HTTPException(status_code=413, detail="private pack exceeds size limit")
    try:
        from awb.web.app import _start

        result = import_private_pack(raw, start_callback=_start)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/project/{result['workspace']}", status_code=303)
