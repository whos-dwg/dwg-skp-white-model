"""Read-only DWG/DXF ingestion and validated millimetre normalization.

AutoCAD Core Console loads a packaged .NET command which opens a *side database*
for reading. It never opens the input in the editor and contains no save command.
Failures remain in ``intake.blockers``; an empty or partial extraction is never a
successful handoff. Existing extraction JSON is accepted without AutoCAD.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from xml.sax.saxutils import escape


SCHEMA = "dwg-layered-intake-v1"
INSUNITS = {0: None, 1: "in", 2: "ft", 3: "mi", 4: "mm", 5: "cm", 6: "m", 7: "km",
            8: "microin", 9: "mil", 10: "yd", 11: "angstrom", 12: "nm", 13: "um",
            14: "dm", 15: "dam", 16: "hm", 17: "gm", 18: "au", 19: "ly", 20: "pc",
            21: "us-ft", 22: "us-in", 23: "us-yd", 24: "us-mi"}
UNIT_FACTORS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "km": 1e6, "in": 25.4,
                "ft": 304.8, "yd": 914.4, "mi": 1609344.0, "microin": .0000254,
                "mil": .0254, "angstrom": 1e-7, "nm": 1e-6, "um": .001, "dm": 100.,
                "dam": 10000., "hm": 100000., "gm": 1e12, "au": 149597870700000.,
                "ly": 9.4607304725808e18, "pc": 3.085677581491367e19,
                "us-ft": 1200000/3937, "us-in": 100000/3937,
                "us-yd": 3600000/3937, "us-mi": 6336000000/3937}
ALIASES = {"millimeters": "mm", "millimetres": "mm", "millimeter": "mm", "millimetre": "mm",
           "centimeters": "cm", "centimetres": "cm", "meters": "m", "metres": "m",
           "kilometers": "km", "inches": "in", "inch": "in", "feet": "ft", "foot": "ft",
           "yards": "yd", "miles": "mi", "decimeters": "dm", "nanometers": "nm",
           "microns": "um", "microinches": "microin", "mils": "mil", "angstroms": "angstrom",
           "dekameters": "dam", "hectometers": "hm", "gigameters": "gm",
           "astronomical": "au", "lightyears": "ly", "parsecs": "pc",
           "usfeet": "us-ft", "usinches": "us-in", "usyards": "us-yd", "usmiles": "us-mi",
           "ussurveyfeet": "us-ft", "ussurveyinch": "us-in", "ussurveyinches": "us-in",
           "ussurveyyard": "us-yd", "ussurveyyards": "us-yd", "ussurveymile": "us-mi", "ussurveymiles": "us-mi"}
GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}
ANNOTATION_TYPES = {"INSERT", "BLOCK_REFERENCE", "TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "POINT", "MLEADER", "MULTILEADER"}
POINT_FIELDS = {"start", "end", "center", "position", "location", "text_position", "base_point",
                "extents_min", "extents_max", "min", "max", "point", "vertices", "curve_points",
                "start_world", "end_world", "center_world", "position_world", "text_position_world",
                "vertices_world", "curve_points_world", "local_extents", "world_extents"}
LENGTH_FIELDS = {"radius", "radius_local", "length", "width", "height", "elevation", "thickness",
                 "height_local", "measurement", "curve_error_mm"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return INSUNITS.get(int(value))
    name = str(value).strip().lower().replace(" ", "").replace("_", "")
    return ALIASES.get(name, name) if ALIASES.get(name, name) in UNIT_FACTORS else None


def _scale_geometry(value: Any, factor: float, *, point: bool = False) -> Any:
    if isinstance(value, list):
        if point and value and all(isinstance(item, (int, float)) for item in value):
            return [item * factor for item in value]
        return [_scale_geometry(item, factor, point=point) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if point and key in {"x", "y", "z"} and isinstance(item, (int, float)):
            result[key] = item * factor
        elif key in POINT_FIELDS:
            result[key] = _scale_geometry(item, factor, point=True)
        elif key in LENGTH_FIELDS and isinstance(item, (int, float)):
            # A supplied curve_error_mm is already expressed in mm.
            result[key] = item if key == "curve_error_mm" else item * factor
        elif key in {"area", "area_world"} and isinstance(item, (int, float)):
            result[key] = item * factor * factor
        elif isinstance(item, (list, dict)):
            result[key] = _scale_geometry(item, factor)
        else:
            result[key] = item
    return result


def _canonical_points(value: Any, *, point: bool = False) -> Any:
    """Accept numeric XY/XYZ arrays at the boundary, then expose one point schema."""
    if isinstance(value, list):
        if point and len(value) in {2, 3} and all(isinstance(item, (int, float)) for item in value):
            return dict(zip(("x", "y", "z"), value))
        return [_canonical_points(item, point=point) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_points(item, point=key in POINT_FIELDS) for key, item in value.items()}
    return value


def _find_autocad() -> Path | None:
    explicit = os.environ.get("DWG_LAYERED_ACCORECONSOLE")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    found = shutil.which("accoreconsole.exe")
    if found and not re.search(r"AutoCAD LT", found, re.IGNORECASE):
        return Path(found)
    root = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Autodesk"
    matches = sorted((path for path in root.glob("AutoCAD */accoreconsole.exe")
                      if re.fullmatch(r"AutoCAD 20\d{2}", path.parent.name)), reverse=True) if root.exists() else []
    return matches[0] if matches else None


def _run(command: list[str], *, directory: Path, env: dict[str, str], timeout: int, log: Path) -> subprocess.CompletedProcess:
    # File output avoids inherited pipe handles making timeout cleanup hang when
    # AutoCAD launches a licensing or reporting subprocess.
    timed_out = False
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=directory, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait(timeout=10)
    value = log.read_bytes()
    encoding = "utf-16-le" if value[:200].count(b"\0") > 15 else "utf-8"
    log.write_text(value.decode(encoding, errors="replace"), encoding="utf-8")
    if timed_out:
        raise RuntimeError(f"engine_timeout_after_{timeout}s; diagnostics={log}")
    return subprocess.CompletedProcess(command, process.returncode, b"", b"")


def _autocad_extract(source: Path, directory: Path, unit: str | None) -> tuple[dict, str]:
    core = _find_autocad()
    if core is None:
        raise RuntimeError("AutoCAD Core Console unavailable. Install AutoCAD or set DWG_LAYERED_ACCORECONSOLE; no DWG fallback guessed.")
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise RuntimeError(".NET SDK unavailable; required to compile packaged AutoCAD reader.")
    helper = Path(__file__).with_name("adapters") / "AutoCadExtract.cs"
    if not helper.is_file():
        raise RuntimeError(f"packaged AutoCAD reader missing: {helper}")
    build = directory / "reader-build"
    build.mkdir()
    shutil.copyfile(helper, build / helper.name)
    refs = "".join(f'<Reference Include="{name}"><HintPath>{escape(str(core.parent / (name.lower()+".dll")))}</HintPath><Private>false</Private></Reference>'
                   for name in ("AcCoreMgd", "AcDbMgd", "AcMgd"))
    (build / "Reader.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0-windows</TargetFramework>'
        '<ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable><GenerateAssemblyInfo>false</GenerateAssemblyInfo>'
        '<CopyLocalLockFileAssemblies>false</CopyLocalLockFileAssemblies><NuGetAudit>false</NuGetAudit></PropertyGroup>'
        f'<ItemGroup>{refs}</ItemGroup></Project>', encoding="utf-8")
    # No package references, and no network restore. Compilation uses installed SDK/reference packs.
    (build / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8")
    env = dict(os.environ)
    env.update({"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "DOTNET_CLI_HOME": str(build / "dotnet-home"), "MSBUILDDISABLENODEREUSE": "1"})
    compiled = _run([dotnet, "build", str(build / "Reader.csproj"), "--configuration", "Release", "--nologo", "--disable-build-servers"],
                    directory=build, env=env, timeout=120, log=directory / "reader-build.log")
    if compiled.returncode:
        raise RuntimeError(f"AutoCAD reader compilation failed; diagnostics={directory / 'reader-build.log'}")
    assembly = build / "bin" / "Release" / "net8.0-windows" / "Reader.dll"
    output = directory / "native-extraction.json"
    script = directory / "read-only.scr"
    # Trusted helper was just compiled inside this run directory. Source data is never code.
    dll_path = str(assembly).replace("\\", "/")
    script.write_text(f'SECURELOAD\n0\n_.NETLOAD\n"{dll_path}"\nDWGLAYEREDREAD\n_.QUIT\n_Y\n', encoding="utf-8")
    env.update({"DWG_LAYERED_INPUT": str(source), "DWG_LAYERED_OUTPUT": str(output)})
    if unit:
        env["DWG_LAYERED_SCALE"] = str(UNIT_FACTORS[unit])
    # Core Console requires an editor document to execute NETLOAD commands.
    # A scratch COPY supplies that context; the reader still reads source in a
    # separate, read-only database so relative external references stay correct.
    bootstrap = core.parent / "Support" / "chroma.dwg"
    if not bootstrap.is_file():
        bootstrap = next(iter(core.parent.glob("Support/*.dwg")), None)
    if bootstrap is None:
        raise RuntimeError("No bootstrap DWG found for Core Console reader")
    scratch = directory / "reader-context.dwg"
    shutil.copyfile(bootstrap, scratch)
    # Isolated registry/profile avoids changing the user's AutoCAD preferences.
    profile = directory / "autocad-profile"
    executed = _run([str(core), "/i", str(scratch), "/readonly", "/s", str(script), "/l", "en-US", "/isolate", "dwglayered_" + directory.name, str(profile)], directory=directory,
                    env=env, timeout=240, log=directory / "core-console.log")
    if not output.is_file():
        raise RuntimeError(f"AutoCAD extraction produced no data (exit {executed.returncode}); diagnostics={directory / 'core-console.log'}")
    result = json.loads(output.read_text(encoding="utf-8-sig"))
    if result.get("error"):
        raise RuntimeError(f"AutoCAD could not read source: {result['error']}; diagnostics={directory / 'core-console.log'}")
    if executed.returncode:
        raise RuntimeError(f"AutoCAD exited with {executed.returncode}; diagnostics={directory / 'core-console.log'}")
    return result, f"autocad:{core.parent.name}"


def _dependencies(data: dict, source: Path, prior: dict) -> list[dict]:
    dependencies = []
    records = list(prior.get("dependencies") or [])
    original_path = (data.get("database") or {}).get("filename")
    reference_base = Path(original_path).parent if original_path and Path(original_path).is_absolute() else source.parent
    for block in list(data.get("blocks") or []) + list(data.get("block_definitions") or []):
        if block.get("is_xref") or block.get("is_from_external_reference") or block.get("xref_status"):
            records.append({"path": block.get("path") or block.get("path_name") or block.get("xref_path"),
                            "xref_status": block.get("xref_status"), "block_name": block.get("name")})
    seen = set()
    for record in records:
        name = record.get("path")
        if not name:
            dependencies.append({**record, "path": None, "sha256": None, "status": "missing_path"})
            continue
        path = Path(name)
        if not path.is_absolute():
            path = reference_base / path
        path = path.resolve()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        status = "resolved" if path.is_file() else "missing"
        xref_status = str(record.get("xref_status") or "").lower()
        if status == "resolved" and xref_status and xref_status not in {"resolved", "notanxref"}:
            status = xref_status
        digest = _sha256(path) if path.is_file() else None
        if record.get("sha256") and digest != record["sha256"]:
            status = "changed"
        dependencies.append({**record, "path": str(path), "sha256": digest, "status": status})
    return dependencies


def _provenance(data: dict, source: Path, digest: str, dependencies: list[dict]) -> None:
    hashes = {str(source).casefold(): digest}
    hashes.update({str(item["path"]).casefold(): item["sha256"] for item in dependencies if item.get("sha256")})
    instance_layers = {}
    for entity in data["entities"]:
        if entity.get("type") == "INSERT" and entity.get("owner_block") == "*Model_Space":
            instance_layers[str(entity.get("handle"))] = entity.get("effective_layer") or entity.get("layer") or "0"
    for entity in sorted(data["resolved_instances"], key=lambda item: str(item.get("instance_path", "")).count("/")):
        if entity.get("type") == "BLOCK_REFERENCE":
            path = str(entity.get("instance_path") or "")
            layer = entity.get("layer") or "0"
            inherited = instance_layers.get(path.rsplit("/", 1)[0], "0")
            instance_layers[path] = entity.get("effective_layer") or (inherited if layer == "0" else layer)
    for collection in (data["entities"], data["resolved_instances"]):
        for entity in collection:
            original = str(entity.get("original_layer") or entity.get("layer") or "0")
            path = str(entity.get("instance_path") or "")
            inherited = instance_layers.get(path, "0")
            entity["original_layer"] = original
            entity.setdefault("block_layer", inherited if path else None)
            effective = str(entity.get("effective_layer") or (inherited if original == "0" and path else original))
            entity["effective_layer"] = effective
            entity["layer"] = effective
            file = str(entity.get("source_file") or source)
            entity["source_file"] = file
            if file.casefold() not in hashes:
                candidate = Path(file)
                hashes[file.casefold()] = _sha256(candidate) if candidate.is_file() else str(entity.get("source_id") or digest)
            handle = str(entity.get("source_handle") or entity.get("handle") or "")
            entity["source_handle"] = handle
            entity.setdefault("handle", handle)
            entity["instance_path"] = path
            source_id = hashes.get(file.casefold(), digest)[:16]
            identity = f"{source_id}:{path}:{handle}"
            entity["source_id"] = source_id
            entity["entity_id"] = identity
            entity["source_handles"] = [handle] if handle else []
            entity["source_layers"] = [effective]
            if entity.get("curve_points") or entity.get("curve_points_world"):
                entity.setdefault("curve_error_mm", .5)


def extract_document(source: Path | str, *, output_dir: Path | str, backend: str = "auto", unit: str | None = None) -> dict:
    """Extract ``source`` into mm geometry and save ``output_dir/normalized.json``.

    ``backend`` is ``auto``, ``autocad`` or ``json``. Unitless documents require
    an explicit ``unit``; an explicit unit always appears in audit warnings.
    Consumers MUST reject nonempty ``intake.blockers`` until corrected. Reported
    unsupported objects require explicit exclusion/review before modeling.
    Each engine invocation has bounded build/extraction timeouts (120/240 sec).
    """
    source = Path(source).resolve()
    directory = Path(output_dir).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if backend not in {"auto", "autocad", "json"}:
        raise ValueError(f"Unknown extraction backend: {backend}")
    if unit is not None and _unit(unit) is None:
        raise ValueError(f"Unsupported explicit unit: {unit}")
    if directory == source.parent and source.name == "normalized.json":
        raise ValueError("Output must not overwrite the input normalized.json")
    directory.mkdir(parents=True, exist_ok=True)
    digest = _sha256(source)
    intake = {"schema": SCHEMA, "source_path": str(source), "source_sha256": digest, "units": "mm",
              "input_units": None, "scale_to_mm": None, "backend": backend, "warnings": [], "blockers": [],
              "unsupported": [], "dependencies": [], "stats": {}}
    data: dict[str, Any] = {"entities": [], "resolved_instances": [], "layers": []}
    suffix = source.suffix.lower()
    explicit = _unit(unit)
    started = time.monotonic()
    try:
        if suffix == ".json" and backend in {"auto", "json"}:
            value = json.loads(source.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or not isinstance(value.get("entities"), list):
                raise ValueError("normalized JSON must be an object with an entities array")
            data = copy.deepcopy(value)
            intake["backend"] = "normalized-json"
        elif suffix in {".dwg", ".dxf"} and backend in {"auto", "autocad"}:
            # Each run gets its own helper/cache/logs; stale JSON cannot masquerade as a new extraction.
            import uuid
            run = directory / ("engine-" + uuid.uuid4().hex[:12])
            run.mkdir()
            data, intake["backend"] = _autocad_extract(source, run, explicit)
            intake["engine_directory"] = str(run)
        else:
            raise ValueError(f"Unsupported input/backend combination: {suffix}/{backend}")
    except (OSError, ValueError, RuntimeError) as exc:
        intake["blockers"].append("extraction_failed: " + str(exc))
    data.setdefault("entities", [])
    data.setdefault("resolved_instances", [])
    prior = data.get("intake") or {}
    metadata = data.get("database") or {}
    declared = _unit(prior.get("units") or data.get("units") or metadata.get("insertion_units") or metadata.get("insunits"))
    input_unit = explicit or declared
    if prior.get("units") == "mm" and explicit and explicit != "mm":
        intake["blockers"].append("already_normalized_mm_cannot_be_reinterpreted; omit unit override")
        input_unit = "mm"
    if not input_unit:
        intake["blockers"].append("units_unconfirmed: supply an explicit unit (for legacy mm JSON use unit='mm')")
    if explicit:
        intake["warnings"].append(f"explicit_input_unit={explicit}; metadata_unit={declared or 'unspecified'}")
    intake["input_units"] = input_unit
    factor = UNIT_FACTORS[input_unit] if input_unit else 1.0
    intake["scale_to_mm"] = factor if input_unit else None
    for field in ("entities", "resolved_instances"):
        if not isinstance(data[field], list) or any(not isinstance(entity, dict) for entity in data[field]):
            intake["blockers"].append(f"invalid_{field}_array")
            data[field] = []
        if factor != 1:
            data[field] = [_scale_geometry(entity, factor) for entity in data[field]]
        data[field] = [_canonical_points(entity) for entity in data[field]]
    for issue in data.get("blockers", []) + prior.get("blockers", []):
        if issue not in intake["blockers"]:
            intake["blockers"].append(issue)
    intake["warnings"].extend(data.get("warnings") or [])
    intake["dependencies"] = _dependencies(data, source, prior)
    for dependency in intake["dependencies"]:
        if dependency["status"] != "resolved":
            intake["blockers"].append(f"xref_{dependency['status']}: {dependency['path']}")
    _provenance(data, source, digest, intake["dependencies"])
    placed = [item for item in data["entities"] if item.get("owner_block") == "*Model_Space"]
    placed.extend(item for item in data["resolved_instances"] if item.get("root_space") == "*Model_Space")
    unsupported = list(data.get("unsupported") or prior.get("unsupported") or [])
    unsupported_ids = {(str(item.get("instance_path") or ""), str(item.get("handle") or item.get("source_handle") or "")) for item in unsupported}
    valid_geometry = 0
    for entity in placed:
        typ = str(entity.get("type") or "")
        if not entity.get("source_handle"):
            intake["blockers"].append("placed_object_missing_source_handle")
        if typ in GEOMETRY_TYPES and not entity.get("unsupported"):
            keys = {"LINE": ("start_world", "start"), "LWPOLYLINE": ("vertices_world", "vertices"), "POLYLINE": ("vertices_world", "vertices"),
                    "ARC": ("curve_points_world", "center_world", "center"), "CIRCLE": ("curve_points_world", "center_world", "center"),
                    "ELLIPSE": ("curve_points_world", "curve_points"), "SPLINE": ("curve_points_world", "curve_points")}[typ]
            def point_ok(value: Any) -> bool:
                return isinstance(value, dict) and all(isinstance(value.get(axis), (int, float)) for axis in ("x", "y"))
            geometry_ok = any(entity.get(key) is not None for key in keys)
            if typ == "LINE":
                geometry_ok = point_ok(entity.get("start_world") or entity.get("start")) and point_ok(entity.get("end_world") or entity.get("end"))
            elif typ in {"LWPOLYLINE", "POLYLINE"}:
                vertices = entity.get("vertices_world") or entity.get("vertices") or []
                geometry_ok = len(vertices) >= 2 and all(point_ok(vertex.get("point", vertex)) for vertex in vertices)
            if not geometry_ok:
                intake["blockers"].append("geometry_payload_missing: " + entity["entity_id"])
            else:
                valid_geometry += 1
        elif typ not in ANNOTATION_TYPES or entity.get("unsupported"):
            key = (entity["instance_path"], entity["source_handle"])
            if key not in unsupported_ids:
                unsupported.append({"type": typ, "handle": entity["source_handle"], "layer": entity["layer"],
                                    "instance_path": entity["instance_path"], "reason": "no_geometry_adapter"})
                unsupported_ids.add(key)
    if not placed:
        intake["blockers"].append("empty_model_space: no placed objects extracted")
    elif valid_geometry == 0:
        intake["blockers"].append("no_supported_geometry_in_model_space")
    # Reject non-finite coordinates before any classifier/modeler consumes them.
    def finite(value: Any) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, dict):
            return all(finite(item) for item in value.values())
        if isinstance(value, list):
            return all(finite(item) for item in value)
        return True
    if not finite(data.get("entities")) or not finite(data.get("resolved_instances")):
        intake["blockers"].append("non_finite_geometry")
        def sanitize(value: Any) -> Any:
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, list):
                return [sanitize(item) for item in value]
            if isinstance(value, dict):
                return {key: sanitize(item) for key, item in value.items()}
            return value
        data["entities"] = sanitize(data["entities"])
        data["resolved_instances"] = sanitize(data["resolved_instances"])
    if _sha256(source) != digest:
        intake["blockers"].append("source_changed_during_extraction")
    intake["unsupported"] = unsupported
    intake["blockers"] = list(dict.fromkeys(intake["blockers"]))
    intake["stats"] = {"entities": len(data["entities"]), "resolved_instances": len(data["resolved_instances"]),
                       "placed_objects": len(placed), "supported_geometry": valid_geometry,
                       "unsupported_objects": len(unsupported), "layers": len(data.get("layers") or []),
                       "duration_seconds": round(time.monotonic() - started, 3)}
    data["intake"] = intake
    data["units"] = "mm"
    (directory / "normalized.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return data
