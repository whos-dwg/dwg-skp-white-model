"""Exercise the standalone sync entry point without either live application.

The source files, source-bound model snapshots and Pickfirst reads are isolated
synthetic evidence. Only the narrow SketchUp transport is substituted.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from test_live_cad_geometry import cad, entity
from test_live_repair import document, selection as opening_selection, snapshot
from test_live_wall_system import fixture as compound_fixture


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def bind_case(tmp_path, *, compound=False):
    """Bind synthetic geometry to an actual immutable temporary source file."""
    if compound:
        doc, picked, state, scope, material, opening = compound_fixture()
    else:
        doc, picked, state = document(), cad(), snapshot()
        scope = material = opening = None
    source = tmp_path / "source.dwg"
    source.write_bytes(b"isolated sync test source; not a production drawing")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    doc["intake"].update(source_path=str(source), source_sha256=source_sha)
    for row in [*doc.get("entities", []), *doc.get("resolved_instances", [])]:
        row["source_file"] = str(source)
    picked["document"]["path"] = str(source)
    state["root"]["source_manifest"].update(source_path=str(source), source_sha256=source_sha)
    state["model"]["path"] = str(tmp_path / "current.skp")
    job = tmp_path / "job"
    write_json(job / "document.json", doc)
    selection = write_json(tmp_path / "cad-plan-current.json", picked)
    scope_path = write_json(tmp_path / "wall-scope.json", scope) if compound else None
    return {
        "job": job, "selection": selection, "state": state, "scope": scope_path,
        "source": source, "material": material, "opening": opening,
    }


def run_cli(argv):
    return importlib.import_module("dwg_semantic.sync").main([str(v) for v in argv])


def plan_args(case, plan_path):
    args = ["plan", case["job"], "--full-model", "--selection-json", case["selection"],
            "--live-cad-geometry", "--output", plan_path]
    if case["scope"]:
        args += ["--wall-system-scope-json", case["scope"]]
    else:
        args += ["--auto-match-position"]
    return args


def make_plan(case, tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", return_value=case["state"]), \
         patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("no live application")):
        assert run_cli(plan_args(case, plan_path)) == 0
    capsys.readouterr()
    return plan_path, read_json(plan_path)


def apply_args(plan_path, plan, receipt, current=None):
    args = ["apply", plan_path, "--confirm-sha256", plan["plan_sha256"], "--receipt", receipt]
    if current is not None:
        args += ["--current-selection-json", current]
    return args


def current_selection(case, tmp_path, *, changed=False):
    picked = read_json(case["selection"])
    picked["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    if changed:
        picked["objects"][0]["properties"]["Layer"] = "CHANGED-SINCE-PLAN"
    return write_json(tmp_path / "cad-apply-current.json", picked)


def test_standalone_plan_shortens_live_wall_without_changing_source_or_model(tmp_path, capsys):
    case = bind_case(tmp_path)
    source_before = case["source"].read_bytes()
    document_before = (case["job"] / "document.json").read_bytes()
    state_before = copy.deepcopy(case["state"])
    plan_path = tmp_path / "plan.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", return_value=case["state"]), \
         patch("dwg_semantic.sketchup_live.apply_live_repair_plan", side_effect=AssertionError("plan cannot apply")):
        assert run_cli(plan_args(case, plan_path)) == 0
    result = json.loads(capsys.readouterr().out)
    plan = read_json(plan_path)
    assert result["status"] == "ready_to_apply"
    assert result["model_modified"] is False and result["new_skp_created"] is False
    assert result["plan_sha256"] == plan["plan_sha256"]
    assert plan["action"] == "replace_from_cad"
    assert [row["entity_id"] for row in plan["targets"]] == ["entity:101"]
    assert Polygon(plan["create"][0]["footprint"]["outer"]).bounds == (0, 0, 2500, 200)
    assert plan["source"]["live_cad_revision"]["selection_snapshot"] == read_json(case["selection"])
    assert case["state"] == state_before
    assert case["source"].read_bytes() == source_before
    assert (case["job"] / "document.json").read_bytes() == document_before
    assert not (tmp_path / "current.skp").exists()


def test_standalone_compound_plan_preserves_two_cavities_opening_and_equipment(tmp_path, capsys):
    case = bind_case(tmp_path, compound=True)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    assert len(plan["targets"]) == 3 and len(plan["create"]) == 2
    lower = [Polygon(row["footprint"]["outer"], row["footprint"]["holes"])
             for row in plan["create"] if row["base_z_mm"] == 0]
    upper = [Polygon(row["footprint"]["outer"], row["footprint"]["holes"])
             for row in plan["create"]]
    assert unary_union(lower).symmetric_difference(case["material"].difference(case["opening"])).area < .001
    assert unary_union(upper).symmetric_difference(case["material"]).area < .001
    assert plan["source"]["preserved_opening_audit"][0]["clear_top_z_mm"] == 2000
    assert plan["source"]["preserved_opening_audit"][0]["source_2d_edge_z_mm"] == 200
    assert all("C" not in row["provenance"]["source_handles"] for row in plan["create"])
    assert "C" in plan["source"]["live_cad_revision"]["scope"]["roles"]
    assert plan_path.is_file()


@pytest.mark.parametrize("bad_evidence", ["partial", "stale", "ambiguous", "source_drift"])
def test_standalone_plan_rejects_incomplete_stale_ambiguous_or_changed_source(tmp_path, capsys, bad_evidence):
    case = bind_case(tmp_path)
    picked = read_json(case["selection"])
    if bad_evidence == "partial":
        picked["returned_count"] = 0
    elif bad_evidence == "stale":
        picked["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    elif bad_evidence == "ambiguous":
        case["state"]["entities"].append(entity("wall-duplicate", "entity:202"))
    else:
        case["source"].write_bytes(b"edited or overwritten source")
    write_json(case["selection"], picked)
    plan_path = tmp_path / "blocked.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", return_value=case["state"]), \
         patch("dwg_semantic.sketchup_live.apply_live_repair_plan", side_effect=AssertionError("must not apply")):
        assert run_cli(plan_args(case, plan_path)) == 2
    assert not plan_path.exists()
    assert json.loads(capsys.readouterr().err)["status"] == "error"


@pytest.mark.parametrize("compound", [False, True])
def test_standalone_apply_requires_fresh_second_pickfirst_read_before_bridge(tmp_path, capsys, compound):
    case = bind_case(tmp_path, compound=compound)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    receipt = tmp_path / "receipt.json"
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("must reject before bridge")):
        assert run_cli(apply_args(plan_path, plan, receipt)) == 2
    assert "重新讀取" in json.loads(capsys.readouterr().err)["message"]
    assert not receipt.exists()


@pytest.mark.parametrize("compound", [False, True])
def test_standalone_apply_rejects_changed_second_read_before_bridge(tmp_path, capsys, compound):
    case = bind_case(tmp_path, compound=compound)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    current = current_selection(case, tmp_path, changed=True)
    receipt = tmp_path / "receipt.json"
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("must reject before bridge")):
        assert run_cli(apply_args(plan_path, plan, receipt, current)) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    assert not receipt.exists()


def test_standalone_apply_sends_only_verified_plan_and_persists_one_receipt(tmp_path, capsys):
    case = bind_case(tmp_path)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    current = current_selection(case, tmp_path)
    receipt_path = tmp_path / "receipt.json"
    remote_result = {"status": "applied", "model_saved": False, "new_skp_created": False,
                     "undo_name": "Synthetic sync", "created_entity_ids": ["entity:303"]}
    with patch("dwg_semantic.sketchup_live._require_current_bridge"), \
         patch("dwg_semantic.sketchup_live._call", return_value=remote_result) as call:
        assert run_cli(apply_args(plan_path, plan, receipt_path, current)) == 0
    assert call.call_count == 1
    assert call.call_args.args[0] == "apply_live_repair"
    assert call.call_args.args[1]["plan"] == plan
    assert read_json(receipt_path) == remote_result
    assert json.loads(capsys.readouterr().out) == remote_result


def test_existing_receipt_stops_before_any_bridge_request(tmp_path, capsys):
    case = bind_case(tmp_path)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    receipt = write_json(tmp_path / "receipt.json", {"retained": "earlier operation"})
    before = receipt.read_bytes()
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("receipt preflight must stop")):
        assert run_cli(apply_args(plan_path, plan, receipt, current_selection(case, tmp_path))) == 2
    assert receipt.read_bytes() == before
    assert "已存在" in json.loads(capsys.readouterr().err)["message"]


def test_receipt_race_reports_committed_result_and_never_retries_apply(tmp_path, capsys):
    case = bind_case(tmp_path)
    plan_path, plan = make_plan(case, tmp_path, capsys)
    receipt = tmp_path / "receipt.json"

    def committed_with_racing_file(*args, **kwargs):
        write_json(receipt, {"racing_writer": True})
        return {"status": "applied", "model_saved": False, "new_skp_created": False}

    with patch("dwg_semantic.sketchup_live._require_current_bridge"), \
         patch("dwg_semantic.sketchup_live._call", side_effect=committed_with_racing_file) as call:
        assert run_cli(apply_args(plan_path, plan, receipt, current_selection(case, tmp_path))) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "applied" and result["receipt_persistence_error"]
    assert read_json(receipt) == {"racing_writer": True}
    assert call.call_count == 1


def opening_case(tmp_path):
    case = bind_case(tmp_path)
    picked = opening_selection()
    picked["document"]["path"] = str(case["source"])
    picked["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(case["selection"], picked)
    return case


def make_opening_plan(case, tmp_path, capsys):
    plan_path = tmp_path / "opening-plan.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", return_value=case["state"]), \
         patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("no live application")):
        assert run_cli(["plan", case["job"], "--full-model", "--selection-json", case["selection"],
                        "--action", "cut_opening", "--height-mm", "2100", "--base-z-mm", "0",
                        "--opening-type", "door", "--output", plan_path]) == 0
    capsys.readouterr()
    return plan_path, read_json(plan_path)


@pytest.mark.parametrize("evidence", ["missing", "stale", "changed", "partial"])
def test_normalized_opening_sync_requires_complete_fresh_second_read(tmp_path, capsys, evidence):
    case = opening_case(tmp_path)
    plan_path, plan = make_opening_plan(case, tmp_path, capsys)
    assert "live_cad_revision" not in plan["source"]
    assert plan["source"]["sync_selection"]["selection_snapshot"] == read_json(case["selection"])
    current = None
    if evidence != "missing":
        current = current_selection(case, tmp_path, changed=evidence == "changed")
        picked = read_json(current)
        if evidence == "stale":
            picked["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        elif evidence == "partial":
            picked["returned_count"] = 0
        write_json(current, picked)
    receipt = tmp_path / "opening-receipt.json"
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("must reject before bridge")):
        assert run_cli(apply_args(plan_path, plan, receipt, current)) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    assert not receipt.exists()


def test_normalized_opening_sync_accepts_verified_second_read(tmp_path, capsys):
    case = opening_case(tmp_path)
    plan_path, plan = make_opening_plan(case, tmp_path, capsys)
    receipt = tmp_path / "opening-receipt.json"
    with patch("dwg_semantic.sketchup_live._require_current_bridge"), \
         patch("dwg_semantic.sketchup_live._call", return_value={"status": "applied"}) as call:
        assert run_cli(apply_args(plan_path, plan, receipt, current_selection(case, tmp_path))) == 0
    assert call.call_count == 1 and call.call_args.args[0] == "apply_live_repair"
    assert read_json(receipt)["status"] == "applied"
    capsys.readouterr()


def test_sync_observation_cannot_be_changed_without_invalidating_plan_digest(tmp_path, capsys):
    case = opening_case(tmp_path)
    plan_path, plan = make_opening_plan(case, tmp_path, capsys)
    plan["source"]["sync_selection"]["geometry_signature"] = "forged"
    write_json(plan_path, plan)
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("tampered plan cannot reach bridge")):
        assert run_cli(apply_args(plan_path, plan, tmp_path / "receipt.json", current_selection(case, tmp_path))) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"


@pytest.mark.parametrize("evidence", ["stale", "partial"])
def test_normalized_opening_invalid_read_stops_before_full_model_inspection(tmp_path, capsys, evidence):
    case = opening_case(tmp_path)
    picked = read_json(case["selection"])
    if evidence == "stale":
        picked["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    else:
        picked["returned_count"] = 0
    write_json(case["selection"], picked)
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", side_effect=AssertionError("invalid CAD must stop first")):
        assert run_cli(["plan", case["job"], "--full-model", "--selection-json", case["selection"],
                        "--action", "cut_opening", "--height-mm", "2100", "--output", tmp_path / "plan.json"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_remove_wrong_uses_explicit_model_selection_without_requiring_cad_selection(tmp_path, capsys):
    case = bind_case(tmp_path)
    case["state"]["selection_entity_ids"] = ["entity:101"]
    plan_path = tmp_path / "remove-plan.json"
    with patch("dwg_semantic.sketchup_live.inspect_active_white_model", return_value=case["state"]):
        assert run_cli(["plan", case["job"], "--full-model", "--action", "remove_wrong", "--output", plan_path]) == 0
    plan = read_json(plan_path)
    assert [row["entity_id"] for row in plan["targets"]] == ["entity:101"]
    assert not plan["create"] and "sync_selection" not in plan["source"]
    with patch("dwg_semantic.sketchup_live._require_current_bridge"), \
         patch("dwg_semantic.sketchup_live._call", return_value={"status": "applied"}) as call:
        assert run_cli(apply_args(plan_path, plan, tmp_path / "remove-receipt.json")) == 0
    assert call.call_count == 1
    capsys.readouterr()


def test_doctor_is_offline_and_does_not_require_autocad_or_open_model(capsys):
    with patch("socket.create_connection", side_effect=AssertionError("doctor must be offline")), \
         patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("doctor cannot inspect apps")):
        assert run_cli(["doctor"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)


@pytest.mark.parametrize("command", ["build", "approve", "revise", "inspect", "review"])
def test_sync_cli_does_not_expose_initial_modeling_commands(command, capsys):
    with pytest.raises(SystemExit) as error:
        run_cli([command])
    assert error.value.code == 2
    capsys.readouterr()


def test_apply_cli_requires_a_receipt_destination(capsys):
    with pytest.raises(SystemExit) as error:
        run_cli(["apply", "nonexistent-plan.json", "--confirm-sha256", "anything"])
    assert error.value.code == 2
    assert "--receipt" in capsys.readouterr().err


def test_importing_standalone_sync_does_not_load_initial_modeling_pipeline(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import dwg_semantic.sync; "
         "assert 'dwg_semantic.pipeline' not in sys.modules; print('independent')"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "independent"


@pytest.mark.parametrize("ordinary", [False, True])
def test_reusing_plan_time_snapshot_as_current_read_stops_before_bridge(tmp_path, capsys, ordinary):
    case = opening_case(tmp_path) if ordinary else bind_case(tmp_path)
    plan_path, plan = (make_opening_plan(case, tmp_path, capsys) if ordinary
                       else make_plan(case, tmp_path, capsys))
    with patch("dwg_semantic.sketchup_live._call", side_effect=AssertionError("reused snapshot cannot reach bridge")):
        assert run_cli(apply_args(plan_path, plan, tmp_path / "receipt.json", case["selection"])) == 2
    assert "重新讀取" in json.loads(capsys.readouterr().err)["message"]
    assert not (tmp_path / "receipt.json").exists()
