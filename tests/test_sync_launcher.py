"""Run the shipped thin launcher from isolated, portable sibling installs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installed_pair(tmp_path):
    parent = tmp_path / "portable-skills"
    for name in ("dwg-layered-skp", "dwg-skp-sync"):
        shutil.copytree(
            ROOT / "skills" / name, parent / name,
            ignore=shutil.ignore_patterns(".deps", "__pycache__", "*.pyc"),
        )
    cwd = tmp_path / "unrelated-working-directory"
    cwd.mkdir()
    return parent, cwd


def launch(parent, cwd, *args, dependencies=None):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    # The launcher must find its sibling release, never the developer checkout.
    env.pop("PYTHONPATH", None)
    env.pop("DWG_SKP_DEPENDENCIES", None)
    if dependencies is not None:
        env["DWG_SKP_DEPENDENCIES"] = str(dependencies)
    return subprocess.run(
        [sys.executable, str(parent / "dwg-skp-sync/scripts/dwg_skp_sync.py"), *args],
        cwd=cwd, env=env, capture_output=True, encoding="utf-8", timeout=30,
    )


def test_portable_doctor_resolves_only_sibling_core_from_unrelated_cwd(installed_pair):
    parent, cwd = installed_pair
    result = launch(parent, cwd, "doctor")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "ready" and data["skill"] == "dwg-skp-sync"
    assert data["applications_contacted"] is False and data["model_modified"] is False
    assert Path(data["bridge_source"]).resolve() == (
        parent / "dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb"
    ).resolve()
    assert data["contract"]["snapshot_mode"] == "local_geometry"
    assert data["contract"]["component_index"] is False
    assert not (parent / "dwg-skp-sync/scripts/runtime").exists()
    assert not (parent / "dwg-skp-sync/scripts/.deps").exists()
    assert not list(cwd.iterdir())


def test_portable_help_exposes_sync_actions_without_initial_modeling(installed_pair):
    parent, cwd = installed_pair
    result = launch(parent, cwd, "--help")
    assert result.returncode == 0, result.stderr
    assert "dwg-skp-sync" in result.stdout
    assert "plan" in result.stdout and "apply" in result.stdout and "status" in result.stdout
    assert "build" not in result.stdout and "approve" not in result.stdout
    assert not list(cwd.iterdir())


def test_portable_sync_rejects_build_before_any_model_work(installed_pair):
    parent, cwd = installed_pair
    result = launch(parent, cwd, "build")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr and "build" in result.stderr
    assert not result.stdout.strip()
    assert not list(cwd.iterdir())


def test_missing_sibling_core_fails_without_falling_back_to_checkout(tmp_path):
    parent = tmp_path / "skills"
    shutil.copytree(ROOT / "skills/dwg-skp-sync", parent / "dwg-skp-sync")
    cwd = tmp_path / "unrelated"
    cwd.mkdir()
    result = launch(parent, cwd, "doctor")
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "error"
    assert not result.stdout.strip()
    assert not (parent / "dwg-layered-skp").exists()
    assert not list(cwd.iterdir())


@pytest.mark.parametrize("relative", [
    "scripts/runtime/dwg_semantic/__init__.py",
    "scripts/runtime/dwg_semantic/sync.py",
    "scripts/sketchup/dwg_live_repair_bridge.rb",
    "scripts/runtime-manifest.json",
])
def test_drifted_release_rejects_runtime_bridge_or_manifest_before_execution(installed_pair, relative):
    parent, cwd = installed_pair
    path = parent / "dwg-layered-skp" / relative
    marker = cwd / "unverified-runtime-executed.txt"
    if path.suffix == ".py":
        # Executing the changed file would leave observable evidence. Both the
        # package initializer and command module must be verified before import.
        path.write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unsafe import')\n",
            encoding="utf-8",
        )
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    result = launch(parent, cwd, "doctor")
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "error"
    assert not result.stdout.strip()
    assert not marker.exists()
    assert not list(cwd.iterdir())


def test_dependency_override_cannot_shadow_pinned_dwg_semantic_engine(installed_pair, tmp_path):
    parent, cwd = installed_pair
    alternate = tmp_path / "third-party-dependencies"
    fake = alternate / "dwg_semantic"
    fake.mkdir(parents=True)
    marker = cwd / "shadow-engine-executed.txt"
    (fake / "__init__.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('shadow import')\n"
        "raise RuntimeError('untrusted engine override was imported')\n",
        encoding="utf-8",
    )
    result = launch(parent, cwd, "doctor", dependencies=alternate)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["skill"] == "dwg-skp-sync"
    assert not marker.exists()
    assert not list(cwd.iterdir())


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---", 2)[1])


def test_sync_metadata_identifies_name_and_allows_explicit_and_implicit_use():
    skill = ROOT / "skills/dwg-skp-sync"
    metadata = frontmatter(skill / "SKILL.md")
    ui = yaml.safe_load((skill / "agents/openai.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "dwg-skp-sync"
    assert ui["interface"]["display_name"] == "dwg-skp-sync"
    assert "$dwg-skp-sync" in ui["interface"]["default_prompt"]
    assert ui.get("policy", {}).get("allow_implicit_invocation", True) is True
    assert "同步我目前選取的牆" in metadata["description"]
    assert "dwg-layered-skp" in metadata["description"]


def test_initial_modeling_metadata_routes_current_selection_sync_to_separate_skill():
    initial = frontmatter(ROOT / "skills/dwg-layered-skp/SKILL.md")
    assert initial["name"] == "dwg-layered-skp"
    assert "initial" in initial["description"].lower()
    assert "dwg-skp-sync" in initial["description"]


def test_preloaded_foreign_engine_is_rejected_in_reused_interpreter(installed_pair):
    parent, cwd = installed_pair
    launcher = parent / "dwg-skp-sync/scripts/dwg_skp_sync.py"
    script = """import importlib.util, sys, types
spec=importlib.util.spec_from_file_location('thin_sync',sys.argv[1])
launcher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
foreign=types.ModuleType('dwg_semantic.sync')
foreign.__file__=str(__import__('pathlib').Path.cwd()/'foreign_engine.py')
foreign.main=lambda:71
sys.modules['dwg_semantic.sync']=foreign
raise SystemExit(launcher.main())
"""
    result = subprocess.run([sys.executable, "-c", script, str(launcher)], cwd=cwd,
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2
    assert "另一份 dwg_semantic" in json.loads(result.stderr)["message"]
    assert not result.stdout.strip() and not list(cwd.iterdir())
