from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from .geometry import entity_segments


COLORS = {
    "background": "#CBD5E1",
    "architectural": "#E83E8C",
    "structural": "#F59E0B",
    "column": "#00A878",
    "curtain": "#146CFF",
    "curtain_accessory": "#22C55E",
    "opening": "#0EA5E9",
    "sanitary": "#7C3AED",
    "fixture": "#0891B2",
    "review": "#DC2626",
}


def _pairs(items: Iterable[dict[str, Any]]) -> list[list[tuple[float, float]]]:
    output = []
    for item in items:
        start, end = item.get("start"), item.get("end")
        if start and end:
            output.append([(float(start[0]), float(start[1])), (float(end[0]), float(end[1]))])
    return output


def _source_pairs(data: dict[str, Any], extent: dict[str, Any]) -> list[list[tuple[float, float]]]:
    output = []
    x0, y0 = extent["min"]["x"], extent["min"]["y"]
    x1, y1 = extent["max"]["x"], extent["max"]["y"]
    for entity in data.get("entities", []):
        if entity.get("owner_block") != "*Model_Space" or entity.get("type") not in {"LINE", "LWPOLYLINE"}:
            continue
        for segment in entity_segments(entity, world=False):
            mx, my = segment.midpoint
            if x0 <= mx <= x1 and y0 <= my <= y1:
                output.append([segment.p1, segment.p2])
    return output


def _add_lines(ax: Any, pairs: list, color: str, width: float, alpha: float = 1.0, zorder: int = 1) -> None:
    if pairs:
        ax.add_collection(LineCollection(pairs, colors=color, linewidths=width, alpha=alpha, zorder=zorder))


def _extent(result: dict[str, Any]) -> dict[str, Any]:
    envelopes = result["components"]["curtain_envelopes"]
    if envelopes:
        return envelopes[0]["bbox"]
    structural = [segment for component in result["components"]["structural_walls"] for segment in component["segments"]]
    pairs = _pairs(result["components"]["architectural_walls"] + structural)
    points = [point for pair in pairs for point in pair]
    for opening in result["components"].get("openings", []):
        box = opening["bbox"]
        points.extend([
            (float(box["min"]["x"]), float(box["min"]["y"])),
            (float(box["max"]["x"]), float(box["max"]["y"])),
        ])
    for column in result["components"]["columns"]:
        box = column["bbox"]
        points.extend([
            (float(box["min"]["x"]), float(box["min"]["y"])),
            (float(box["max"]["x"]), float(box["max"]["y"])),
        ])
    return {
        "min": {"x": min(point[0] for point in points), "y": min(point[1] for point in points)},
        "max": {"x": max(point[0] for point in points), "y": max(point[1] for point in points)},
    }


def _style_axes(ax: Any, title: str, title_size: float = 13) -> None:
    ax.set_aspect("equal", adjustable="box")
    ax.set_facecolor("#FFFFFF")
    ax.grid(True, color="#E2E8F0", linewidth=0.4, alpha=0.45)
    ax.set_title(title, loc="left", fontsize=title_size, fontweight="bold", color="#0F172A")
    ax.tick_params(labelsize=7, colors="#64748B")


def render_overview(data: dict[str, Any], result: dict[str, Any], output: Path) -> None:
    extent = _extent(result)
    width = extent["max"]["x"] - extent["min"]["x"]
    height = extent["max"]["y"] - extent["min"]["y"]
    margin = max(width, height) * 0.035
    fig, ax = plt.subplots(figsize=(14, 13), dpi=180)
    _add_lines(ax, _source_pairs(data, extent), COLORS["background"], 0.25, 0.18, 0)
    _add_lines(ax, _pairs(result["components"]["architectural_walls"]), COLORS["architectural"], 0.9, 0.9, 2)
    structural = [segment for component in result["components"]["structural_walls"] for segment in component["segments"]]
    _add_lines(ax, _pairs(structural), COLORS["structural"], 1.45, 0.95, 3)
    opening_components = result["components"].get("openings", [])
    adjacent_openings = [
        segment
        for component in opening_components
        if component.get("wall_adjacency_status") == "adjacent"
        for segment in component["segments"]
    ]
    unmatched_openings = [
        segment
        for component in opening_components
        if component.get("wall_adjacency_status") != "adjacent"
        for segment in component["segments"]
    ]
    _add_lines(ax, _pairs(unmatched_openings), COLORS["opening"], 0.55, 0.28, 4)
    _add_lines(ax, _pairs(adjacent_openings), COLORS["opening"], 1.3, 0.95, 5)
    promoted = result["inferences"]["sanitary_wall_segments_promoted"]
    _add_lines(ax, _pairs(promoted), COLORS["sanitary"], 2.25, 0.95, 5)
    for column in result["components"]["columns"]:
        box = column["bbox"]
        ax.add_patch(Rectangle(
            (box["min"]["x"], box["min"]["y"]),
            box["max"]["x"] - box["min"]["x"],
            box["max"]["y"] - box["min"]["y"],
            facecolor=COLORS["column"], edgecolor="#047857", linewidth=1.0, alpha=0.55, zorder=6,
        ))
    curtain_accessories = [
        segment
        for component in result["components"].get("curtain_accessories", [])
        for segment in component.get("segments", [])
    ]
    _add_lines(ax, _pairs(curtain_accessories), COLORS["curtain_accessory"], 1.25, 0.95, 6)
    curtain = [segment for envelope in result["components"]["curtain_envelopes"] for segment in envelope["segments"]]
    _add_lines(ax, _pairs(curtain), COLORS["curtain"], 3.2, 1.0, 7)
    for request in result.get("audits", {}).get("semantic_decision_requests", []):
        box = request["bbox"]
        ax.add_patch(Rectangle(
            (box["min"]["x"], box["min"]["y"]),
            box["max"]["x"] - box["min"]["x"],
            box["max"]["y"] - box["min"]["y"],
            fill=False,
            edgecolor=COLORS["review"],
            linewidth=1.8,
            linestyle="--",
            zorder=9,
        ))
        ax.text(
            box["min"]["x"],
            box["max"]["y"],
            request["id"],
            color=COLORS["review"],
            fontsize=7,
            fontweight="bold",
            ha="left",
            va="bottom",
            zorder=10,
        )
    for zone in result["audits"]["sanitary_enclosures"]:
        points = zone["fixture_points"]
        ax.scatter([point[0] for point in points], [point[1] for point in points], s=10, color=COLORS["fixture"], zorder=8)
    ax.set_xlim(extent["min"]["x"] - margin, extent["max"]["x"] + margin)
    ax.set_ylim(extent["min"]["y"] - margin, extent["max"]["y"] + margin)
    schema = str(result.get("schema") or "semantic classification").replace("dwg-semantic-classification-", "").upper()
    _style_axes(ax, f"DWG · Semantic component review {schema}")
    handles = [
        Line2D([0], [0], color=COLORS["curtain"], lw=3, label="Curtain / large-scale WIN perimeter system"),
        Line2D([0], [0], color=COLORS["curtain_accessory"], lw=2, label="Curtain accessory · section / elevation / mullion"),
        Line2D([0], [0], color=COLORS["architectural"], lw=2, label="General architectural wall"),
        Line2D([0], [0], color=COLORS["structural"], lw=2, label="Structural wall"),
        Line2D([0], [0], color=COLORS["opening"], lw=2, label="WIN/window/door source · wall-adjacent strong"),
        Line2D([0], [0], color=COLORS["sanitary"], lw=3, label="Recovered sanitary enclosure wall"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor=COLORS["column"], markersize=9, label="Independent column"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=COLORS["fixture"], markersize=6, label="Sanitary fixture seed"),
        Line2D([0], [0], color=COLORS["review"], lw=2, linestyle="--", label="Special-size object · user confirmation required"),
    ]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.035), ncol=4, frameon=False, fontsize=8)
    fig.text(
        0.01, 0.012,
        f"Validation: {result['validation']['status'].upper()} · "
        f"{result['counts']['columns']} columns · {result['counts']['opening_wall_adjacent_objects']} wall-adjacent opening objects · "
        f"{result['generalization_guards']['explicit_nested_layer_entities_materialized']} nested semantic objects · "
        f"{result['counts'].get('curtain_accessory_objects', 0)} curtain accessories · "
        f"{result['counts'].get('special_dimension_review_requests', 0)} decision request(s)",
        fontsize=8, color="#475569",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_curtain_audit(data: dict[str, Any], result: dict[str, Any], output: Path) -> None:
    curtains = result["components"].get("curtain_envelopes", [])
    if not curtains:
        return
    extent = curtains[0]["bbox"]
    width = extent["max"]["x"] - extent["min"]["x"]
    height = extent["max"]["y"] - extent["min"]["y"]
    margin = max(width, height) * 0.025
    fig, ax = plt.subplots(figsize=(14, 13), dpi=180)
    _add_lines(ax, _source_pairs(data, extent), COLORS["background"], 0.25, 0.14, 0)
    accessories = [
        segment
        for component in result["components"].get("curtain_accessories", [])
        for segment in component.get("segments", [])
    ]
    curtain_segments = [segment for curtain in curtains for segment in curtain.get("segments", [])]
    _add_lines(ax, _pairs(accessories), COLORS["curtain_accessory"], 1.4, 0.95, 4)
    _add_lines(ax, _pairs(curtain_segments), COLORS["curtain"], 3.0, 1.0, 5)
    ax.set_xlim(extent["min"]["x"] - margin, extent["max"]["x"] + margin)
    ax.set_ylim(extent["min"]["y"] - margin, extent["max"]["y"] + margin)
    schema = str(result.get("schema") or "semantic classification").replace("dwg-semantic-classification-", "").upper()
    _style_axes(ax, f"DWG · Curtain system and attached component audit · {schema}")
    ax.legend(
        handles=[
            Line2D([0], [0], color=COLORS["curtain"], lw=3, label="Primary 4win perimeter system"),
            Line2D([0], [0], color=COLORS["curtain_accessory"], lw=2, label="Attached section / elevation / mullion geometry"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.035),
        ncol=2,
        frameon=False,
        fontsize=8,
    )
    layers = sorted({
        str(item.get("source_layer") or "")
        for item in result["components"].get("curtain_accessories", [])
        if item.get("source_layer")
    })
    fig.text(
        0.01,
        0.012,
        f"{len(curtains)} curtain system · {result['counts'].get('oversized_opening_objects_auto_classified_as_curtain', 0)} primary WIN objects · "
        f"{result['counts'].get('curtain_accessory_objects', 0)} attached accessories · {len(layers)} accessory layers",
        fontsize=8,
        color="#475569",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_sanitary(data: dict[str, Any], result: dict[str, Any], output: Path) -> None:
    zones = result["audits"]["sanitary_enclosures"]
    columns = 3
    rows = math.ceil(len(zones) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(15, 4.8 * rows), dpi=180)
    axes = list(axes.flat) if hasattr(axes, "flat") else [axes]
    promoted = result["inferences"]["sanitary_wall_segments_promoted"]
    architectural = result["components"]["architectural_walls"]
    adjacent_openings = [
        segment
        for component in result["components"].get("openings", [])
        if component.get("wall_adjacency_status") == "adjacent"
        for segment in component["segments"]
    ]
    for ax, zone in zip(axes, zones):
        search = zone["search_bbox"]
        _add_lines(ax, _source_pairs(data, search), COLORS["background"], 0.35, 0.24, 0)
        _add_lines(ax, _pairs(architectural), COLORS["architectural"], 0.9, 0.45, 1)
        _add_lines(ax, _pairs(adjacent_openings), COLORS["opening"], 1.0, 0.8, 2)
        zone_promoted = [
            segment for segment in promoted
            if any(zone["id"] in evidence for evidence in segment.get("evidence", []))
        ]
        _add_lines(ax, _pairs(zone_promoted), COLORS["sanitary"], 2.5, 1.0, 4)
        points = zone["fixture_points"]
        ax.scatter([point[0] for point in points], [point[1] for point in points], s=22, color=COLORS["fixture"], zorder=5)
        margin = 200.0
        ax.set_xlim(search["min"]["x"] - margin, search["max"]["x"] + margin)
        ax.set_ylim(search["min"]["y"] - margin, search["max"]["y"] + margin)
        _style_axes(
            ax,
            f"{zone['id']}\nfixtures {zone['fixture_count']} · recovered {zone['promoted_segment_count']} · "
            f"coverage {zone['covered_sides']}/4 ({zone['status']})",
            title_size=9,
        )
    for ax in axes[len(zones):]:
        ax.axis("off")
    schema = str(result.get("schema") or "semantic classification").replace("dwg-semantic-classification-", "").upper()
    fig.suptitle(f"Sanitary enclosure review · {schema} evidence", x=0.02, ha="left", fontsize=18, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def render_block_layer_audit(result: dict[str, Any], output: Path) -> None:
    """Show only geometry recovered from explicit layers inside block instances."""
    extent = _extent(result)
    width = extent["max"]["x"] - extent["min"]["x"]
    height = extent["max"]["y"] - extent["min"]["y"]
    margin = max(width, height) * 0.035
    fig, ax = plt.subplots(figsize=(14, 13), dpi=180)

    all_structural = [
        segment for component in result["components"]["structural_walls"] for segment in component["segments"]
    ]
    _add_lines(ax, _pairs(result["components"]["architectural_walls"]), COLORS["background"], 0.45, 0.32, 0)
    _add_lines(ax, _pairs(all_structural), COLORS["background"], 0.45, 0.32, 0)

    nested_architectural = [
        segment
        for segment in result["components"]["architectural_walls"]
        if "/" in str(segment.get("source_handle") or "")
    ]
    nested_structural = [
        segment
        for component in result["components"]["structural_walls"]
        if "/" in str(component.get("source_handle") or "")
        for segment in component["segments"]
    ]
    nested_openings = [
        segment
        for component in result["components"].get("openings", [])
        if component.get("instance_path") and component.get("wall_adjacency_status") == "adjacent"
        for segment in component["segments"]
    ]
    _add_lines(ax, _pairs(nested_architectural), COLORS["architectural"], 2.1, 1.0, 3)
    _add_lines(ax, _pairs(nested_structural), COLORS["structural"], 2.1, 1.0, 4)
    _add_lines(ax, _pairs(nested_openings), COLORS["opening"], 2.0, 1.0, 5)
    nested_column_count = 0
    for column in result["components"]["columns"]:
        if not any("/" in str(handle or "") for handle in column.get("source_handles", [])):
            continue
        nested_column_count += 1
        box = column["bbox"]
        ax.add_patch(Rectangle(
            (box["min"]["x"], box["min"]["y"]),
            box["max"]["x"] - box["min"]["x"],
            box["max"]["y"] - box["min"]["y"],
            facecolor=COLORS["column"], edgecolor="#047857", linewidth=1.5, alpha=0.75, zorder=6,
        ))

    ax.set_xlim(extent["min"]["x"] - margin, extent["max"]["x"] + margin)
    ax.set_ylim(extent["min"]["y"] - margin, extent["max"]["y"] + margin)
    schema = str(result.get("schema") or "semantic classification").replace("dwg-semantic-classification-", "").upper()
    _style_axes(ax, f"DWG · Explicit semantic layers recovered inside blocks · {schema}")
    handles = [
        Line2D([0], [0], color=COLORS["architectural"], lw=3, label="Nested WALL-layer geometry"),
        Line2D([0], [0], color=COLORS["structural"], lw=3, label="Nested COL/structural geometry"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor=COLORS["column"], markersize=9, label="Nested column"),
        Line2D([0], [0], color=COLORS["opening"], lw=3, label="Nested WIN/window/door opening"),
    ]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.035), ncol=4, frameon=False, fontsize=8)
    guards = result["generalization_guards"]
    fig.text(
        0.01,
        0.012,
        f"Recovered from nested object layers: {guards['explicit_nested_architectural_wall_entities']} wall · "
        f"{guards['explicit_nested_structural_entities']} structural candidates · "
        f"{nested_column_count} consolidated columns · {guards['nested_opening_objects_classified']} opening sources",
        fontsize=8,
        color="#475569",
    )
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render semantic review images.")
    parser.add_argument("source", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--overview", type=Path, required=True)
    parser.add_argument("--sanitary", type=Path, required=True)
    parser.add_argument("--block-audit", type=Path)
    parser.add_argument("--curtain-audit", type=Path)
    args = parser.parse_args()
    data = json.loads(args.source.read_text(encoding="utf-8"))
    result = json.loads(args.result.read_text(encoding="utf-8"))
    render_overview(data, result, args.overview)
    render_sanitary(data, result, args.sanitary)
    if args.block_audit:
        render_block_layer_audit(result, args.block_audit)
    if args.curtain_audit:
        render_curtain_audit(data, result, args.curtain_audit)


if __name__ == "__main__":
    main()
