from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Gate, Task
from .storage import Ledger
from .workspace import load_workspace, save_manifest, write_workspace
from awb.templates.templates import get_template


class PrivateImportError(RuntimeError):
    pass


PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$")
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_FILES = 250


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    raw = info.filename.replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PrivateImportError(f"Unsafe archive path: {info.filename}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise PrivateImportError(f"Symlinks are not allowed in private bundles: {info.filename}")
    return path


def _load_bootstrap(zf: zipfile.ZipFile) -> dict[str, Any]:
    candidates = [name for name in zf.namelist() if PurePosixPath(name).name == "PROJECT_BOOTSTRAP.json"]
    if len(candidates) != 1:
        raise PrivateImportError("Bundle must contain exactly one PROJECT_BOOTSTRAP.json")
    try:
        data = json.loads(zf.read(candidates[0]).decode("utf-8"))
    except Exception as exc:
        raise PrivateImportError(f"Invalid PROJECT_BOOTSTRAP.json: {exc}") from exc
    if not isinstance(data, dict):
        raise PrivateImportError("PROJECT_BOOTSTRAP.json must contain an object")
    return data


def import_private_bundle(bundle: bytes, workspaces_dir: Path) -> tuple[Path, dict[str, Any]]:
    if not bundle:
        raise PrivateImportError("Empty private project bundle")
    if len(bundle) > MAX_ARCHIVE_BYTES:
        raise PrivateImportError("Private project bundle exceeds the 25 MB upload limit")

    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle))
    except zipfile.BadZipFile as exc:
        raise PrivateImportError("Private project bundle must be a ZIP archive") from exc

    with zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        if not infos or len(infos) > MAX_FILES:
            raise PrivateImportError(f"Private bundle must contain between 1 and {MAX_FILES} files")
        total = sum(max(0, info.file_size) for info in infos)
        if total > MAX_EXTRACTED_BYTES:
            raise PrivateImportError("Expanded private bundle exceeds the 100 MB safety limit")
        safe_paths = {info.filename: _safe_member(info) for info in infos}
        bootstrap = _load_bootstrap(zf)

        project_key = str(bootstrap.get("project_key") or bootstrap.get("project_name") or "").strip()
        if not PROJECT_KEY_RE.fullmatch(project_key):
            raise PrivateImportError("project_key must be 2-80 characters using letters, digits, '.', '_' or '-'")
        kind = str(bootstrap.get("kind") or "research").strip()
        if kind != "research":
            raise PrivateImportError("Private bundle import currently accepts research projects only")
        goal = str(bootstrap.get("goal") or "").strip()
        if len(goal) < 20:
            raise PrivateImportError("Private bundle must declare a substantive goal")

        root = (workspaces_dir.resolve() / project_key).resolve()
        workspaces_root = workspaces_dir.resolve()
        if root.parent != workspaces_root:
            raise PrivateImportError("Resolved project path escapes the workspace directory")
        if root.exists() and any(root.iterdir()):
            raise PrivateImportError(f"Workspace already exists: {project_key}")

        manifest = get_template("research", project_key, goal)
        if bootstrap.get("description"):
            manifest["description"] = str(bootstrap["description"])

        requested_gates = bootstrap.get("gates")
        if requested_gates is not None:
            if not isinstance(requested_gates, list) or not requested_gates:
                raise PrivateImportError("gates must be a non-empty list when supplied")
            validated = []
            seen: set[str] = set()
            for item in requested_gates:
                if not isinstance(item, dict):
                    raise PrivateImportError("Each gate must be an object")
                gate = Gate.model_validate(item)
                if gate.id in seen:
                    raise PrivateImportError(f"Duplicate gate id: {gate.id}")
                seen.add(gate.id)
                validated.append(gate.model_dump(mode="json"))
            manifest["gates"] = validated

        overrides = bootstrap.get("agent_instructions") or {}
        if not isinstance(overrides, dict):
            raise PrivateImportError("agent_instructions must be an object")
        for agent in manifest["agents"]:
            if agent["id"] in overrides:
                agent["instructions"] = str(overrides[agent["id"]]).strip()

        # Imported source material is immutable to autonomous agents. Derived work belongs
        # in artifacts or normal workspace files, never back into the uploaded evidence.
        protected = manifest["runtime"].setdefault("git", {}).setdefault("protected_paths", [])
        if "sources" not in protected:
            protected.append("sources")

        write_workspace(root, manifest)
        source_root = root / "sources"
        source_root.mkdir(parents=True, exist_ok=True)
        for info in infos:
            relative = safe_paths[info.filename]
            target = (source_root / Path(*relative.parts)).resolve()
            if source_root.resolve() not in target.parents and target != source_root.resolve():
                raise PrivateImportError(f"Archive member escapes source directory: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))

        # Record provenance without exposing source contents in logs.
        provenance = {
            "project_key": project_key,
            "imported_files": len(infos),
            "expanded_bytes": total,
            "auto_launch": bool(bootstrap.get("auto_launch", False)),
        }
        (root / "PRIVATE_IMPORT.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

        ws = load_workspace(root)
        ledger = Ledger(root / "ledger.sqlite3")
        seed_tasks = bootstrap.get("seed_tasks") or []
        if not isinstance(seed_tasks, list):
            raise PrivateImportError("seed_tasks must be a list")
        for index, item in enumerate(seed_tasks, start=1):
            if not isinstance(item, dict):
                raise PrivateImportError("Each seed task must be an object")
            title = str(item.get("title") or "").strip()
            if not title:
                raise PrivateImportError("Every seed task requires a title")
            task = Task(
                id=f"SEED-{index:04d}",
                title=title,
                description=str(item.get("description") or title),
                priority=float(item.get("priority", 10.0)),
                created_by="private_import",
            )
            ledger.upsert_task(task)
            ledger.event("task_created", {"title": title, "source": "private_import"}, task.id)
        ledger.event("private_bundle_imported", provenance)
        save_manifest(ws)
        return root, bootstrap
