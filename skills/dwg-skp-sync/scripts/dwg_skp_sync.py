"""Portable thin launcher: one pinned sibling core, no second geometry runtime."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import sys


def shared_core() -> Path:
    skill = Path(__file__).resolve().parents[1]
    core = (skill.parent / "dwg-layered-skp").resolve()
    dependency = json.loads((skill / "scripts/core-dependency.json").read_text(encoding="utf-8"))
    if (dependency.get("schema") != "dwg-skp-sync-core-dependency-v1" or
            dependency.get("skill") != "dwg-layered-skp" or
            dependency.get("runtime_manifest") != "scripts/runtime-manifest.json"):
        raise ValueError("dwg-skp-sync的共用核心相依契約不符；請安裝同一發布版本的兩個技能。")
    manifest_path = core / dependency["runtime_manifest"]
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != dependency.get("runtime_manifest_sha256"):
        raise ValueError("共用核心版本與dwg-skp-sync不一致；請安裝同一發布版本，勿在同步時重建引擎。")
    manifest = json.loads(raw)
    files = dependency.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("dwg-skp-sync缺少共用核心檔案雜湊。")
    expected = dict(manifest["files"])
    bridge = "scripts/sketchup/dwg_live_repair_bridge.rb"
    expected[bridge] = files.get(bridge)
    if manifest.get("schema") != "dwg-layered-skp-package-v2" or files != expected:
        raise ValueError("dwg-skp-sync共用核心清單不完整。")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        path = (core / name).resolve()
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name or
                core not in path.parents or
                hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("dwg-skp-sync共用核心檔案已變動：" + name)
    return core


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        core = shared_core()
        runtime = core / "scripts/runtime"
        # A reused interpreter may already have a different engine imported;
        # changing sys.path cannot replace those cached modules safely.
        for name, module in list(sys.modules.items()):
            if name == "dwg_semantic" or name.startswith("dwg_semantic."):
                origin = getattr(module, "__file__", None)
                if not origin or runtime not in Path(origin).resolve().parents:
                    raise ValueError("此 Python 行程已載入另一份 dwg_semantic；請在獨立行程啟動dwg-skp-sync。")
        # Dependency overrides are for third-party packages, never a substitute
        # dwg_semantic engine from the workspace or a different skill release.
        dependencies = [value for value in os.environ.get("DWG_SKP_DEPENDENCIES", "").split(os.pathsep) if value]
        dependencies.append(str(core / "scripts/.deps"))
        for directory in reversed(dependencies):
            if Path(directory).is_dir():
                sys.path.insert(0, directory)
        sys.path.insert(0, str(runtime))
        command = importlib.import_module("dwg_semantic.sync")
        if runtime not in Path(command.__file__).resolve().parents:
            raise ValueError("dwg-skp-sync載入的核心來源不符。")
        return command.main()
    except (OSError, ValueError, ImportError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "message": "dwg-skp-sync無法啟動：" + str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
