"""Copy the tested runtime into the skill; the source package remains canonical."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_SKILL = "dwg-layered-skp"
SYNC_SKILL = "dwg-skp-sync"
DEPENDENCY_SCHEMA = "dwg-skp-sync-core-dependency-v1"


def _copy_text(source: Path, target: Path) -> None:
    """Hash the same LF bytes that Git will store and export."""
    target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))


def _write_json(path: Path, value: dict) -> None:
    # write_text() translates LF to CRLF on Windows, invalidating a raw-byte
    # manifest pin after Git applies the repository's eol=lf policy.
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def package_sync(destination: Path, core: Path) -> dict:
    """Pin the packaged sibling runtime; the sync action carries no engine copy."""
    destination, core = destination.resolve(), core.resolve()
    if not (destination / "SKILL.md").is_file():
        raise ValueError("Sync skill entrypoint is missing")
    if (destination / "scripts/runtime").exists():
        raise ValueError("Sync must share the sibling runtime, not contain a second engine")
    manifest_path = core / "scripts/runtime-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != "dwg-layered-skp-package-v2" or not manifest.get("files"):
        raise ValueError("Core runtime manifest is invalid")
    files = dict(manifest["files"])
    bridge = "scripts/sketchup/dwg_live_repair_bridge.rb"
    _copy_text(core / bridge, core / bridge)
    files[bridge] = hashlib.sha256((core / bridge).read_bytes()).hexdigest()
    for relative, expected in files.items():
        path = (core / relative).resolve()
        if core not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Core runtime manifest does not match: " + relative)
    dependency = {
        "schema": DEPENDENCY_SCHEMA,
        "skill": CORE_SKILL,
        "runtime_manifest": "scripts/runtime-manifest.json",
        "runtime_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "files": files,
    }
    target = destination / "scripts/core-dependency.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, dependency)
    return {"destination": str(destination), "shared_core": str(core), "pinned_files": len(files)}


def package(destination: Path) -> dict:
    destination = destination.resolve()
    runtime = destination / "scripts/runtime/dwg_semantic"
    # Keep normal pip installs and the skill bundle on the same canonical rules.
    canonical_rules = ROOT / "src/dwg_semantic/config/default_rules.json"
    canonical_rules.parent.mkdir(parents=True, exist_ok=True)
    _copy_text(ROOT / "config/default_rules.json", canonical_rules)
    for source in sorted((ROOT / "src/dwg_semantic").rglob("*")):
        if not source.is_file() or any(part in {"__pycache__", "bin", "obj"} for part in source.relative_to(ROOT / "src/dwg_semantic").parts):
            continue
        if source.suffix not in {".py", ".cs", ".csproj", ".json", ".ps1"}:
            continue
        relative = source.relative_to(ROOT / "src/dwg_semantic")
        if relative.parts[0] == 'config' and relative.as_posix() != 'config/default_rules.json':
            continue
        target = runtime / source.relative_to(ROOT / "src/dwg_semantic")
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_text(source, target)
    # Per-project recipes belong to project archives, not the normal skill.
    # Existing retired files are explicitly backed up/removed by installation,
    # never by a broad directory deletion here.
    files = {}
    for source in sorted(runtime.rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            files[source.relative_to(destination).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {"schema": "dwg-layered-skp-package-v2", "files": files}
    _write_json(destination / "scripts/runtime-manifest.json", manifest)
    report = {"destination": str(destination), "runtime_files": len(files)}
    sync = destination.parent / SYNC_SKILL
    if (sync / "SKILL.md").is_file():
        report["sync"] = package_sync(sync, destination)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=ROOT / "skills/dwg-layered-skp")
    args = parser.parse_args()
    print(json.dumps(package(args.destination), ensure_ascii=False))
