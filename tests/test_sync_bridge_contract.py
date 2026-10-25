"""Standalone sync plans through production Ruby in an isolated adapter.

The embedded Ruby process has synthetic SketchUp entities and forbids starting
the bridge listener. It never connects to either live application.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

from test_sync_cli import bind_case, make_plan, read_json


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/native_safety"))
from test_native_safety_contract import run_ruby


@pytest.mark.parametrize("compound", [False, True], ids=["single-live-wall", "compound-wall-system"])
def test_sync_cli_observation_hash_is_accepted_by_production_ruby_and_commits_once(tmp_path, capsys, compound):
    case = bind_case(tmp_path, compound=compound)
    _, plan = make_plan(case, tmp_path, capsys)
    observation = plan["source"]["sync_selection"]
    assert observation["selection_snapshot"] == read_json(case["selection"])
    canonical = json.loads(plan["canonical_body_json"])
    assert canonical["source"]["sync_selection"] == observation

    result = run_ruby({"snapshot": case["state"], "plan": plan, "production_create": True})
    assert result["accepted"] is True, result
    assert result["operations"] == ["start", "commit"], result
    receipt = result["receipt"]
    assert receipt["status"] == "applied" and receipt["verified"] is True
    assert receipt["plan_sha256"] == plan["plan_sha256"]
    assert receipt["model_saved"] is False and receipt["new_skp_created"] is False
    assert len(receipt["removed"]) == len(plan["targets"])
    assert len(receipt["created"]) == len(plan["create"])
    assert all(row["manifold"] and row["volume_mm3"] > 0 for row in receipt["created"])
    assert result["selection_entity_ids"] == [row["entity_id"] for row in receipt["created"]]
    assert not (tmp_path / "current.skp").exists()


@pytest.mark.parametrize("compound", [False, True], ids=["single-live-wall", "compound-wall-system"])
def test_production_ruby_rejects_changed_full_observation_before_starting_operation(tmp_path, capsys, compound):
    case = bind_case(tmp_path, compound=compound)
    _, plan = make_plan(case, tmp_path, capsys)
    changed = copy.deepcopy(plan)
    # This field is inside the new full observation, rather than the older
    # live_cad_revision overlay. Ruby must enforce the complete canonical body.
    changed["source"]["sync_selection"]["selection_snapshot"]["objects"][0]["handle"] = "TAMPERED"
    result = run_ruby({"snapshot": case["state"], "plan": changed, "production_create": True})
    assert result["accepted"] is False, result
    assert result["code"] == "PLAN_DIGEST_MISMATCH", result
    assert result["operations"] == [], result
    assert not (tmp_path / "current.skp").exists()
