"""Published active-model sync actions shared by both command entry points.

No initial intake/review/build pipeline is imported here. Geometry, binding,
source freshness and transactional readback remain in the existing core.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .workflow_io import WorkflowError, _read, _source_freshness


def _live_bridge_source() -> Path:
    module = Path(__file__).resolve()
    candidates = (
        module.parents[2] / "skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb",
        module.parents[2] / "sketchup/dwg_live_repair_bridge.rb",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise WorkflowError("找不到隨附的 SketchUp 即時修正橋接器。")


def live_repair_plan_job(job: Path | str, output: Path | str, *, action: str,
                         selection_path: Path | str | None = None,
                         height_mm: float | None = None, base_z_mm: float | None = None,
                         category: str | None = None, tag: str | None = None,
                         opening_type: str = "opening",
                         target_entity_ids: list[str] | None = None,
                         live_cad_geometry: bool = False, auto_match_position: bool = False,
                         wall_system_scope_path: Path | str | None = None,
                         require_current_selection: bool = False,
                         partial_snapshot: bool = False,
                         port: int = 51236) -> dict:
    """Inspect the active model and persist a no-write, hash-bound repair plan."""
    from .live_repair import build_live_repair_plan
    from .sketchup_live import inspect_active_white_model

    job, output = Path(job).resolve(), Path(output).resolve()
    if output.exists():
        raise WorkflowError("即時修正計畫檔已存在；拒絕覆寫：" + str(output))
    document_path = job / "document.json"
    if not document_path.is_file():
        raise WorkflowError("工作資料夾缺少 document.json。")
    document = _read(document_path)
    freshness = _source_freshness(document)
    if freshness:
        raise WorkflowError("即時修正來源已失效：" + "; ".join(freshness))
    selection = None
    if selection_path is not None:
        selection_path = Path(selection_path).resolve()
        if not selection_path.is_file():
            raise WorkflowError("找不到 AutoCAD 選取快照：" + str(selection_path))
        selection = _read(selection_path)
    needs_fresh = live_cad_geometry or (require_current_selection and action != 'remove_wrong')
    if needs_fresh:
        from .live_cad_geometry import require_fresh_selection
        require_fresh_selection(selection or {})
        from .selection_openings import _selection_errors
        _selection_errors(selection, document)
    wall_scope = _read(Path(wall_system_scope_path)) if wall_system_scope_path is not None else None
    query = None
    if partial_snapshot:
        from .local_snapshot import make_local_query, validate_local_snapshot
        query = make_local_query(document, selection, action=action, target_entity_ids=target_entity_ids,
                                 live_cad_geometry=live_cad_geometry, auto_match_position=auto_match_position,
                                 wall_system_scope=wall_scope)
    snapshot = inspect_active_white_model(port=port, **({'local_query': query} if query is not None else {}))
    if partial_snapshot:
        validate_local_snapshot(snapshot, query)
    if needs_fresh:
        require_fresh_selection(selection)
    plan = build_live_repair_plan(
        document, selection, snapshot, action=action,
        height_mm=height_mm, base_z_mm=base_z_mm,
        category=category, tag=tag, opening_type=opening_type,
        target_entity_ids=target_entity_ids,live_cad_geometry=live_cad_geometry,
        auto_match_position=auto_match_position,
        **({'wall_system_scope': wall_scope} if wall_scope is not None else {}),
    )
    if require_current_selection and action != 'remove_wrong':
        from .live_cad_geometry import selection_geometry_signature
        from .live_repair import seal_live_repair_plan
        plan['source']['sync_selection'] = {
            'geometry_signature': selection_geometry_signature(selection),
            'selection_snapshot': deepcopy(selection),
        }
        seal_live_repair_plan(plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return {
        "status": "ready_to_apply",
        "plan": str(output),
        "plan_sha256": plan["plan_sha256"],
        "action": plan["action"],
        "target_count": len(plan["targets"]),
        "create_count": len(plan["create"]),
        "active_model": plan["model"],
        "new_skp_created": False,
        "model_modified": False,
        "read_stats": snapshot.get('read_stats', {}),
        "snapshot_mode": 'local_geometry' if partial_snapshot else 'full_model',
    }


def live_repair_apply_job(plan_path: Path | str, *, confirmation_sha256: str,
                          receipt_path: Path | str | None = None, port: int = 51236,
                          current_selection_path: Path | str | None = None,
                          current_cad_model_path: Path | str | None = None) -> dict:
    """Apply one confirmed plan to the active model without saving or creating an SKP."""
    from .sketchup_live import apply_live_repair_plan

    plan_path = Path(plan_path).resolve()
    if not plan_path.is_file():
        raise WorkflowError("找不到即時修正計畫：" + str(plan_path))
    plan = _read(plan_path)
    if receipt_path is not None:
        receipt_path = Path(receipt_path).resolve()
        if receipt_path.exists():
            raise WorkflowError("即時修正收據檔已存在；拒絕覆寫：" + str(receipt_path))
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        # Verify the destination before the remote operation. Never discover a
        # permissions/overwrite error only after committing the live model.
        import tempfile
        with tempfile.TemporaryFile(dir=receipt_path.parent):
            pass
    freshness = _source_freshness({"intake": plan.get("source", {}).get("manifest", {
        "source_path": plan.get("source", {}).get("path"),
        "source_sha256": plan.get("source", {}).get("sha256")})})
    if freshness:
        raise WorkflowError("即時修正來源已失效：" + "; ".join(freshness))
    current_selection=_read(Path(current_selection_path)) if current_selection_path is not None else None
    current_model=_read(Path(current_cad_model_path)) if current_cad_model_path is not None else None
    receipt = apply_live_repair_plan(plan, confirmation_sha256=confirmation_sha256, port=port,
                                    **({'current_cad_model':current_model}if plan.get('source',{}).get('sync_model')else{}),
                                    **({'current_selection':current_selection}if (
                                        plan.get('source',{}).get('live_cad_revision') or
                                        plan.get('source',{}).get('sync_selection'))else{}))
    if receipt_path is not None:
        try:
            # Exclusive creation also handles a destination appearing during
            # the remote call. Preserve the completed-operation receipt even
            # if persistence fails; never invite an unsafe retry of the edit.
            with receipt_path.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
        except OSError as error:
            receipt = {**receipt, "receipt_persistence_error": str(error), "receipt_requested_path": str(receipt_path)}
    return receipt


def live_model_plan_job(job,output,*,selection_path,cad_model_path=None,port=51236):
    """An actual empty selection routes here; missing evidence never does."""
    from .cad_model import require_empty_selection,capture_model,validate_model_snapshot
    from .model_sync import build_model_sync_plan
    from .sketchup_live import inspect_active_white_model
    job,output=Path(job).resolve(),Path(output).resolve()
    if output.exists():raise WorkflowError('整模同步計畫已存在；拒絕覆寫。')
    document=_read(job/'document.json')
    empty=_read(Path(selection_path)) if selection_path is not None else None
    require_empty_selection(empty,document)
    if cad_model_path is not None:cad=_read(Path(cad_model_path))
    else:
        cad_model_path=output.with_name(output.stem+'-cad-model.json')
        cad=capture_model(document,cad_model_path,reader_directory=job/'.sync-reader-cache')
    validate_model_snapshot(cad,document)
    snapshot=inspect_active_white_model(whole_model=True,port=port)
    decisions=_read(job/'decisions.json')if(job/'decisions.json').is_file()else{}
    plan=build_model_sync_plan(document,cad,snapshot,slab_decisions=decisions.get('slabs'))
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8')as f:json.dump(plan,f,ensure_ascii=False,indent=2,allow_nan=False)
    return {'status':plan['status'],'plan':str(output),'plan_sha256':plan['plan_sha256'],'action':plan['action'],
        'target_count':len(plan['targets']),'create_count':len(plan['create']),
        'difference_summary':{k:v for k,v in plan['difference_summary'].items()if not isinstance(v,list)},
        'new_skp_created':False,'model_modified':False,'snapshot_mode':'whole_model','read_stats':snapshot['read_stats'],
        'cad_model_snapshot':str(cad_model_path)}

