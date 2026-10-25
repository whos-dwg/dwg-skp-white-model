"""DWG -> review -> explicit approval -> source-layer SKP.

An approval is a record of a human decision, not a command that an agent may
invent. The exact reviewed inputs and prepared geometry are hashed and checked
again before the native writer is called.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

from .classifier import classify_document, load_rules
from .geometry import entity_segments

SCHEMA = "dwg-skp-review-v2"
INPUT_FILES = ("document.json", "classification.json", "decisions.json", "rules.json", "model.plan.json")
CATEGORIES = ("architectural_wall", "structural_wall", "column", "curtain", "curtain_accessory", "slab", "opening", "reference", "exclude")
COLORS = {"architectural_wall": "#ce5882", "structural_wall": "#cd912d", "column": "#168b76", "curtain": "#3376bd", "curtain_accessory": "#728d96", "slab": "#538caa", "opening": "#77b3d7", "reference": "#adb4bf", "exclude": "#d2d5da", None: "#c34238"}


from .workflow_io import WorkflowError, _read, _write, digest_file, _source_freshness


def digest_data(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _runtime_digest() -> str:
    root = Path(__file__).parent
    # Any geometry/interpretation code change invalidates an old review.
    return digest_data({p.relative_to(root).as_posix(): digest_file(p) for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".py", ".cs", ".csproj"}})


def default_rules_path() -> Path:
    package = Path(__file__).parent
    for candidate in (package / "config/default_rules.json", package.parent / "config/default_rules.json", package.parents[1] / "config/default_rules.json"):
        if candidate.is_file():
            return candidate
    raise WorkflowError("找不到通用規則；請指定 --rules，或重新安裝完整 skill。")


def _model_entities(document: dict) -> list[dict]:
    from .modeling import model_entities
    return model_entities(document)


def inventory_layers(document: dict, classification: dict) -> list[dict]:
    suggestions: dict[str, set[str]] = defaultdict(set)
    names = {"architectural_walls": "architectural_wall", "structural_walls": "structural_wall", "columns": "column", "curtain_envelopes": "curtain", "curtain_accessories": "curtain_accessory", "openings": "opening"}
    for name, category in names.items():
        for component in classification.get("components", {}).get(name, []):
            layers = component.get("source_layers") or [component.get("source_layer")]
            for layer in layers:
                if layer is not None:
                    suggestions[str(layer)].add(category)
    for component in classification.get("components", {}).get("material_footprints", []):
        for layer in component.get("source_layers", []):
            suggestions[str(layer)].add(component["category"])
    for item in classification.get("audits", {}).get("ignored_fire_protection_objects", []):
        layer = item.get("source_layer")
        if layer is not None:
            suggestions[str(layer)].add("exclude")
    inventory: dict[str, dict] = {}
    for entity in _model_entities(document):
        original = str(entity.get("original_layer", entity.get("layer", "0")))
        effective = str(entity.get("effective_layer") or entity.get("layer") or "0")
        layer = effective if original in {"0", "Layer0"} else original
        item = inventory.setdefault(layer, {"layer": layer, "count": 0, "types": Counter(), "original_layers": set(), "sample_ids": [], "source_files": set()})
        item["count"] += 1
        item["types"][str(entity.get("type") or "UNKNOWN")] += 1
        item["original_layers"].add(str(entity.get("original_layer", entity.get("layer", "0"))))
        item["source_files"].add(str(entity.get("source_file") or document.get("intake", {}).get("source_path") or ""))
        if len(item["sample_ids"]) < 8:
            item["sample_ids"].append(str(entity.get("entity_id") or entity.get("source_id") or entity.get("id") or entity.get("handle") or entity.get("source_handle") or ""))
    result = []
    for layer, item in sorted(inventory.items()):
        options = sorted(suggestions[layer])
        if not options and set(item["types"]) <= {"TEXT", "MTEXT", "DIMENSION", "HATCH", "LEADER", "MLEADER", "POINT"}:
            options = ["reference"]
        item.update(types=dict(item["types"]), original_layers=sorted(item["original_layers"]), source_files=sorted(item["source_files"]), suggested_categories=options, mixed=len(options) > 1)
        result.append(item)
    return result


def _profile_defaults(profile: Path | None) -> dict:
    if not profile:
        return {}
    data = _read(profile)
    defaults = dict(data.get("modeling", data.get("model_defaults", {})))
    if "wall_height_mm" in defaults and "default_height_mm" not in defaults:
        defaults["default_height_mm"] = defaults["wall_height_mm"]
    return defaults


def inspect_source(source: Path | str, job: Path | str, *, rules: Path | None = None, profile: Path | None = None, unit: str | None = None, backend: str = "auto", recognition_decisions: Path | None = None) -> dict:
    from .intake import extract_document
    job = Path(job).resolve()
    if job.exists() and any(job.iterdir()):
        raise WorkflowError("工作資料夾已有內容；請選擇新的資料夾，保留舊核定及交付版本。")
    job.mkdir(parents=True, exist_ok=True)
    resolved_rules = load_rules(rules or default_rules_path(), profile=profile)
    document = extract_document(source, output_dir=job / "extract", backend=backend, unit=unit)
    recognition = _read(recognition_decisions) if recognition_decisions else None
    if document.get("intake", {}).get("blockers"):
        classification = {"schema": "dwg-semantic-classification-blocked", "source_name": Path(source).name, "components": {}, "audits": {}, "counts": {}, "validation": {"status": "fail", "failures": [{"rule": "intake_complete_before_classification", "details": document["intake"]["blockers"]}], "warnings": []}}
    else:
        classification = classify_document(document, resolved_rules, source_name=Path(source).name, recognition_decisions=recognition)
    inventory = inventory_layers(document, classification)
    intake = document.get("intake", {})
    decisions = {"schema": "dwg-skp-decisions-v2", "source_sha256": intake.get("source_sha256"), "default_height_mm": None, "opening_height_mm": None, "source_tag_policy": "effective", "layer_mapping": {}, "object_overrides": {}, "notes": "候選方案；需由使用者確認用途、範圍、尺寸及輪廓後核定。"}
    if recognition:
        decisions["recognition"] = recognition
    for item in inventory:
        options = item["suggested_categories"]
        decisions["layer_mapping"][item["layer"]] = {"category": options[0] if len(options) == 1 else None, "height_mm": None, "base_z_mm": 0, "footprint_confirmed": False}
        if options == ["exclude"]:
            decisions["layer_mapping"][item["layer"]]["reason"] = "explicit_profile_scope_exclusion"
    defaults = _profile_defaults(profile)
    for key in ("default_height_mm", "opening_height_mm", "floor_datum_m", "layer_mapping"):
        if key in defaults:
            if key == "layer_mapping":
                for layer, config in defaults[key].items():
                    if layer in decisions["layer_mapping"]:
                        decisions["layer_mapping"][layer].update(config)
            else:
                decisions[key] = defaults[key]
    slab_defaults = defaults.get("slabs")
    if isinstance(slab_defaults, dict):
        expected = slab_defaults.get("source_sha256")
        matched = not expected or expected == intake.get("source_sha256")
        # A case profile may propose known interpretations, never approve a new
        # slab scope. Entity-specific exceptions must stay bound to their DWG.
        if slab_defaults.get("region_overrides") and not expected:
            matched = False
        decisions["profile_slab_defaults_status"] = {
            "applied": matched,
            "reason": "source_matched" if matched else "case_source_not_matched",
            "expected_source_sha256": expected,
        }
        if matched:
            decisions["slabs"] = slab_defaults
    from .modeling_standard import seed_standard
    seed_standard(decisions)
    _write(job / "document.json", document)
    _write(job / "rules.json", resolved_rules)
    _write(job / "classification.json", classification)
    _write(job / "layers.json", inventory)
    _write(job / "decisions.json", decisions)
    _write(job / "job.json", {"schema": SCHEMA, "source": str(Path(source).resolve()), "profile": str(profile.resolve()) if profile else None, "profile_sha256": digest_file(profile) if profile else None, "created_utc": _now(), "modeling_standard": decisions['modeling_standard']})
    return review_job(job)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recognition_check_job(job,reference_path,output):
    from .recognition_regression import check_recognition,write_regression
    job=Path(job).resolve();output=Path(output).resolve()
    if output.exists():
        raise WorkflowError("回歸檢查檔已存在；請選擇新的輸出檔案。")
    document=_read(job/"document.json")
    freshness=_source_freshness(document)
    if freshness:
        raise WorkflowError("；".join(freshness))
    report=check_recognition(_read(job/"recognition.json"),_read(Path(reference_path)),document)
    write_regression(report,output)
    return {"status":"pass"if report["all_pass"]else"fail","passed":report["passed"],"total":report["total"],"output":str(output)}



def _draft_payload(document: dict, classification: dict, decisions: dict) -> dict:
    from .modeling import prepare_model
    try:
        from .modeling_standard import active, roof_height_seed, finish_composition
        base = prepare_model(document, classification, roof_height_seed(document, decisions))
        if active(decisions) and decisions.get('slabs', {}).get('enabled'):
            from .slab_workflow import compose_slab_payload
            return finish_composition(compose_slab_payload(base, document, classification, decisions), decisions)
        return base
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as error:
        return {"schema": "dwg-skp-model-v2", "units": "mm", "groups": [], "blockers": [f"建模方案不完整：{error}"], "warnings": []}


def _fixed_stair_source_defaults(document, classification, decisions):
    """Known 2D stair marks remain evidence, not unsupported stair 3D bodies.

    Only unresolved source classifications are filled. An explicit conflicting
    material choice remains visible for review; walls and door thresholds are
    never swept into the stair-mark reference disposition.
    """
    from .modeling_standard import active
    if not active(decisions):
        return
    from .modeling import entity_key, model_entities, _raw_layer
    automatic=classification.get('automatic_plan',{}).get('slab_openings',{})
    ids={identity for row in automatic.get('staircase_openings',[])
         for identity in row.get('stair_mark_source_entity_ids',[])}
    ids.update(identity for row in automatic.get('stair_boundary_review_requests',[])
               for identity in row.get('source_entity_ids',[]))
    for entity in model_entities(document):
        identity=entity_key(entity)
        if identity not in ids:
            continue
        layer=_raw_layer(entity,decisions.get('source_tag_policy','effective'))
        config={**(decisions.get('layer_mapping',{}).get(layer)or{}),
                **(decisions.get('object_overrides',{}).get(identity)or{})}
        if config.get('category') is None:
            decisions.setdefault('object_overrides',{}).setdefault(identity,{}).update(
                category='reference',reason='fixed_standard_source_bound_2d_stair_marks_retained_for_whole_enclosure_opening')


def review_job(job: Path | str, prepared_plan: Path | str | None = None) -> dict:
    job = Path(job).resolve()
    document, classification, decisions = (_read(job / name) for name in ("document.json", "classification.json", "decisions.json"))
    if (job / "approval.json").exists():
        raise WorkflowError("此版本已核定；請使用 revise 建立新工作版本。")
    recognition_files = []
    if classification.get("automatic_plan") is not None:
        from .classifier import attach_automatic_plan
        from .recognition_review import write_recognition_review
        attach_automatic_plan(classification,document,_read(job / "rules.json"),decisions.get("recognition"),slab_decisions=decisions.get("slabs"))
        _write(job / "classification.json", classification)
        recognition_files = write_recognition_review(classification["automatic_plan"], job)
    from .modeling_standard import apply_standard_decisions, enforce_standard_payload, finish_composition, seed_standard
    info = _read(job / 'job.json') if (job / 'job.json').is_file() else {}
    standard_issues = []
    if info.get('modeling_standard') and not isinstance(decisions.get('modeling_standard'), dict):
        seed_standard(decisions)
        standard_issues.append({'code':'fixed_modeling_standard_cannot_be_removed_from_job'})
    _fixed_stair_source_defaults(document,classification,decisions)
    standard_issues.extend(apply_standard_decisions(decisions, classification))
    _write(job / 'decisions.json', decisions)
    if prepared_plan is not None:
        candidate = _read(Path(prepared_plan))
        _write(job / "prepared-plan.json", candidate)
    prepared_file = job / "prepared-plan.json"
    base_file = job / "base-model.plan.json"
    opening_base=job/'opening-base-model.plan.json'
    if opening_base.is_file():
        if prepared_file.is_file() or base_file.is_file():
            raise WorkflowError('選取開口快照不能同時接受另一份宿主 prepared／RC base 方案。')
        from copy import deepcopy
        from .modeling import _json_hash
        payload=deepcopy(verify_opening_base_snapshot(job))
        payload.update(document_digest=_json_hash(document),classification_digest=_json_hash(classification),
                       decisions_digest=_json_hash(decisions))
        parent=Path(_read(job/'opening-base-origin.json')['parent_job'])
        delivery=_read(parent/'delivery_qa.json')if(parent/'delivery_qa.json').is_file()else{}
        if delivery.get('status')=='pass' and delivery.get('output'):
            path=Path(delivery['output'])
            if not path.is_file()or digest_file(path)!=delivery.get('output_sha256'):
                raise WorkflowError('原核定交付 SKP 與保存雜湊不同。')
            payload['native_base']={'path':str(path),'sha256':delivery['output_sha256'],
                                    'payload_sha256':_json_hash(_read(opening_base))}
    elif base_file.is_file():
        from .slab_workflow import compose_slab_payload, verify_base_snapshot
        from .prepared import validate_prepared
        if prepared_file.is_file():
            raise WorkflowError("RC 修訂使用 base-model.plan.json 與 decisions.slabs，不同時接受另一份 prepared-plan。")
        verify_base_snapshot(job)
        base_payload = _read(base_file)
        composed = finish_composition(compose_slab_payload(base_payload,document,classification,decisions), decisions)
        payload = validate_prepared(composed,document,classification,decisions,base_payload=base_payload)
        origin=_read(job / "base-origin.json")
        if origin.get("saved_model"):
            from .modeling import _json_hash
            payload["native_base"]={"path":origin["saved_model"],"sha256":origin["saved_model_sha256"],
                                    "payload_sha256":_json_hash(base_payload)}
    elif prepared_file.is_file():
        from .prepared import validate_prepared
        payload = validate_prepared(_read(prepared_file), document, classification, decisions)
    else:
        payload = _draft_payload(document, classification, decisions)
    if decisions.get("opening_patches") or decisions.get("opening_selection_reviews"):
        from .modeling import apply_selection_opening_patches
        patches=decisions
        if opening_base.is_file():
            existing={row['id']for row in payload.get('selection_opening_audit',[])}
            patches={**decisions,'opening_patches':[row for row in decisions.get('opening_patches',[])if row.get('id')not in existing]}
        payload = apply_selection_opening_patches(payload, patches)
    if opening_base.is_file():
        from .source_accounting import attach_final_source_accounting
        payload['blockers'].extend(attach_final_source_accounting(document,payload,decisions,classification))
    payload = enforce_standard_payload(document, payload, decisions, classification)
    from .fixed_standard_validation import validate_fixed_payload
    payload['blockers'].extend(validate_fixed_payload(payload))
    payload['blockers'].extend(standard_issues)
    _write(job / 'decisions.json', decisions)
    blockers = list(document.get("intake", {}).get("blockers", [])) + _source_freshness(document) + list(payload.get("blockers", []))
    if decisions.get("source_sha256") != document.get("intake", {}).get("source_sha256"):
        blockers.append("決策資料與來源檔雜湊不一致。")
    if not payload.get("groups"):
        blockers.append("沒有可生成的立體構件；不能核定空模型。")
    if document.get("intake", {}).get("units") != "mm":
        blockers.append("抽取資料尚未確認並正規化為 mm。")
    payload["blockers"] = blockers
    _write(job / "model.plan.json", payload)
    hashes = {name: digest_file(job / name) for name in INPUT_FILES}
    if info.get('modeling_standard'):
        hashes['job.json'] = digest_file(job / 'job.json')
    if prepared_file.is_file():
        hashes["prepared-plan.json"] = digest_file(prepared_file)
    if base_file.is_file():
        hashes["base-model.plan.json"] = digest_file(base_file)
        hashes["base-origin.json"] = digest_file(job / "base-origin.json")
    if opening_base.is_file():
        hashes['opening-base-model.plan.json']=digest_file(opening_base)
        hashes['opening-base-origin.json']=digest_file(job/'opening-base-origin.json')
    selection_file = job / "opening-selection.json"
    if selection_file.is_file():
        hashes["opening-selection.json"] = digest_file(selection_file)
    presentation_file = job / "presentation-files.json"
    extra_presentations = []
    if presentation_file.is_file():
        presentation = _read(presentation_file)
        for name in presentation.get("files", []):
            candidate = (job / name).resolve()
            if candidate.parent != job or not candidate.is_file():
                raise WorkflowError("額外審閱檔案必須存在於本次工作資料夾根目錄：" + str(name))
            extra_presentations.append(name)
        hashes["presentation-files.json"] = digest_file(presentation_file)
    intake = document.get("intake", {})
    review = {"schema": SCHEMA, "status": "blocked" if blockers else "ready_for_confirmation", "created_utc": _now(), "input_hashes": hashes, "runtime_sha256": _runtime_digest(), "source_sha256": intake.get("source_sha256"), "blockers": blockers, "warnings": [*intake.get("warnings", []), *payload.get("warnings", [])], "unsupported": intake.get("unsupported", []), "references": payload.get("references", []), "excluded": payload.get("excluded", []), "group_count": len(payload.get("groups", [])), "review_files": ["review.md", "review.svg", "layers.json", "decisions.json", "model.plan.json", *recognition_files]}
    render_review(job, document, decisions, payload, review)
    review["presentation_hashes"] = {name: digest_file(job / name) for name in ("review.md", "review.svg", "layers.json", *extra_presentations, *recognition_files)}
    review["review_sha256"] = digest_data({k: v for k, v in review.items() if k != "created_utc"})
    _write(job / "review.json", review)
    return review


def _short(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render_review(job: Path, document: dict, decisions: dict, payload: dict, review: dict) -> None:
    layers = _read(job / "layers.json")
    rows = ["# DWG → SKP 待核定方案", "", f"狀態：{review['status']}；立體構件：{review['group_count']}；單位：mm。", "", "先確認彩色平面、原圖層用途、建模範圍、輪廓與尺寸。紅色表示尚未指定用途。", "", "![來源與候選輪廓](review.svg)", "", "| 原圖層／有效圖層 | 物件數 | 建議／指定用途 | 高度 mm | 輪廓已確認 |", "|---|---:|---|---:|---|"]
    if decisions.get('modeling_standard'):
        rows[2:2]=['固定白模規範：樓板整片等厚，原外框及孔界不動；牆柱與全高帷幕到當地板底；樓梯整間含平台挖空；防火門／捲簾本體不建，2D洞口及門檻證據保留。', '']
    for layer in layers:
        config = decisions.get("layer_mapping", {}).get(layer["layer"], {})
        escape = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {escape(layer['layer'])} | {layer['count']} | {escape(config.get('category') or '待確認')} | {config.get('height_mm') or decisions.get('default_height_mm') or '待確認'} | {'是' if config.get('footprint_confirmed') else '否／不適用'} |")
    rows += ["", "## 實際候選構件與尺寸", "", "下表是將要建成的有效尺寸，已套用逐物件覆寫。圖層表只列預設值，請以下表核對例外。", "", "| 構件 ID | 分類 | 原圖層／主要 Tag | 基底 Z mm | 高度 mm | 頂部 Z mm |", "|---|---|---|---:|---:|---:|"]
    for group in payload.get("groups", []):
        base, height = group.get("base_z_mm", 0), group.get("height_mm")
        top = base + height if isinstance(base, (int, float)) and isinstance(height, (int, float)) else "待確認"
        clean = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {clean(group.get('id'))} | {clean(group.get('category'))} | {clean(group.get('source_layer'))} / {clean(group.get('tag', group.get('source_layer')))} | {base} | {height} | {top} |")
    rows += ["", "## 尺寸、輪廓與個別決策", "", f"預設高度：{decisions.get('default_height_mm')} mm；開口高度：{decisions.get('opening_height_mm')} mm；地坪標高：{decisions.get('floor_datum_m', '未指定')} m；來源圖層政策：{decisions.get('source_tag_policy', 'effective')}。", ""]
    for layer, config in decisions.get("layer_mapping", {}).items():
        rows.append(f"- 圖層 `{layer}`：{_short(config)}")
    for key, config in decisions.get("object_overrides", {}).items():
        rows.append(f"- 單件覆寫 `{key}`：{_short(config)}")
    if decisions.get("opening_patches"):
        rows += ["", "## AutoCAD 選取開口修訂", "", "下列開口來自使用者在來源 DWG 的明確 Pickfirst 選取；仍須核對宿主、平面範圍、基底及高度。", "", "| 修訂 ID | CAD Handle | 宿主 | 基底 Z mm | 高度 mm | 輪廓依據 |", "|---|---|---|---:|---:|---|"]
        for patch in decisions.get("opening_patches", []):
            clean = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
            rows.append(f"| {clean(patch.get('id'))} | {clean(patch.get('selection_handle'))} | {clean(', '.join(patch.get('host_ids', [])))} | {patch.get('base_z_mm')} | {patch.get('height_mm')} | {clean(patch.get('footprint_basis'))} |")
    if decisions.get("slabs", {}).get("enabled"):
        rows += ["", "## RC 樓板新增範圍", "", "原建築構件保留，以下樓板決策屬本次新增方案；舊版確認不代表已核定新增樓板。", "", "```json", json.dumps(decisions["slabs"], ensure_ascii=False, indent=2), "```", "", "完整板域、分區、留洞與尺寸依據見 model.plan.json 的 slab_audit；無特別條件時缺少板厚採可覆寫的白模預設 200 mm，缺少標高仍需確認。"]
    rows += ["", "## 阻擋項目", ""] + ([f"- {_short(item)}" for item in review["blockers"]] or ["無。等待使用者明確核定本版本。"])
    rows += ["", "## 提醒與未建模內容", ""] + ([f"- {_short(item)}" for item in [*review.get("warnings", []), *review.get("unsupported", []), *payload.get("excluded", [])]] or ["沒有額外警告，參考物件與排除清單如下。"])
    rows += ["", f"保留參考物件：{len(payload.get('references', []))}；排除物件：{len(payload.get('excluded', []))}。完整逐物件清單見 model.plan.json 及 review.json。"]
    reference_counts = Counter((str(item.get("source_layer")), str(item.get("reason"))) for item in payload.get("references", []))
    rows += [f"- 參考圖層 `{layer}`：{count} 件；原因：{reason}。" for (layer, reason), count in sorted(reference_counts.items())]
    rows += ["", "## 核定方式", "", "由使用者在對話中確認方案；Codex 再依原話保存核定紀錄。請勿把產生草稿或舊案例核定當作本次核定。", "", "修改 decisions.json 後重新執行 review。核定後來源、決策、輪廓或程式有變，都需要新版本重新檢查。", ""]
    (job / "review.md").write_text("\n".join(rows), encoding="utf-8")
    draw_svg(job / "review.svg", document, decisions, payload)


def _points(value: Any) -> list[list[float]]:
    output = []
    for point in value or []:
        point = point.get("point", point) if isinstance(point, dict) else point
        if isinstance(point, dict):
            point = [point.get("x"), point.get("y")]
        try:
            x, y = float(point[0]), float(point[1])
            if math.isfinite(x) and math.isfinite(y):
                output.append([x, y])
        except (TypeError, ValueError, IndexError):
            continue
    return output


def draw_svg(path: Path, document: dict, decisions: dict, payload: dict) -> None:
    paths = []
    extents = []
    for entity in _model_entities(document):
        original = str(entity.get("original_layer", entity.get("layer", "0")))
        effective = str(entity.get("effective_layer") or entity.get("layer") or "0")
        layer = effective if original in {"0", "Layer0"} and decisions.get("source_tag_policy", "effective") == "effective" else original
        category = decisions.get("object_overrides", {}).get(str(entity.get("entity_id") or entity.get("id") or entity.get("handle")), {}).get("category", decisions.get("layer_mapping", {}).get(layer, {}).get("category"))
        points = _points(entity.get("curve_points_world"))
        if not points:
            try:
                segments = entity_segments(entity)
                for segment in segments:
                    paths.append(([list(segment.p1), list(segment.p2)], layer, category))
                    extents.extend([segment.p1, segment.p2])
            except (ValueError, TypeError, KeyError):
                pass
        else:
            paths.append((points, layer, category))
            extents.extend(points)
    for group in payload.get("groups", []):
        extents.extend(_points(group.get("outer")))
    if extents:
        x0, y0 = min(p[0] for p in extents), min(p[1] for p in extents)
        x1, y1 = max(p[0] for p in extents), max(p[1] for p in extents)
    else:
        x0, y0, x1, y1 = 0, 0, 100, 100
    scale = min(1120 / max(x1-x0, 1), 690 / max(y1-y0, 1))
    def coordinates(points: list) -> str:
        return " ".join(f"{40+(p[0]-x0)*scale:.3f},{780-(p[1]-y0)*scale:.3f}" for p in points)
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="840" viewBox="0 0 1200 840">', '<rect width="1200" height="840" fill="#f8f7f4"/>', '<text x="40" y="34" font-family="sans-serif" font-size="20" fill="#273342">DWG / SOURCE LAYERS + MODEL FOOTPRINTS</text>', '<text x="40" y="58" font-family="sans-serif" font-size="12" fill="#657080">Plan view · units mm · candidate geometry, requires user confirmation</text>']
    for points, layer, category in paths:
        svg.append(f'<polyline points="{coordinates(points)}" fill="none" stroke="{COLORS.get(category, COLORS[None])}" stroke-width="0.8" opacity="0.7"><title>{html.escape(layer)} / {html.escape(str(category or "UNRESOLVED"))}</title></polyline>')
    for group in payload.get("groups", []):
        rings = [_points(group.get("outer")), *[_points(ring) for ring in group.get("holes", [])]]
        parts = []
        for ring in rings:
            if ring:
                pairs = coordinates(ring).split()
                parts.append("M " + " L ".join(pairs) + " Z")
        category = group.get("category")
        color = COLORS.get(category, "#5b7286")
        label = f"{group.get('name', group.get('id'))} / {group.get('source_layer')} / z={group.get('base_z_mm')} + {group.get('height_mm')} mm"
        svg.append(f'<path d="{" ".join(parts)}" fill="{color}" fill-opacity="0.25" stroke="{color}" stroke-width="1.1" fill-rule="evenodd"><title>{html.escape(label)}</title></path>')
    for opening in payload.get("selection_opening_audit", []):
        footprint = opening.get("clearance_footprint") or {}
        rings = [_points(footprint.get("outer")), *[_points(ring) for ring in footprint.get("holes", [])]]
        parts = []
        for ring in rings:
            if ring:
                pairs = coordinates(ring).split()
                parts.append("M " + " L ".join(pairs) + " Z")
        if parts:
            label = f"CAD selection opening {opening.get('selection_handle')} / z={opening.get('base_z_mm')} + {opening.get('height_mm')} mm"
            svg.append(f'<path d="{" ".join(parts)}" fill="#77b3d7" fill-opacity="0.72" stroke="#1f628c" stroke-width="1.8" fill-rule="evenodd"><title>{html.escape(label)}</title></path>')
    svg.append('<text x="40" y="818" font-family="sans-serif" font-size="12" fill="#657080">Original XY retained. Red: unresolved use. Filled regions: proposed model footprint. Check review.md for scope and heights.</text></svg>')
    path.write_text("\n".join(svg), encoding="utf-8")


def _verify_review(job: Path, review: dict) -> None:
    check = {k: v for k, v in review.items() if k not in {"created_utc", "review_sha256"}}
    if digest_data(check) != review.get("review_sha256"):
        raise WorkflowError("核定預覽紀錄已改變；請重新 review。")
    for name, expected in {**review["input_hashes"], **review.get("presentation_hashes", {})}.items():
        candidate = (job / name).resolve()
        if candidate.parent != job or not candidate.is_file() or digest_file(candidate) != expected:
            raise WorkflowError(f"{name} 與預覽版本不一致；請重新 review 並取得確認。")
    if _runtime_digest() != review.get("runtime_sha256"):
        raise WorkflowError("解析或建模程式已變更；請重新 review 並取得確認。")
    freshness = _source_freshness(_read(job / "document.json"))
    if freshness:
        raise WorkflowError("；".join(freshness))
    if (job / "base-model.plan.json").is_file():
        from .slab_workflow import verify_base_snapshot
        verify_base_snapshot(job)
    if (job / 'opening-base-model.plan.json').is_file():
        verify_opening_base_snapshot(job)


def verified_approved_parent(parent):
    """Use an approved, unchanged current parent, never merely existing files."""
    parent=Path(parent).resolve()
    if not (parent/'review.json').is_file() or not (parent/'approval.json').is_file():
        raise WorkflowError('父工作尚未完成來源綁定的預覽核定。')
    review,approval=_read(parent/'review.json'),_read(parent/'approval.json')
    _verify_review(parent,review)
    if (review.get('status')!='ready_for_confirmation' or review.get('blockers') or
            approval.get('review_sha256')!=review.get('review_sha256') or
            approval.get('input_hashes')!=review.get('input_hashes') or
            approval.get('runtime_sha256')!=review.get('runtime_sha256') or
            not str(approval.get('confirmation')or'').strip()):
        raise WorkflowError('父工作沒有與目前預覽／來源完全相符的核定。')
    payload=_read(parent/'model.plan.json')
    if payload.get('blockers') or not payload.get('groups'):
        raise WorkflowError('父工作沒有可用的完整建模方案。')
    return review,approval,payload


def verify_opening_base_snapshot(job):
    job=Path(job).resolve()
    origin=_read(job/'opening-base-origin.json')
    parent=Path(origin['parent_job']).resolve()
    review,_,base=verified_approved_parent(parent)
    if review['review_sha256']!=origin['parent_review_sha256'] or digest_file(job/'opening-base-model.plan.json')!=origin['base_plan_file_sha256']:
        raise WorkflowError('開口修訂的原核定宿主快照已改變。')
    prior=_read(parent/'decisions.json');current=_read(job/'decisions.json')
    permitted={'opening_patches','opening_selection_reviews'}
    if {k:v for k,v in prior.items()if k not in permitted}!={k:v for k,v in current.items()if k not in permitted}:
        raise WorkflowError('選取開口修訂只增加來源綁定開口；其他尺寸／範圍變更須另建完整修訂方案。')
    for key in permitted:
        old=prior.get(key,[]);new=current.get(key,[])
        if not isinstance(new,list) or new[:len(old)]!=old:
            raise WorkflowError('原核定開口不能在追加修訂中被靜默刪除或改寫。')
    for name in ('document.json','rules.json'):
        if digest_file(job/name)!=digest_file(parent/name):
            raise WorkflowError('選取開口修訂的來源／規則與原核定方案不一致。')
    return base


def approve_job(job: Path | str, *, confirmation: str, expected_review: str, approved_by: str = "user") -> dict:
    job = Path(job).resolve()
    if (job / "approval.json").exists():
        raise WorkflowError("此版本已有核定紀錄，不能覆寫。")
    review = _read(job / "review.json")
    _verify_review(job, review)
    if review.get("status") != "ready_for_confirmation" or review.get("blockers"):
        raise WorkflowError("尚有阻擋項目，不能核定。")
    if expected_review != review["review_sha256"]:
        raise WorkflowError("指定的核定預覽雜湊不一致。")
    if not confirmation.strip() or not approved_by.strip():
        raise WorkflowError("需保存使用者明確確認的原話及核定者。")
    approval = {"schema": "dwg-skp-approval-v2", "review_sha256": review["review_sha256"], "approved_utc": _now(), "approved_by": approved_by, "confirmation": confirmation.strip(), "input_hashes": review["input_hashes"], "runtime_sha256": review["runtime_sha256"]}
    _write(job / "approval.json", approval)
    return approval


def build_job(job: Path | str, output: Path | str | None = None) -> dict:
    from .modeling import write_model
    job = Path(job).resolve()
    if not (job / "approval.json").is_file():
        raise WorkflowError("尚未取得使用者核定，不能生成交付 SKP。")
    review, approval = _read(job / "review.json"), _read(job / "approval.json")
    _verify_review(job, review)
    if approval.get("schema") != "dwg-skp-approval-v2" or approval.get("review_sha256") != review.get("review_sha256") or approval.get("input_hashes") != review.get("input_hashes") or approval.get("runtime_sha256") != review.get("runtime_sha256") or not approval.get("confirmation", "").strip():
        raise WorkflowError("核定紀錄與目前方案不一致。")
    if review.get("status") != "ready_for_confirmation" or review.get("blockers"):
        raise WorkflowError("方案仍有阻擋項目。")
    destination = Path(output).resolve() if output else job / "delivery/model.skp"
    if destination.exists():
        raise WorkflowError("交付檔已存在；請指定新版本路徑。")
    payload = _read(job / "model.plan.json")
    qa = write_model(payload, destination)
    freshness = _source_freshness(_read(job / "document.json"))
    _write(job / "delivery_qa.json", {"schema": "dwg-skp-delivery-v2", "status": "pass" if qa.get("all_pass") and not freshness else "fail", "source_freshness_issues": freshness, "review_sha256": review["review_sha256"], "approval_sha256": digest_file(job / "approval.json"), "output": str(destination), "output_sha256": digest_file(destination) if destination.exists() else None, "qa": qa})
    if not qa.get("all_pass"):
        raise WorkflowError("SKP 重新讀入檢查未通過；請查看 delivery_qa.json。")
    if freshness:
        raise WorkflowError("來源在生成期間改變；本次交付需重新檢查：" + "；".join(freshness))
    return qa


def revise_job(job: Path | str, destination: Path | str) -> dict:
    import shutil
    job, destination = Path(job).resolve(), Path(destination).resolve()
    if destination.exists():
        raise WorkflowError("新版本資料夾已存在。")
    if destination==job or destination in job.parents or job in destination.parents:
        raise WorkflowError('修訂須使用獨立且不互相包含的工作資料夾。')
    destination.mkdir(parents=True)
    for name in ("document.json", "classification.json", "decisions.json", "rules.json", "layers.json", "job.json"):
        shutil.copy2(job / name, destination / name)
    if (job / "prepared-plan.json").is_file():
        shutil.copy2(job / "prepared-plan.json", destination / "prepared-plan.json")
    if (job / "base-model.plan.json").is_file():
        for name in ("base-model.plan.json", "base-origin.json"):
            shutil.copy2(job / name, destination / name)
    if (job/'opening-base-model.plan.json').is_file():
        for name in ('opening-base-model.plan.json','opening-base-origin.json'):
            shutil.copy2(job/name,destination/name)
    else:
        from .modeling_standard import seed_standard
        revised_decisions = _read(destination / 'decisions.json')
        seed_standard(revised_decisions)
        _write(destination / 'decisions.json', revised_decisions)
    info = _read(destination / "job.json")
    info.update(parent_job=str(job), created_utc=_now())
    if not (job/'opening-base-model.plan.json').is_file():
        info['modeling_standard'] = revised_decisions['modeling_standard']
    _write(destination / "job.json", info)
    return review_job(destination)


def attach_selection_openings_job(parent: Path | str, destination: Path | str, selection_path: Path | str, *,
                                  height_mm: float, base_z_mm: float = 0,
                                  opening_type: str = "opening", host_tolerance_mm: float = 1.0) -> dict:
    """Create a new reviewed revision from the active AutoCAD selection snapshot."""
    import shutil
    from .selection_openings import propose_selection_openings

    parent, destination = Path(parent).resolve(), Path(destination).resolve()
    selection_path = Path(selection_path).resolve()
    if destination.exists():
        raise WorkflowError("新版本資料夾已存在。")
    if destination==parent or destination in parent.parents or parent in destination.parents:
        raise WorkflowError('開口修訂須使用獨立且不互相包含的工作資料夾。')
    required = [parent / name for name in ("document.json", "classification.json", "decisions.json",
                                            "rules.json", "layers.json", "job.json", "model.plan.json")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise WorkflowError("父工作缺少可修訂檔案：" + "；".join(missing))
    parent_review,_,approved_plan=verified_approved_parent(parent)
    if not selection_path.is_file():
        raise WorkflowError("找不到 AutoCAD 選取快照：" + str(selection_path))
    document = _read(parent / "document.json")
    decisions = _read(parent / "decisions.json")
    selection = _read(selection_path)
    model_plan = approved_plan
    existing = {str(item.get("id")) for item in decisions.get("opening_patches", []) if item.get("id")}
    try:
        proposal = propose_selection_openings(
            document, model_plan, selection, height_mm=height_mm, base_z_mm=base_z_mm,
            opening_type=opening_type, host_tolerance_mm=host_tolerance_mm,
            existing_patch_ids=existing,
        )
    except ValueError as error:
        raise WorkflowError(str(error)) from error

    destination.mkdir(parents=True)
    for name in ("document.json", "classification.json", "rules.json", "layers.json", "job.json"):
        shutil.copy2(parent / name, destination / name)
    _write(destination/'opening-base-model.plan.json',model_plan)
    _write(destination/'opening-base-origin.json',{'schema':'dwg-approved-opening-base-v1','parent_job':str(parent),
        'parent_review_sha256':parent_review['review_sha256'],'base_plan_file_sha256':digest_file(parent/'model.plan.json')})
    decisions.setdefault("opening_patches", []).extend(proposal["patches"])
    decisions.setdefault("opening_selection_reviews", []).append({
        "proposal_sha256": proposal["proposal_sha256"],
        "selection_snapshot_sha256": proposal["selection_snapshot_sha256"],
        "source_sha256": proposal["source_sha256"],
        "status": proposal["status"],
        "selection_handles": [item.get("handle") for item in proposal["items"]],
        "blockers": proposal["blockers"],
    })
    _write(destination / "decisions.json", decisions)
    _write(destination / "opening-selection.json", proposal)
    info = _read(destination / "job.json")
    info.update(parent_job=str(parent), created_utc=_now(), revision_kind="cad_selection_openings",
                opening_selection_proposal_sha256=proposal["proposal_sha256"])
    _write(destination / "job.json", info)
    return review_job(destination)


def doctor() -> dict:
    import shutil
    from .intake import _find_autocad
    from .skp_native import Native
    core = _find_autocad()
    try:
        import shapely
        geometry_dependency = shapely.__version__
    except ImportError as error:
        geometry_dependency = str(error)
    result = {"python": sys.version.split()[0], "shapely": geometry_dependency, "rules": str(default_rules_path()), "autocad": str(core) if core else None, "dotnet": shutil.which("dotnet"), "sketchup": None}
    try:
        with Native() as native:
            result["sketchup"] = str(native.library_path)
    except (RuntimeError, OSError, FileNotFoundError, AttributeError) as error:
        result["sketchup"] = str(error)
    return result


def slab_review_job(job: Path | str, output: Path | str, *, document_path: Path | None = None,
                    elevation_unit: str | None = None, boundary_layers: tuple[str, ...] = ()) -> dict:
    from .slabs import prepare_slab_review, write_slab_review
    job = Path(job).resolve()
    document = _read(document_path or job / "document.json")
    classification = _read(job / "classification.json")
    issues = _source_freshness(document)
    if issues:
        raise WorkflowError("；".join(issues))
    if document.get("intake", {}).get("blockers") or document.get("intake", {}).get("units") != "mm":
        raise WorkflowError("樓板判讀需要讀取完整且已正規化為 mm 的來源。")
    existing = _read(job / "document.json")
    if existing.get("intake", {}).get("source_sha256") != document.get("intake", {}).get("source_sha256"):
        raise WorkflowError("樓板註記與平面分類必須來自相同版本的來源。")
    slab_decisions = _read(job / "decisions.json").get("slabs", {})
    if slab_decisions.get("source_sha256") not in (None, document.get("intake", {}).get("source_sha256")):
        raise WorkflowError("樓板案例覆寫與目前 DWG 來源版本不同；請重新判讀，勿沿用舊物件 ID。")
    review = prepare_slab_review(document, classification, elevation_unit=elevation_unit,
                                 boundary_layers=boundary_layers, slab_decisions=slab_decisions)
    return write_slab_review(review, output)


# Compatibility imports: historical CLI/API calls use the same sync actions.
from .live_workflow import _live_bridge_source, live_repair_plan_job, live_repair_apply_job


def main(argv: list[str] | None = None) -> int:
    # Windows' legacy code page cannot represent all source layer names.
    # Keep the CLI JSON protocol UTF-8 without changing the machine locale.
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"):
            stream.reconfigure(encoding="utf-8",errors="backslashreplace")
    parser = argparse.ArgumentParser(description="DWG → 原圖層判讀預覽 → 使用者核定 → 原生 SKP")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check local dependencies without editing source files")
    slabs = sub.add_parser("slab-review", help="Review RC slab extent, annotations, openings and level changes without editing a saved model")
    slabs.add_argument("job", type=Path)
    slabs.add_argument("--output", type=Path, required=True)
    slabs.add_argument("--document", type=Path, help="Optional fresh annotation extraction from the same source version")
    slabs.add_argument("--elevation-unit", choices=["mm", "cm", "m"], help="Confirmed annotation unit; separate from CAD XY units")
    slabs.add_argument("--boundary-layer", action="append", default=[], help="Repeat for source layers interpreted as exterior boundary evidence")
    attach = sub.add_parser("slab-attach", help="Prepare a separate RC slab revision from an approved building base; does not approve or write SKP")
    attach.add_argument("parent_job", type=Path)
    attach.add_argument("--to", type=Path, required=True)
    attach.add_argument("--slab-decisions", type=Path, required=True)
    attach.add_argument("--document", type=Path, help="Fresh annotations from exactly the same DWG version")
    intake = sub.add_parser("inspect", help="Read DWG/DXF/normalized JSON and prepare a review")
    intake.add_argument("source", type=Path)
    intake.add_argument("--job", type=Path, required=True)
    intake.add_argument("--rules", type=Path)
    intake.add_argument("--profile", type=Path)
    intake.add_argument("--unit")
    intake.add_argument("--backend", default="auto")
    intake.add_argument("--recognition-decisions", type=Path, help="Previously accepted source-bound semantic system choices and bounded repair settings")
    regression=sub.add_parser("recognition-check",help="Compare automatic recognition with a source-bound reviewed case reference")
    regression.add_argument("job",type=Path)
    regression.add_argument("--reference",type=Path,required=True)
    regression.add_argument("--output",type=Path,required=True)
    review = sub.add_parser("review", help="Rebuild candidate geometry and preview after decision edits")
    review.add_argument("job", type=Path)
    review.add_argument("--prepared-plan", type=Path, help="Source-bound assembled contours from a measured geometry solver")
    approve = sub.add_parser("approve", help="Record the user's actual explicit confirmation of one review")
    approve.add_argument("job", type=Path)
    approve.add_argument("--confirmation", required=True)
    approve.add_argument("--review-sha256", required=True)
    approve.add_argument("--approved-by", default="user")
    build = sub.add_parser("build", help="Write and reopen the exact approved geometry")
    build.add_argument("job", type=Path)
    build.add_argument("--output", type=Path)
    revise = sub.add_parser("revise", help="Copy inputs to a new version without carrying old approval")
    revise.add_argument("job", type=Path)
    revise.add_argument("--to", type=Path, required=True)
    openings = sub.add_parser("opening-selection-attach", help="Create a reviewed opening revision from a read-only AutoCAD Pickfirst snapshot")
    openings.add_argument("parent_job", type=Path)
    openings.add_argument("--selection-json", type=Path, required=True)
    openings.add_argument("--to", type=Path, required=True)
    openings.add_argument("--height-mm", type=float, required=True)
    openings.add_argument("--base-z-mm", type=float, default=0)
    openings.add_argument("--opening-type", choices=["opening", "door", "window"], default="opening")
    openings.add_argument("--host-tolerance-mm", type=float, default=1.0)
    live_install = sub.add_parser("live-install", help="Install the narrow active-model repair bridge; takes effect after SketchUp restarts")
    live_install.add_argument("--sketchup-version", help="Install only one detected version, for example 2021")
    live_status_parser = sub.add_parser("live-status", help="Check the purpose-built active-model repair bridge")
    live_status_parser.add_argument("--port", type=int, default=51236)
    live_plan = sub.add_parser("live-repair-plan", help="Plan a bounded repair against the active SketchUp model without writing it")
    live_plan.add_argument("job", type=Path)
    live_plan.add_argument("--selection-json", type=Path)
    live_plan.add_argument("--action", choices=["cut_opening", "add_missing", "replace_from_cad", "remove_wrong"], required=True)
    live_plan.add_argument("--output", type=Path, required=True)
    live_plan.add_argument("--height-mm", type=float)
    live_plan.add_argument("--base-z-mm", type=float)
    live_plan.add_argument("--category", choices=["architectural_wall", "structural_wall", "column", "curtain", "curtain_accessory"])
    live_plan.add_argument("--tag")
    live_plan.add_argument("--opening-type", choices=["opening", "door", "window"], default="opening")
    live_plan.add_argument("--target-entity-id", action="append", default=[])
    live_plan.add_argument("--port", type=int, default=51236)
    live_plan.add_argument('--live-cad-geometry',action='store_true')
    live_plan.add_argument('--auto-match-position',action='store_true')
    live_plan.add_argument('--wall-system-scope-json',type=Path,help='Explicit per-handle wall/cavity/opening/reference roles and user scope quote')
    live_apply = sub.add_parser("live-repair-apply", help="Apply one hash-confirmed repair to the active model as a single Undo operation")
    live_apply.add_argument("plan", type=Path)
    live_apply.add_argument("--confirm-sha256", required=True)
    live_apply.add_argument("--receipt", type=Path)
    live_apply.add_argument("--port", type=int, default=51236)
    live_apply.add_argument('--current-selection-json',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "slab-review":
            result = slab_review_job(args.job, args.output, document_path=args.document, elevation_unit=args.elevation_unit, boundary_layers=tuple(args.boundary_layer))
        elif args.command == "slab-attach":
            from .slab_workflow import attach_slab_job
            result = attach_slab_job(args.parent_job,args.to,args.slab_decisions,document_path=args.document)
        elif args.command == "inspect":
            result = inspect_source(args.source, args.job, rules=args.rules, profile=args.profile, unit=args.unit, backend=args.backend, recognition_decisions=args.recognition_decisions)
        elif args.command == "recognition-check":
            result=recognition_check_job(args.job,args.reference,args.output)
        elif args.command == "review":
            result = review_job(args.job, prepared_plan=args.prepared_plan)
        elif args.command == "approve":
            result = approve_job(args.job, confirmation=args.confirmation, expected_review=args.review_sha256, approved_by=args.approved_by)
        elif args.command == "build":
            result = build_job(args.job, args.output)
        elif args.command == "revise":
            result = revise_job(args.job, args.to)
        elif args.command == "opening-selection-attach":
            result = attach_selection_openings_job(
                args.parent_job, args.to, args.selection_json,
                height_mm=args.height_mm, base_z_mm=args.base_z_mm,
                opening_type=args.opening_type, host_tolerance_mm=args.host_tolerance_mm,
            )
        elif args.command == "live-install":
            from .sketchup_live import install_live_bridge
            result = install_live_bridge(_live_bridge_source(), sketchup_version=args.sketchup_version)
        elif args.command == "live-status":
            from .sketchup_live import live_status
            result = live_status(port=args.port)
        elif args.command == "live-repair-plan":
            result = live_repair_plan_job(
                args.job, args.output, action=args.action,
                selection_path=args.selection_json,
                height_mm=args.height_mm, base_z_mm=args.base_z_mm,
                category=args.category, tag=args.tag, opening_type=args.opening_type,
                target_entity_ids=args.target_entity_id, port=args.port,
                live_cad_geometry=args.live_cad_geometry,auto_match_position=args.auto_match_position,
                wall_system_scope_path=args.wall_system_scope_json,
            )
        else:
            result = live_repair_apply_job(
                args.plan, confirmation_sha256=args.confirm_sha256,
                receipt_path=args.receipt, port=args.port,current_selection_path=args.current_selection_json,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") in {"blocked","fail","error"} else 0
    except (WorkflowError, ValueError, RuntimeError, FileNotFoundError, ImportError, OSError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
