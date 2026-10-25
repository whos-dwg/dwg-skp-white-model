"""Serialization joins must reuse source endpoints, not reproject raw ones."""
from __future__ import annotations

import copy
import math

import pytest
from shapely.geometry import Polygon

from dwg_semantic.mesh_union import boundaries, quantized, shape, topology


def oblique_cell(offset=(193827.0000158, -171234.78469256)):
    x, y = offset
    # Long diagonal segments amplify mixing a raw origin with a lattice direction.
    outer = [[x, y + 6582.01539012], [x + 3398.15529105, y],
             [x + 40751.84472301, y], [x + 44150.00001299, y + 6582.01537792],
             [x + 44150.00001822, y + 29017.98532660],
             [x + 40751.84490933, y + 35600.00035861],
             [x + 3398.15510242, y + 35600.00035861], [x, y + 29017.98532552]]
    holes = [[[x + 8000.12345678, y + 9000.98765432],
              [x + 9500.23456789, y + 9200.87654321],
              [x + 9300.34567891, y + 11000.76543219],
              [x + 8100.45678912, y + 10900.65432198]]]
    return {"id": "generic-oblique", "footprint": {"outer": outer, "holes": holes},
            "base_z_mm": -200., "height_mm": 200.}


@pytest.mark.parametrize("offset", [(193827.0000158, -171234.78469256),
                                     (-284193.1958037, 163847.3102468),
                                     (0.0000158, -0.78469256)])
def test_oblique_source_endpoints_close_on_the_existing_serialization_lattice(offset):
    cell = oblique_cell(offset)
    original = copy.deepcopy(cell)
    _, faces = boundaries([cell])
    audit = topology(faces)
    assert audit["all_closed"], audit
    assert audit["shell_count"] == 1
    assert cell == original  # Neither CAD footprints nor hole boundaries are rewritten.
    source_vertices = {quantized((x, y, z)) for ring in
                       [cell["footprint"]["outer"], *cell["footprint"]["holes"]]
                       for x, y in ring for z in (-200., 0.)}
    emitted = {tuple(p) for face in faces for ring in [face["outer"], *face["holes"]] for p in ring}
    assert emitted == source_vertices
    assert math.isclose(shape(cell).area, Polygon(original["footprint"]["outer"],
                                                  original["footprint"]["holes"]).area)


def test_separate_source_material_is_not_closed_by_a_larger_weld():
    cell = oblique_cell()
    other = copy.deepcopy(cell)
    other["id"] = "separate"
    for ring in [other["footprint"]["outer"], *other["footprint"]["holes"]]:
        for p in ring:
            p[0] += 50000.
    _, faces = boundaries([cell, other])
    assert topology(faces)["all_closed"]
    assert topology(faces)["shell_count"] == 2
