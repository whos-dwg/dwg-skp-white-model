"""Release bytes must survive Git LF normalization without changing pins."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location("archive_test_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(repo, newline, reverse=False):
    files = {
        ".gitattributes": "* text=auto eol=lf\n",
        "config/default_rules.json": '{"rules": []}\n',
        "src/dwg_semantic/__init__.py": '"""Synthetic package."""\n',
        "src/dwg_semantic/sync.py": "def main():\n    return 0\n",
        "src/dwg_semantic/adapters/Fixture.cs": "// synthetic adapter\nclass Fixture {}\n",
        "skills/dwg-layered-skp/SKILL.md": "---\nname: dwg-layered-skp\n---\n",
        "skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb": "# synthetic bridge\nVERSION = 1\n",
        "skills/dwg-skp-sync/SKILL.md": "---\nname: dwg-skp-sync\n---\n",
        "skills/dwg-skp-sync/scripts/dwg_skp_sync.py": "# synthetic launcher\n",
    }
    for name, text in reversed(list(files.items())) if reverse else files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8").replace(b"\n", newline))
    packer = load_tool("package_skill")
    packer.ROOT = repo
    packer.package(repo / "skills/dwg-layered-skp")
    return repo / "skills/dwg-layered-skp", repo / "skills/dwg-skp-sync"


def pinned_files(core, sync):
    dependency_path = sync / "scripts/core-dependency.json"
    dependency = json.loads(dependency_path.read_bytes())
    manifest_path = core / dependency["runtime_manifest"]
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == dependency["runtime_manifest_sha256"]
    result = {
        "runtime-manifest.json": manifest_path.read_bytes(),
        "core-dependency.json": dependency_path.read_bytes(),
    }
    for relative, expected in dependency["files"].items():
        content = (core / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        result[relative] = content
    return result


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf-checkout", "crlf-checkout"])
def test_packaged_pins_and_hashed_payload_are_lf_on_every_checkout(tmp_path, newline):
    core, sync = seed(tmp_path / "repo", newline)
    assert all(b"\r\n" not in content for content in pinned_files(core, sync).values())


def test_checkout_newlines_and_file_creation_order_produce_identical_pinned_bytes(tmp_path):
    first = seed(tmp_path / "lf", b"\n")
    second = seed(tmp_path / "crlf", b"\r\n", reverse=True)
    assert pinned_files(*first) == pinned_files(*second)


def test_git_archive_from_crlf_checkout_keeps_pair_installable_and_rejects_tampering(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is needed to verify actual source archive normalization")
    repo = tmp_path / "repo"
    seed(repo, b"\r\n")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "--quiet")
    git("-c", "core.autocrlf=false", "add", ".gitattributes", "config", "src", "skills")
    git("-c", "user.name=Release regression", "-c", "user.email=release-regression@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic release")
    archive = tmp_path / "source.zip"
    git("archive", "--format=zip", "--output=" + str(archive), "HEAD")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extracted)
    assert not (extracted / ".git").exists()
    core = extracted / "skills/dwg-layered-skp"
    sync = extracted / "skills/dwg-skp-sync"
    assert all(b"\r\n" not in content for content in pinned_files(core, sync).values())
    installer = load_tool("install_skill")
    installer.ROOT = extracted
    installer.SOURCE = core
    installed = tmp_path / "installed"
    installer.install(installed / "dwg-layered-skp", dry_run=False)
    installer.install(installed / "dwg-skp-sync", skill="dwg-skp-sync", dry_run=True)
    result = installer.install(installed / "dwg-skp-sync", skill="dwg-skp-sync", dry_run=False)
    assert result["shared_core"]["verified_files"] == 5
    changed = installed / "dwg-layered-skp/scripts/runtime/dwg_semantic/sync.py"
    changed.write_bytes(changed.read_bytes() + b"# changed after installation\n")
    with pytest.raises(ValueError, match="file differs"):
        installer.install(installed / "dwg-skp-sync", skill="dwg-skp-sync", dry_run=True)
