"""Narrow, installed DWG/SKP sync entry point; never invokes initial modeling."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def doctor() -> dict:
    """Check local shared dependencies without connecting to either application."""
    from .live_workflow import _live_bridge_source
    from .live_repair import PLAN_SCHEMA, SNAPSHOT_SCHEMA
    from .modeling_standard import VERSION
    import shapely

    return {
        "status": "ready", "skill": "dwg-skp-sync",
        "core": "dwg_semantic", "shapely_version": shapely.__version__,
        "bridge_source": str(_live_bridge_source()),
        "bridge_protocol_version": "2.0", "fixed_modeling_standard_version": VERSION,
        "contract": {
            "document": "existing job/document.json (immutable)",
            "snapshot_schema": SNAPSHOT_SCHEMA, "plan_schema": PLAN_SCHEMA,
            "wall_system_scope_schema": "cad-wall-system-scope-v1",
            "live_cad_revision": "separate plan.source.live_cad_revision overlay",
            "selection_max_age_seconds": 30,
            "snapshot_mode": "local_geometry", "component_index": False,
            "inventory_mode": "stateless_actual_bounds", "partial_snapshot_version": "1.0",
            "whole_model_sync_version": "1.0", "mode": "selection_if_nonempty_else_complete_model_difference",
            "slab_sync_version": "1.0", "slab_scope": "whole_model_only_source_bound_whole_shell",
        },
        "applications_contacted": False, "model_modified": False,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="dwg-skp-sync", description="dwg-skp-sync：有選取局部同步，無選取整模比對並僅更新差異")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check local shared runtime without contacting CAD or SketchUp")
    status = sub.add_parser("status", help="Read the current SketchUp bridge health")
    status.add_argument("--port", type=int, default=51236)
    install = sub.add_parser("install-bridge", help="Explicit bridge maintenance; never restarts SketchUp")
    install.add_argument("--sketchup-version")
    cad_read=sub.add_parser('cad-read',help='Read complete current CAD database without selecting or saving')
    cad_read.add_argument('job',type=Path)
    cad_read.add_argument('--output',type=Path,required=True)
    plan = sub.add_parser("plan", help="Inspect and plan a bounded sync without editing the model")
    plan.add_argument("job", type=Path)
    plan.add_argument("--selection-json", type=Path)
    plan.add_argument('--mode',choices=['auto','selection','model'],default='auto')
    plan.add_argument('--cad-model-json',type=Path)
    plan.add_argument("--action", choices=["cut_opening", "add_missing", "replace_from_cad", "remove_wrong"],
                      default="replace_from_cad")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--height-mm", type=float)
    plan.add_argument("--base-z-mm", type=float)
    plan.add_argument("--category", choices=["architectural_wall", "structural_wall", "column", "curtain", "curtain_accessory"])
    plan.add_argument("--tag")
    plan.add_argument("--opening-type", choices=["opening", "door", "window"], default="opening")
    plan.add_argument("--target-entity-id", action="append", default=[])
    plan.add_argument("--port", type=int, default=51236)
    plan.add_argument("--live-cad-geometry", action="store_true")
    plan.add_argument("--auto-match-position", action="store_true")
    plan.add_argument("--wall-system-scope-json", type=Path)
    plan.add_argument("--full-model", action="store_true", help="Explicit legacy full-read diagnostics; default reads local geometry only")
    apply = sub.add_parser("apply", help="Apply one reviewed hash-bound plan as a single Undo; no save")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--confirm-sha256", required=True)
    apply.add_argument("--receipt", type=Path, required=True)
    apply.add_argument("--port", type=int, default=51236)
    apply.add_argument("--current-selection-json", type=Path)
    apply.add_argument('--current-cad-model-json',type=Path)
    args = parser.parse_args(argv)
    try:
        from .live_workflow import _live_bridge_source, live_repair_plan_job, live_repair_apply_job

        if args.command == "doctor":
            result = doctor()
        elif args.command == "status":
            from .sketchup_live import live_status
            result = live_status(port=args.port)
        elif args.command == "install-bridge":
            from .sketchup_live import install_live_bridge
            result = install_live_bridge(_live_bridge_source(), sketchup_version=args.sketchup_version)
        elif args.command=='cad-read':
            from .cad_model import capture_model
            from .workflow_io import _read
            document=_read(args.job/'document.json')
            value=capture_model(document,args.output,reader_directory=args.job/'.sync-reader-cache')
            result={'status':'ready','snapshot':str(args.output.resolve()),'observed_at_utc':value['observed_at_utc'],
                'entity_count':value['entity_count'],'resolved_instance_count':value['resolved_instance_count'],
                'selection_count':value['selection_count'],'source_modified':False}
        elif args.command == "plan":
            from .workflow_io import _read
            selected=_read(args.selection_json)if args.selection_json is not None else None
            whole=args.mode=='model' or (args.mode=='auto' and args.action!='remove_wrong' and
                isinstance(selected,dict) and selected.get('selection_count')==0)
            if whole:
                if args.action!='replace_from_cad' or args.full_model or args.live_cad_geometry or args.auto_match_position or args.wall_system_scope_json or args.target_entity_id or any(v is not None for v in (args.category,args.tag,args.height_mm,args.base_z_mm)):
                    raise ValueError('整模差異同步不接受局部目標、動作、高度覆寫或舊版完整讀取旗標。')
                from .live_workflow import live_model_plan_job
                result=live_model_plan_job(args.job,args.output,selection_path=args.selection_json,
                    cad_model_path=args.cad_model_json,port=args.port)
            else:
                if args.cad_model_json is not None:raise ValueError('有 CAD 選取時維持局部同步，不接受整模快照擴大範圍。')
                result = live_repair_plan_job(
                args.job, args.output, action=args.action, selection_path=args.selection_json,
                height_mm=args.height_mm, base_z_mm=args.base_z_mm, category=args.category,
                tag=args.tag, opening_type=args.opening_type, target_entity_ids=args.target_entity_id,
                port=args.port, live_cad_geometry=args.live_cad_geometry,
                auto_match_position=args.auto_match_position, wall_system_scope_path=args.wall_system_scope_json,
                require_current_selection=True,
                partial_snapshot=not args.full_model,
            )
        else:
            result = live_repair_apply_job(
                args.plan, confirmation_sha256=args.confirm_sha256, receipt_path=args.receipt,
                port=args.port, current_selection_path=args.current_selection_json,
                current_cad_model_path=args.current_cad_model_json,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in {"blocked", "fail", "error"} else 0
    except (ValueError, RuntimeError, OSError, ImportError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
