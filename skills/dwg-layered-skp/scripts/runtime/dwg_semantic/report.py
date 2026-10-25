from __future__ import annotations

from typing import Any


def render_markdown(result: dict[str, Any]) -> str:
    counts = result["counts"]
    validation = result["validation"]
    guards = result["generalization_guards"]
    continuity = result["audits"]["wall_continuity"]
    lines = [
        "# DWG 通用語意規則回歸報告",
        "",
        f"來源：`{result['source_name']}`  ",
        f"規則：`{result['rules_schema']}`  ",
        f"驗證：**{validation['status'].upper()}**",
        "",
        "## 結果摘要",
        "",
        f"- 一般牆乾淨線段：{counts['architectural_wall_segments']}",
        f"- 結構牆構件：{counts['structural_wall_components']}",
        f"- 獨立柱：{counts['columns']}",
        f"- 帷幕／大型外圍窗系統：{counts['curtain_envelopes']}",
        f"  - 由大型 WIN 平行外圈自動辨識的來源物件：{counts.get('oversized_opening_objects_auto_classified_as_curtain', 0)}",
        f"  - 貼附帷幕的斷面／看面／mullion 附屬物件：{counts.get('curtain_accessory_objects', 0)}",
        f"  - 附屬圖層數：{counts.get('curtain_accessory_layers', 0)}",
        f"- 依明確設定排除的防火捲簾／防火門物件：{counts.get('ignored_fire_protection_objects', 0)}",
        f"- 特殊尺寸待使用者確認：{counts.get('special_dimension_review_requests', 0)}",
        f"- WIN／窗／門開口來源物件：{counts['opening_source_objects']}",
        f"  - 圖塊實例：{counts['opening_block_instances']}",
        f"  - 線／多段線等幾何物件：{counts['opening_geometry_objects']}",
        f"  - 鄰接牆網、可作開口證據：{counts['opening_wall_adjacent_objects']}",
        f"  - 尚未鄰接牆網、僅保留來源證據：{counts['opening_unmatched_source_objects']}",
        f"- 衛浴／廁所群組：{counts['sanitary_zones']}",
        f"- 因衛浴封閉需求補回的牆線：{counts['sanitary_wall_segments_promoted']}",
        f"- 驗證失敗：{len(validation['failures'])}",
        f"- 待人工確認：{len(validation['warnings'])}",
        "",
        "## 可再現規則",
        "",
        "1. 廁所牆不是用座標補畫；先找衛浴設備種子，再在其周邊以牆面完成層、平行雙線與既有牆網連通性恢復隔間，最後做四側封閉覆蓋檢查。",
        "2. 帷幕可由大型窗／帷幕圖塊及其 mullion/CW 證據辨識；大型 WIN 圖層多段線若形成三條以上同層、近乎等距且範圍一致的外圈，也會保留原始線形並歸為帷幕系統。",
        "   斷面、看面、mullion／豎框／橫框等圖層只有在幾何上貼附已確認的帷幕路徑時，才會繼承為帷幕附屬構件；不會僅憑圖層名稱全域誤收。",
        "   若特殊尺寸物件的證據不足，系統不會靜默排除，而會建立需使用者確認的語意決策項目。",
        "   通用規則保留防火捲簾／防火門；只有選用明確的專案範圍設定時才排除，依據保存在排除稽核。",
        "3. 每一層巢狀圖塊都檢查物件本身的圖層；內層物件若明確屬於 WALL／牆／COL／COLUMN／柱圖層，會以展開後的模型空間座標直接分類。",
        "   只有『由外層圖塊容器推定語意』仍限制在直接子物件，避免把內層軸網、標註與符號誤傳成結構。",
        "4. WIN／WINDOW／窗／門圖層中的圖塊與線幾何會列為開口；牆端點接到這些物件時，標記為合法開口終止，不會硬補成牆。",
        "5. 結構牆先依結構語意與幾何獨立分類，再從一般牆網排除；兩者來源 handle 必須互斥。",
        "6. 柱子跨圖層搜尋封閉、近方形、正交的緊湊輪廓，並要求結構網或主平面外緣的空間證據；通過後成為獨立構件並從牆類排除。",
        "7. 規則禁止 handle 與座標特例；每筆輸出必須附證據鏈。",
        "",
        "## 圖塊內語意與開口稽核",
        "",
        f"- 依內層物件自身圖層展開的語意幾何：{guards['explicit_nested_layer_entities_materialized']}",
        f"  - 一般牆：{guards['explicit_nested_architectural_wall_entities']}",
        f"  - 結構牆／柱候選：{guards['explicit_nested_structural_entities']}",
        f"  - 牆面完成層：{guards['explicit_nested_wall_finish_entities']}",
        f"- 圖塊內開口物件：{guards['nested_opening_objects_classified']}",
        f"- 牆端點紀錄：{continuity['endpoint_records_total']}",
        f"  - 接到其他牆線：{continuity['connected']}",
        f"  - 接到 WIN／窗／門開口：{continuity['opening_terminated']}",
        f"  - 接到獨立柱：{continuity['column_terminated']}",
        f"  - 接到主平面外緣：{continuity['plan_perimeter_terminated']}",
        f"  - 接到相鄰牆／結構面：{continuity['wall_interface_terminated']}",
        f"  - 尚無法解釋：{continuity['unresolved']}",
        "",
        "## 衛浴封閉回歸",
        "",
        "| 群組 | 設備數 | 補回牆線 | 覆蓋側數 | 狀態 |",
        "|---|---:|---:|---:|---|",
    ]
    for audit in result["audits"]["sanitary_enclosures"]:
        lines.append(
            f"| {audit['id']} | {audit['fixture_count']} | {audit.get('promoted_segment_count', 0)} | "
            f"{audit['covered_sides']}/4（門檻 {audit['required_sides']}） | {audit['status']} |"
        )
    lines += [
        "",
        "## 特殊尺寸語意決策",
        "",
    ]
    decision_requests = result.get("audits", {}).get("semantic_decision_requests", [])
    if not decision_requests:
        lines.append("- 無；所有大型 WIN／窗語意物件皆已有足夠證據或既有帷幕容器可解釋。")
    else:
        for request in decision_requests:
            lines.append(
                f"- `{request['id']}`：handle `{request['source_handle']}`，圖層 `{request['source_layer']}`，"
                f"尺寸 {request['width_mm']:.0f} × {request['height_mm']:.0f} mm。{request['question']}"
            )
    accessory_layers = sorted({
        str(item.get("source_layer") or "")
        for item in result.get("components", {}).get("curtain_accessories", [])
        if item.get("source_layer")
    })
    lines += [
        "",
        "## 帷幕附屬構件",
        "",
        f"- 貼附物件數：{counts.get('curtain_accessory_objects', 0)}",
        f"- 來源圖層：{', '.join(f'`{layer}`' for layer in accessory_layers) if accessory_layers else '無'}",
        f"- 防火捲簾／防火門排除物件：{counts.get('ignored_fire_protection_objects', 0)}",
    ]
    lines += [
        "",
        "## 獨立柱構件",
        "",
        "| 構件 | 尺寸（mm） | 來源層 | 判斷分數 |",
        "|---|---:|---|---:|",
    ]
    for column in result["components"]["columns"]:
        lines.append(
            f"| {column['id']} | {column['width_mm']:.0f} × {column['depth_mm']:.0f} | "
            f"{column.get('source_layer') or ''} | {column['score']:.2f} |"
        )
    lines += [
        "",
        "## 類別互斥檢查",
        "",
        "- 柱來源不再同時落入一般牆或結構牆。",
        "- 結構牆與一般牆來源集合互斥。",
        "- WIN／窗／門開口與牆、柱來源集合互斥。",
        "- 帷幕主輪廓與附屬斷面／看面／mullion 保持同一系統關聯，附屬物件必須有空間貼附證據。",
        "- 防火捲簾／防火門的排除僅依明確設定，排除來源不得混入門窗開口或帷幕。",
        "- 圖外衛浴圖例不進入平面群組；僅分析主導平面範圍內的設備。",
        "",
        "## 驗證告警",
        "",
    ]
    coverage = result.get("audits", {}).get("source_coverage", {})
    lines += [
        f"- 來源實例：{coverage.get('input_entities', 0)}；已分類：{coverage.get('classified_entities', 0)}；未分類：{coverage.get('unclassified_entities', 0)}。",
        f"- 未支援幾何物件：{coverage.get('unsupported_geometry_entities', 0)}；類型：{', '.join(coverage.get('unsupported_entity_types', [])) or '無'}。",
        "- 判斷分數是規則證據累加值，不是分類正確率或機率。",
        "- 完整未分類來源與未支援物件清單見 JSON 的 audits.source_coverage。",
    ]
    if not validation["warnings"]:
        lines.append("- 無。")
    else:
        for warning in validation["warnings"]:
            lines.append(f"- `{warning['rule']}`：{warning}")
    if validation["failures"]:
        lines += ["", "## 驗證失敗", ""]
        for failure in validation["failures"]:
            lines.append(f"- `{failure['rule']}`：{failure}")
    return "\n".join(lines) + "\n"
