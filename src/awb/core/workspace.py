from __future__ import annotations

from pathlib import Path
import yaml

from .models import ProjectManifest, Workspace


def load_workspace(root: Path) -> Workspace:
    manifest_path = root / "project.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing {manifest_path}")
    data = yaml.safe_load(manifest_path.read_text())
    return Workspace(root=root, manifest=ProjectManifest.model_validate(data))


def write_workspace(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
