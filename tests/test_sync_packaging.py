"""Packaging/install contracts in isolated folders; no live applications."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location("sync_test_" + name, ROOT / "tools" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    core = repo / "skills/dwg-layered-skp"
    sync = repo / "skills/dwg-skp-sync"
    write(core / "SKILL.md", "---\nname: dwg-layered-skp\ndescription: Core\n---\n")
    write(sync / "SKILL.md", "---\nname: dwg-skp-sync\ndescription: Sync\n---\n")
    write(sync / "scripts/dwg_skp_sync.py", "# isolated test launcher\n")
    write(core / "scripts/runtime/dwg_semantic/sync.py", "VERSION = 1\n")
    bridge = "scripts/sketchup/dwg_live_repair_bridge.rb"
    write(core / bridge, "# DWG_LIVE_REPAIR_BRIDGE\n")
    key = "scripts/runtime/dwg_semantic/sync.py"
    manifest = {"schema": "dwg-layered-skp-package-v2", "files": {
        key: hashlib.sha256((core / key).read_bytes()).hexdigest()}}
    write(core / "scripts/runtime-manifest.json", json.dumps(manifest))
    packer, installer = module("package_skill"), module("install_skill")
    packer.package_sync(sync, core)
    monkeypatch.setattr(installer, "ROOT", repo)
    monkeypatch.setattr(installer, "SOURCE", core)
    return installer, packer, core, sync, tmp_path / "installed"


def install_core(case):
    installer, _, _, _, target = case
    installer.install(target / "dwg-layered-skp", dry_run=False)
    return target / "dwg-skp-sync"


def test_thin_package_pins_shared_runtime_and_bridge_without_copy(bundle):
    _, _, core, sync, _ = bundle
    dependency = json.loads((sync / "scripts/core-dependency.json").read_text())
    assert dependency["skill"] == "dwg-layered-skp"
    assert dependency["runtime_manifest_sha256"] == hashlib.sha256((core / "scripts/runtime-manifest.json").read_bytes()).hexdigest()
    assert set(dependency["files"]) == {"scripts/runtime/dwg_semantic/sync.py", "scripts/sketchup/dwg_live_repair_bridge.rb"}
    assert not (sync / "scripts/runtime").exists()


def test_pair_install_checks_dependency_and_creates_no_second_core(bundle):
    installer, _, _, _, _ = bundle
    target = install_core(bundle)
    preview = installer.install(target, skill="dwg-skp-sync", dry_run=True)
    assert not target.exists()
    assert preview["shared_core"]["verified_files"] == 2
    result = installer.install(target, skill="dwg-skp-sync", dry_run=False)
    assert result["verified"] == 3
    assert not (target / "scripts/runtime").exists()
    assert (target / "scripts/dwg_skp_sync.py").is_file()


def test_missing_shared_core_fails_before_creating_sync(bundle):
    installer, _, _, _, target = bundle
    with pytest.raises(ValueError, match="core first"):
        installer.install(target / "dwg-skp-sync", skill="dwg-skp-sync", dry_run=False)
    assert not target.exists()


@pytest.mark.parametrize("path", ["scripts/runtime/dwg_semantic/sync.py", "scripts/sketchup/dwg_live_repair_bridge.rb", "scripts/runtime-manifest.json"])
def test_drifted_core_or_bridge_fails_before_writing_any_sync_files(bundle, path):
    installer, _, _, _, _ = bundle
    target = install_core(bundle)
    changed = target.parent / "dwg-layered-skp" / path
    changed.write_text(changed.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        installer.install(target, skill="dwg-skp-sync", dry_run=False)
    assert not target.exists()


def test_update_backs_up_existing_sync_and_exact_retired_file(bundle):
    installer, _, _, _, _ = bundle
    target = install_core(bundle)
    installer.install(target, skill="dwg-skp-sync", dry_run=False)
    original = (target / "scripts/dwg_skp_sync.py").read_bytes()
    (target / "scripts/dwg_skp_sync.py").write_bytes(b"# previous local launcher\n")
    write(target / "references/obsolete.md", "recover this")
    write(target / "keep-local.txt", "local file stays")
    result = installer.install(target, skill="dwg-skp-sync", dry_run=False, retire=["references/obsolete.md"])
    backup = Path(result["backup"])
    assert (backup / "scripts/dwg_skp_sync.py").read_bytes() == b"# previous local launcher\n"
    assert (backup / "references/obsolete.md").read_text() == "recover this"
    assert not (target / "references/obsolete.md").exists()
    assert (target / "scripts/dwg_skp_sync.py").read_bytes() == original
    assert (target / "keep-local.txt").read_text() == "local file stays"


@pytest.mark.parametrize("relative", ["../outside", "SKILL.md", "scripts"])
def test_retirement_cannot_escape_or_remove_current_source(bundle, relative):
    installer, _, _, _, _ = bundle
    target = install_core(bundle)
    installer.install(target, skill="dwg-skp-sync", dry_run=False)
    with pytest.raises(ValueError):
        installer.install(target, skill="dwg-skp-sync", dry_run=True, retire=[relative])


def test_dependency_cannot_omit_runtime_or_add_escaping_paths(bundle):
    installer, _, _, sync, _ = bundle
    target = install_core(bundle)
    path = sync / "scripts/core-dependency.json"
    dependency = json.loads(path.read_text())
    dependency["files"].pop("scripts/runtime/dwg_semantic/sync.py")
    dependency["files"]["../outside"] = "bad"
    path.write_text(json.dumps(dependency), encoding="utf-8")
    with pytest.raises(ValueError, match="cover"):
        installer.install(target, skill="dwg-skp-sync", dry_run=False)
    assert not target.exists()


def test_packaging_rejects_second_runtime_in_sync(bundle):
    _, packer, core, sync, _ = bundle
    (sync / "scripts/runtime").mkdir()
    with pytest.raises(ValueError, match="second engine"):
        packer.package_sync(sync, core)


def test_installer_does_not_leave_an_existing_second_runtime(bundle):
    installer, _, _, _, _ = bundle
    target = install_core(bundle)
    write(target / "scripts/runtime/copied_engine.py", "# previous duplicate\n")
    with pytest.raises(ValueError, match="second runtime"):
        installer.install(target, skill="dwg-skp-sync", dry_run=False)
    assert (target / "scripts/runtime/copied_engine.py").read_text() == "# previous duplicate\n"
    assert not (target / "SKILL.md").exists()


def test_wrong_skill_target_and_source_install_are_rejected(bundle):
    installer, _, _, sync, target = bundle
    install_core(bundle)
    for path in (target / "dwg-layered-skp", sync):
        with pytest.raises(ValueError, match="separate"):
            installer.install(path, skill="dwg-skp-sync", dry_run=True)


def test_linked_install_target_is_rejected_before_resolving(bundle, monkeypatch):
    installer, _, _, _, target = bundle
    # Windows symlink privileges vary. Simulate the filesystem classification,
    # keeping this invariant test independent of admin or developer mode.
    path = target / "dwg-skp-sync"
    monkeypatch.setattr(installer, "_is_link", lambda candidate: candidate == path)
    with pytest.raises(ValueError, match="symlink or junction"):
        installer.install(path, skill="dwg-skp-sync", dry_run=False)
    assert not target.exists()


def test_linked_install_parent_is_rejected_before_resolving(bundle, monkeypatch):
    installer, _, _, _, target = bundle
    path = target / "dwg-skp-sync"
    monkeypatch.setattr(installer, "_is_link", lambda candidate: candidate == target)
    with pytest.raises(ValueError, match="symlink or junction"):
        installer.install(path, skill="dwg-skp-sync", dry_run=False)
    assert not target.exists()
