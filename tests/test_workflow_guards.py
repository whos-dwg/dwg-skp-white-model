"""Portable workflow and existing safety-guard tests.

Every job/source/approval here is an explicitly synthetic isolated fixture.
No active CAD/SketchUp application, real drawing, or real approval is used.
"""
from __future__ import annotations
import builtins
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest
from dwg_semantic.classifier import classify_document, load_rules
from dwg_semantic.pipeline import (WorkflowError, _read, _write, approve_job, attach_selection_openings_job,
    build_job, default_rules_path, digest_file, doctor, inventory_layers, review_job)
from dwg_semantic.skp_native import Native
from dwg_semantic.sketchup_live import SketchUpLiveError, install_live_bridge

ROOT=Path(__file__).resolve().parents[1]
SKILL=Path(os.environ.get("DWG_TEST_SKILL", str(ROOT/"skills/dwg-layered-skp")))
SYNTHETIC_CONFIRMATION="SYNTHETIC TEST FIXTURE ONLY: approve isolated sample geometry, not a real user/model decision"


def make_job(root, *, approve=True, build=False):
    root.mkdir(parents=True,exist_ok=True)
    source=root/"synthetic-source.dwg"
    source.write_bytes(b"SYNTHETIC BYTE FIXTURE ONLY - never passed to CAD decoder")
    doc={"intake":{"source_path":str(source),"source_sha256":digest_file(source),"units":"mm",
                   "scale_to_mm":1,"blockers":[],"dependencies":[]},"entities":[
        {"entity_id":"source::W1","handle":"W1","source_handle":"W1","source_file":str(source),
         "type":"LWPOLYLINE","layer":"A-WALL","original_layer":"A-WALL","effective_layer":"A-WALL",
         "owner_block":"*Model_Space","closed":True,
         "vertices":[{"x":x,"y":y,"z":0}for x,y in [(0,0),(4000,0),(4000,200),(0,200)]]},
        {"entity_id":"source::D1","handle":"D1","source_handle":"D1","source_file":str(source),
         "type":"INSERT","layer":"A-DOOR","original_layer":"A-DOOR","effective_layer":"A-DOOR",
         "owner_block":"*Model_Space","instance_path":""}],"resolved_instances":[]}
    config={"source_sha256":digest_file(source),"source_tag_policy":"effective",
        "layer_mapping":{"A-WALL":{"category":"architectural_wall","height_mm":3000,"base_z_mm":0,
                                      "footprint_confirmed":True},
                         "A-DOOR":{"category":"reference","reason":"Synthetic selected source retained for later patch"}},
        "object_overrides":{}}
    rules=load_rules(default_rules_path())
    classification=classify_document(doc,rules,source_name=source.name)
    job=root/"base-job"
    job.mkdir()
    for name,data in {"document.json":doc,"classification.json":classification,"decisions.json":config,
                      "rules.json":rules,"layers.json":inventory_layers(doc,classification),"job.json":{"synthetic":True}}.items():
        _write(job/name,data)
    review=review_job(job)
    assert review["status"]=="ready_for_confirmation",review["blockers"]
    if approve:
        approve_job(job,confirmation=SYNTHETIC_CONFIRMATION,expected_review=review["review_sha256"],
                    approved_by="isolated-synthetic-test-harness")
    if build:
        build_job(job)
    selection={"success":True,"schema_version":"1.0","status":"SELECTED",
        "document":{"name":source.name,"path":str(source)},
        "selection_source":"PickfirstSelectionSet","selection_scope":"top-level selected entities",
        "selection_count":1,"returned_count":1,"truncated":False,
        "objects":[{"selection_index":0,"handle":"D1","object_type":"AcDbBlockReference",
                    "properties":{"Layer":"A-DOOR"},
                    "bounding_box":{"coordinate_system":"WCS","minimum":[1000,-100,0],"maximum":[1900,300,0]}}]}
    selected=root/"synthetic-selected.json"
    _write(selected,selection)
    return job,source,selected


def test_portable_bundle_runs_doctor_and_json_inspect_from_an_unrelated_cwd(tmp_path):
    copied=tmp_path/"moved-skill"
    shutil.copytree(SKILL,copied,
        ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    unrelated=tmp_path/"arbitrary-cwd"
    unrelated.mkdir()
    env=os.environ.copy()
    env.pop("PYTHONPATH",None)
    env.pop("DWG_SKP_DEPENDENCIES",None)
    env["PYTHONDONTWRITEBYTECODE"]="1"
    entry=copied/"scripts/dwg_to_skp.py"
    done=subprocess.run([sys.executable,str(entry),"doctor"],cwd=unrelated,env=env,capture_output=True,text=True,
                        encoding="utf-8",timeout=60)
    assert done.returncode==0,done.stdout+done.stderr
    health=json.loads(done.stdout)
    assert Path(health["rules"]).is_relative_to(copied)
    source=tmp_path/"synthetic-normalized.json"
    source.write_text(json.dumps({"units":"mm","entities":[{"type":"LWPOLYLINE","handle":"W","layer":"A-WALL",
        "owner_block":"*Model_Space","closed":True,
        "vertices":[{"x":x,"y":y,"z":0}for x,y in [(0,0),(1000,0),(1000,200),(0,200)]]}]}),encoding="utf-8")
    job=tmp_path/"portable-json-job"
    inspected=subprocess.run([sys.executable,str(entry),"inspect",str(source),"--job",str(job),"--backend","json"],
        cwd=unrelated,env=env,capture_output=True,text=True,encoding="utf-8",timeout=60)
    assert inspected.returncode in {0,2},inspected.stdout+inspected.stderr
    assert inspected.stdout.strip(), inspected.stderr
    result=json.loads(inspected.stdout)
    assert result["status"]=="blocked","Missing synthetic heights must not be silently invented"
    assert all((job/name).is_file()for name in ("document.json","classification.json","layers.json","decisions.json",
        "model.plan.json","review.json","review.md","review.svg","recognition.json"))
    assert not list(job.rglob("*.skp"))
    assert not (job/"approval.json").exists()
    before={path.relative_to(job).as_posix():digest_file(path)for path in job.rglob('*')if path.is_file()}
    repeated=subprocess.run([sys.executable,str(entry),"inspect",str(source),"--job",str(job),"--backend","json"],
        cwd=unrelated,env=env,capture_output=True,text=True,encoding="utf-8",timeout=60)
    assert repeated.returncode==2
    assert json.loads(repeated.stderr)["status"]=="error"
    assert before=={path.relative_to(job).as_posix():digest_file(path)for path in job.rglob('*')if path.is_file()}


def test_doctor_reports_missing_geometry_cad_sdk_and_native_dependencies_without_hiding_cause():
    real_import=builtins.__import__
    def without_shapely(name,*args,**kwargs):
        if name=="shapely":
            raise ImportError("synthetic missing shapely")
        return real_import(name,*args,**kwargs)
    with patch("builtins.__import__",side_effect=without_shapely),patch("dwg_semantic.intake._find_autocad",return_value=None), \
         patch("shutil.which",return_value=None),patch("dwg_semantic.skp_native.Native",
             side_effect=FileNotFoundError("synthetic missing SketchUpAPI.dll")):
        result=doctor()
    assert result["autocad"]is None and result["dotnet"]is None
    assert "synthetic missing shapely"in result["shapely"]
    assert "synthetic missing SketchUpAPI.dll"in result["sketchup"]


def test_native_rejects_non_main_thread_before_loading_library():
    errors=[]
    def worker():
        try:
            Native()
        except Exception as error:
            errors.append(error)
    thread=threading.Thread(target=worker)
    with patch("ctypes.CDLL",side_effect=AssertionError("Native library must not load on worker thread")):
        thread.start()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors)==1 and isinstance(errors[0],RuntimeError)
    assert "main"in str(errors[0]).lower()or "主"in str(errors[0])


@pytest.mark.parametrize("filename",["recognition.json","recognition.md","recognition-plan.svg","recognition.html"])
def test_every_reviewed_recognition_artifact_change_invalidates_approval(tmp_path,filename):
    job,_,_=make_job(tmp_path,approve=False)
    review=_read(job/"review.json")
    assert filename in review["presentation_hashes"]
    artifact=job/filename
    artifact.write_text(artifact.read_text(encoding="utf-8")+"\nSYNTHETIC TAMPER",encoding="utf-8")
    with pytest.raises(WorkflowError,match="不一致|改變"):
        approve_job(job,confirmation=SYNTHETIC_CONFIRMATION,expected_review=review["review_sha256"],
                    approved_by="isolated-synthetic-test-harness")
    assert not (job/"approval.json").exists()


@pytest.mark.skipif(os.environ.get("DWG_RUN_NATIVE_TESTS") != "1", reason="Opt-in actual SketchUp C API integration")
def test_selection_attach_job_highlight_reapproval_and_actual_native_build(tmp_path):
    parent,source,selected=make_job(tmp_path,approve=True,build=True)
    old_approval=digest_file(parent/"approval.json")
    old_model=digest_file(parent/"delivery/model.skp")
    revised=tmp_path/"selected-opening-job"
    review=attach_selection_openings_job(parent,revised,selected,height_mm=2100,base_z_mm=0,opening_type="door")
    assert review["status"]=="ready_for_confirmation",review["blockers"]
    assert not (revised/"approval.json").exists()
    assert "opening-selection.json"in review["input_hashes"]
    assert "CAD selection opening D1"in(revised/"review.svg").read_text(encoding="utf-8")
    assert "D1"in(revised/"review.md").read_text(encoding="utf-8")
    plan=_read(revised/"model.plan.json")
    assert len(plan["selection_opening_audit"])==1
    with pytest.raises(WorkflowError,match="核定"):
        build_job(revised)
    approve_job(revised,confirmation=SYNTHETIC_CONFIRMATION,expected_review=review["review_sha256"],
                approved_by="isolated-synthetic-test-harness")
    qa=build_job(revised)
    assert qa["all_pass"]
    assert any(row["check"]=="saved opening remains clear" and row["pass"]for row in qa["checks"])
    assert digest_file(parent/"approval.json")==old_approval
    assert digest_file(parent/"delivery/model.skp")==old_model
    assert source.read_bytes()==b"SYNTHETIC BYTE FIXTURE ONLY - never passed to CAD decoder"


def test_all_emitted_recognition_formats_including_optional_images_are_hash_bound(tmp_path):
    job,_,_=make_job(tmp_path,approve=False)
    review=_read(job/"review.json")
    formats=[name for name in review["review_files"]if name.startswith("recognition")]
    assert formats
    for name in formats:
        assert name in review["presentation_hashes"],name
        path=job/name
        original=path.read_bytes()
        path.write_bytes(original+b"\nSYNTHETIC BYTE TAMPER")
        try:
            with pytest.raises(WorkflowError,match="不一致|改變"):
                approve_job(job,confirmation=SYNTHETIC_CONFIRMATION,expected_review=review["review_sha256"],
                            approved_by="isolated-synthetic-test-harness")
        finally:
            path.write_bytes(original)
    assert not (job/"approval.json").exists()


def test_selection_attach_rejects_a_parent_without_actual_version_approval(tmp_path):
    parent,_,selected=make_job(tmp_path,approve=False)
    with pytest.raises(WorkflowError,match="核定|approval|approved"):
        attach_selection_openings_job(parent,tmp_path/"unapproved-revision",selected,height_mm=2100)


def test_selection_attach_rejects_changed_parent_model_and_changed_main_source(tmp_path):
    parent,source,selected=make_job(tmp_path,approve=True)
    plan=_read(parent/"model.plan.json")
    plan["groups"][0]["height_mm"]+=100
    _write(parent/"model.plan.json",plan)
    with pytest.raises(WorkflowError,match="不一致|改變|review|核定"):
        attach_selection_openings_job(parent,tmp_path/"changed-plan-revision",selected,height_mm=2100)
    # Create a separate fresh parent so the source-drift assertion is not masked
    # by the deliberately stale plan above.
    fresh,source2,selected2=make_job(tmp_path/"second",approve=True)
    source2.write_bytes(b"SYNTHETIC SOURCE CHANGED")
    with pytest.raises(WorkflowError,match="來源|改變|source"):
        attach_selection_openings_job(fresh,tmp_path/"changed-source-revision",selected2,height_mm=2100)


def test_isolated_appdata_installer_changes_only_owned_bridge_and_never_models(tmp_path,monkeypatch):
    appdata=tmp_path/"isolated-appdata"
    plugins=appdata/"SketchUp/SketchUp 2021/SketchUp/Plugins"
    plugins.mkdir(parents=True)
    sentinel=plugins/"unrelated-plugin.rb"
    sentinel.write_text("# unrelated plugin remains unchanged",encoding="utf-8")
    model=tmp_path/"never-touch.skp"
    model.write_bytes(b"SYNTHETIC MODEL SENTINEL")
    before_model=model.read_bytes()
    monkeypatch.setenv("APPDATA",str(appdata))
    bridge=ROOT/"skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb"
    result=install_live_bridge(bridge,sketchup_version="2021")
    assert result["restart_required"]is True
    assert result["files"]==[str(plugins/"dwg_live_repair_bridge.rb")]
    assert (plugins/"dwg_live_repair_bridge.rb").read_bytes()==bridge.read_bytes()
    assert sentinel.read_text(encoding="utf-8")=="# unrelated plugin remains unchanged"
    assert model.read_bytes()==before_model
    assert {p.name for p in plugins.iterdir()}=={"unrelated-plugin.rb","dwg_live_repair_bridge.rb"}
    (plugins/"dwg_live_repair_bridge.rb").write_text("# third-party file with no identity marker",encoding="utf-8")
    with pytest.raises(SketchUpLiveError,match="拒絕覆寫"):
        install_live_bridge(bridge,sketchup_version="2021")
    assert (plugins/"dwg_live_repair_bridge.rb").read_text(encoding="utf-8")=="# third-party file with no identity marker"


def test_installer_does_not_create_a_missing_sketchup_installation(tmp_path,monkeypatch):
    monkeypatch.setenv("APPDATA",str(tmp_path/"empty-appdata"))
    bridge=ROOT/"skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb"
    with pytest.raises(SketchUpLiveError,match="找不到"):
        install_live_bridge(bridge,sketchup_version="2021")
    assert not (tmp_path/"empty-appdata").exists()

def test_live_bridge_binds_loopback_and_dispatches_only_narrow_whitelist_without_real_listener():
    install=Path(os.environ.get("DWG_AUDIT_RUBY_HOME","C:/Program Files/SketchUp/SketchUp 2021"))
    if not list(install.glob("*ruby*.dll")):
        pytest.skip("Isolated installed Ruby DLL unavailable; no live application used")
    here=Path(__file__).resolve().parent
    env=os.environ.copy()
    env["DWG_AUDIT_BRIDGE"]=str(ROOT/"skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb")
    env["PYTHONIOENCODING"]="utf-8"
    runner=ROOT/"tests/native_safety/embedded_ruby.py"
    process=subprocess.run([sys.executable,str(runner),str(here/"ruby_dispatch_guard.rb")],
        cwd=here,env=env,capture_output=True,text=True,encoding="utf-8",timeout=30)
    assert process.returncode==0,process.stdout+process.stderr
    result=json.loads(next(line for line in process.stdout.splitlines()if line.startswith('{')))
    assert result["bind"]==["127.0.0.1",51236]
    for name in ("health","inspect_white_model","apply_live_repair"):
        assert result["results"][name]["ok"]is True
    for name in ("eval","ruby","execute_script","run_script","save","save_as","delete_all"):
        row=result["results"][name]
        assert row=={"ok":False,"code":"UNSUPPORTED_OPERATION","socket_closed":True}
    assert result["forbidden_script_executed"]is False
    assert result["root_rules"]["one"]=={"accepted":True}
    assert result["root_rules"]["none"]=={"accepted":False,"code":"WHITE_MODEL_ROOT_AMBIGUOUS"}
    assert result["root_rules"]["two"]=={"accepted":False,"code":"WHITE_MODEL_ROOT_AMBIGUOUS"}

def test_replace_from_cad_without_new_dimensions_preserves_unique_target_verticals_category_and_tag():
    from dwg_semantic.live_repair import build_live_repair_plan
    from test_live_repair import document, selection, snapshot, live_entity
    current=live_entity(source_handle="OLD",height=3470)
    current["base_z_mm"]=125
    current["provenance"]["base_z_mm"]=125
    plan=build_live_repair_plan(document(exact_door=True),selection(),
        snapshot([current],selection=["entity:101"]),action="replace_from_cad")
    assert plan["status"]=="ready_to_apply"
    assert len(plan["create"])==1
    created=plan["create"][0]
    assert created["base_z_mm"]==125
    assert created["height_mm"]==3470
    assert created["category"]=="architectural_wall"
    assert created["tag"]=="A-WALL"
    assert plan["write_policy"]["save_model"]is False
    assert plan["write_policy"]["create_new_skp"]is False


@pytest.mark.parametrize("field",["path","guid"])
def test_live_plan_rejects_unsaved_or_unidentified_current_model(field):
    from dwg_semantic.live_repair import build_live_repair_plan,LiveRepairError
    from test_live_repair import document,selection,snapshot
    current=snapshot()
    current["model"][field]=""
    with pytest.raises(LiveRepairError,match="GUID|儲存|saved"):
        build_live_repair_plan(document(),selection(),current,action="cut_opening",height_mm=2100)


@pytest.mark.parametrize("axis,value",[("xaxis",[0,1,0]),("zaxis",[0,0,2])])
def test_live_plan_rejects_rotated_or_scaled_source_root(axis,value):
    from dwg_semantic.live_repair import build_live_repair_plan,LiveRepairError
    from test_live_repair import document,selection,snapshot
    current=snapshot()
    current["root"]["world_transform"][axis]=value
    with pytest.raises(LiveRepairError,match="根群組|root"):
        build_live_repair_plan(document(),selection(),current,action="cut_opening",height_mm=2100)
