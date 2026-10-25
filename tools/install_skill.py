"""Install this reviewed skill, with a recoverable backup and path verification.

Run --dry-run first. Installing outside the workspace requires normal host tool
approval. Only this skill directory is changed. Explicitly retired files are
removed only after a recoverable backup; directories are never removed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/dwg-layered-skp"
SKILLS = ("dwg-layered-skp", "dwg-skp-sync")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _relative_file(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Manifest must name an exact relative skill file")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Manifest path escapes exact skill directory")
    return path


def _check_dependency(source: Path, core: Path) -> dict:
    """Require the exact installed sibling release before installing the action."""
    dependency = json.loads((source / "scripts/core-dependency.json").read_text(encoding="utf-8"))
    if (dependency.get("schema") != "dwg-skp-sync-core-dependency-v1"
            or dependency.get("skill") != "dwg-layered-skp"
            or dependency.get("runtime_manifest") != "scripts/runtime-manifest.json"
            or not isinstance(dependency.get("files"), dict) or not dependency["files"]):
        raise ValueError("Sync shared-core dependency is invalid; repackage both skills")
    if _is_link(core) or not core.is_dir():
        raise ValueError("Install the matching sibling dwg-layered-skp core first")
    manifest_path = core / dependency["runtime_manifest"]
    try:
        manifest_bytes = manifest_path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError("Install the matching sibling dwg-layered-skp core first") from error
    if hashlib.sha256(manifest_bytes).hexdigest() != dependency.get("runtime_manifest_sha256"):
        raise ValueError("Shared core release differs; install the matching dwg-layered-skp first")
    manifest = json.loads(manifest_bytes)
    bridge = "scripts/sketchup/dwg_live_repair_bridge.rb"
    if (manifest.get("schema") != "dwg-layered-skp-package-v2"
            or not isinstance(manifest.get("files"), dict)
            or set(dependency["files"]) != set(manifest["files"]) | {bridge}
            or any(dependency["files"].get(name) != value for name, value in manifest["files"].items())):
        raise ValueError("Shared core dependency does not cover the runtime manifest")
    for name, expected in dependency["files"].items():
        relative = _relative_file(name)
        path = core / relative
        if core not in path.resolve().parents or any(_is_link(p) for p in (path, *path.parents) if p != core.parent):
            raise ValueError("Shared core path escapes exact sibling skill directory")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Shared core file differs: " + name)
    return {"skill": "dwg-layered-skp", "path": str(core),
            "runtime_manifest_sha256": dependency["runtime_manifest_sha256"],
            "verified_files": len(dependency["files"])}


def install(target: Path, *, dry_run: bool, report_path: Path | None = None, retire=(),
            skill: str = "dwg-layered-skp") -> dict:
    if skill not in SKILLS:
        raise ValueError("Unsupported skill")
    source_root = SOURCE if skill == "dwg-layered-skp" else ROOT / "skills" / skill
    if any(_is_link(path) for path in (target, *target.parents)):
        raise ValueError("Installation directory must not be a symlink or junction")
    target = target.resolve()
    if target.name != skill or target == source_root.resolve() or source_root.resolve() in target.parents:
        raise ValueError(f"Target must be the separate {skill} installation directory")
    if not (source_root / "SKILL.md").is_file():
        raise ValueError("Skill source entrypoint is missing")
    if target.exists() and not target.is_dir():
        raise ValueError("Installation target must be a directory")
    if _is_link(source_root) or any(_is_link(p) for p in source_root.rglob("*")):
        raise ValueError("Skill source must not contain symlinks or junctions")
    dependency = _check_dependency(source_root, target.parent / "dwg-layered-skp") if skill == "dwg-skp-sync" else None
    if skill == "dwg-skp-sync" and any((root / "scripts/runtime").exists() for root in (source_root, target)):
        raise ValueError("Sync action must not contain a second runtime")
    files = [p for p in source_root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    if target.exists() and any(_is_link(p) for p in target.rglob("*")):
        raise ValueError("Existing skill must not contain symlinks or junctions")
    for source in files:
        destination = (target / source.relative_to(source_root)).resolve()
        if target not in destination.parents:
            raise ValueError("Install path escapes exact skill directory")
    retired = []
    for value in retire:
        relative = Path(value)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('Retirement must name an exact relative skill file')
        destination = (target / relative).resolve()
        if target not in destination.parents or (source_root / relative).exists():
            raise ValueError('Retirement escapes skill or is still part of source package')
        if destination.exists() and not destination.is_file():
            raise ValueError('Only exact files can be retired')
        retired.append((relative, destination))
    report = {"source": str(source_root), "target": str(target), "skill": skill, "files": len(files), "dry_run": dry_run,
              "retire": [p.as_posix() for p, _ in retired]}
    if dependency is not None:
        report["shared_core"] = dependency
    if dry_run:
        return report
    backup = ROOT / "work/skill-install-backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + skill + "-" + uuid.uuid4().hex[:8])
    if target.exists():
        shutil.copytree(target, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        report["backup"] = str(backup)
    for relative, destination in retired:
        if destination.exists():
            if not (backup / relative).is_file():
                raise RuntimeError('Retirement backup missing')
            destination.unlink()
    installed = {}
    for source in files:
        relative = source.relative_to(source_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Installed file hash mismatch: {relative}")
        installed[relative.as_posix()] = expected
    report["verified"] = len(installed)
    report_path = report_path or ROOT / "work/upgraded-validation" / ("installed-skill.json" if skill == "dwg-layered-skp" else "installed-dwg-skp-sync.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({**report, "hashes": installed}, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path.resolve())
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--skill", choices=SKILLS, default="dwg-layered-skp")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path, help="Save verification in a new versioned report")
    parser.add_argument("--retire", action="append", default=[], help="Exact obsolete relative file; backed up before removal")
    args = parser.parse_args()
    print(json.dumps(install(args.target, dry_run=args.dry_run, report_path=args.report, retire=args.retire, skill=args.skill), ensure_ascii=False, indent=2))
