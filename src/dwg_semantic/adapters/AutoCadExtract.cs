// Read-only side-database and active-database extraction. No source writes.
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Text.Json;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(DwgLayeredIntake.Commands))]
namespace DwgLayeredIntake;

public sealed class Commands
{
    const int MaxDepth = 32, MaxExpanded = 500000, MaxCurvePoints = 16384;
    readonly List<Dictionary<string, object?>> entities = new(), resolved = new(), unsupported = new();
    readonly List<string> warnings = new(), blockers = new();
    readonly Dictionary<string, int> annotationCounts = new();
    readonly HashSet<string> annotationWarningKeys = new();
    Transaction tr = null!;
    double chordError = .5;
    string sourcePath = "";

    static Dictionary<string, object?> P(Point3d p) => new() { ["x"] = p.X, ["y"] = p.Y, ["z"] = p.Z };
    static bool IsModel(string name) => string.Equals(name, BlockTableRecord.ModelSpace, StringComparison.OrdinalIgnoreCase);
    static Point3d World(Point3d p, List<Matrix3d> transforms)
    { for (int i = transforms.Count - 1; i >= 0; i--) p = p.TransformBy(transforms[i]); return p; }
    static object Bounds(IEnumerable<Point3d> points)
    {
        var a = points.ToList();
        return new Dictionary<string, object?> { ["min"] = P(new Point3d(a.Min(p => p.X), a.Min(p => p.Y), a.Min(p => p.Z))),
            ["max"] = P(new Point3d(a.Max(p => p.X), a.Max(p => p.Y), a.Max(p => p.Z))) };
    }

    [CommandMethod("DWGLAYEREDREAD")]
    public void Read()
    {
        entities.Clear(); resolved.Clear(); unsupported.Clear(); warnings.Clear(); blockers.Clear();
        annotationCounts.Clear(); annotationWarningKeys.Clear();
        sourcePath = Environment.GetEnvironmentVariable("DWG_LAYERED_INPUT") ?? "";
        string output = Environment.GetEnvironmentVariable("DWG_LAYERED_OUTPUT") ?? "";
        var doc = Application.DocumentManager.MdiActiveDocument;
        try
        {
            using var db = new Database(false, true);
            if (Path.GetExtension(sourcePath).Equals(".dxf", StringComparison.OrdinalIgnoreCase)) db.DxfIn(sourcePath, null);
            else { db.ReadDwgFile(sourcePath, FileOpenMode.OpenForReadAndAllShare, false, null); db.CloseInput(true); }
            double factor;
            if (!double.TryParse(Environment.GetEnvironmentVariable("DWG_LAYERED_SCALE"), System.Globalization.NumberStyles.Float,
                    System.Globalization.CultureInfo.InvariantCulture, out factor))
                factor = db.Insunits == UnitsValue.Undefined ? 1 : UnitsConverter.GetConversionFactor(db.Insunits, UnitsValue.Millimeters);
            chordError = .5 / factor;
            var previous = HostApplicationServices.WorkingDatabase;
            Dictionary<string, object?> payload;
            try
            {
                HostApplicationServices.WorkingDatabase = db;
                // Resolves external references in memory; never binds or saves.
                try { db.ResolveXrefs(false, false); }
                catch (System.Exception ex) { blockers.Add("xref_resolution_failed: " + ex.Message); }
                payload = Extract(db);
            }
            finally { HostApplicationServices.WorkingDatabase = previous; }
            File.WriteAllText(output, JsonSerializer.Serialize(payload));
            doc.Editor.WriteMessage("\nDWGLAYEREDREAD OK\n");
        }
        catch (System.Exception ex)
        {
            File.WriteAllText(output, JsonSerializer.Serialize(new { error = ex.ToString() }));
            doc.Editor.WriteMessage("\nDWGLAYEREDREAD ERROR: " + ex.Message + "\n");
        }
    }

    // Live whole-model evidence is a different envelope from Pickfirst. Read
    // the current in-memory database, including unsaved edits, under AutoCAD's
    // command lock. Never select-all, save, reload xrefs or edit the database.
    [CommandMethod("DWGLAYEREDSNAPSHOT", CommandFlags.Modal)]
    public void ReadActive()
    {
        var doc = Application.DocumentManager.MdiActiveDocument;
        var prompt = new PromptStringOptions("\nSnapshot request JSON: ") { AllowSpaces = true };
        var answer = doc.Editor.GetString(prompt);
        if (answer.Status != PromptStatus.OK) return;
        string requestPath = Path.GetFullPath(answer.StringResult.Trim().Trim('"'));
        string output = "";
        try
        {
            using var request = JsonDocument.Parse(File.ReadAllText(requestPath));
            var r = request.RootElement;
            if (r.GetProperty("schema").GetString() != "dwg-cad-model-read-request-v1")
                throw new InvalidOperationException("Invalid snapshot request schema");
            output = Path.GetFullPath(r.GetProperty("output").GetString()!);
            if (Path.GetDirectoryName(output) != Path.GetDirectoryName(requestPath) || File.Exists(output))
                throw new InvalidOperationException("Snapshot output must be a new sibling file");
            sourcePath = Path.GetFullPath(r.GetProperty("source_path").GetString()!);
            if (!string.Equals(Path.GetFullPath(doc.Name), sourcePath, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Active CAD document differs from the bound source");
            var implied = doc.Editor.SelectImplied();
            int selected = implied.Status == PromptStatus.OK ? implied.Value.Count : 0;
            if (selected != 0) throw new InvalidOperationException("Whole-model read requires an empty Pickfirst selection");
            double factor = r.GetProperty("scale_to_mm").GetDouble();
            if (!double.IsFinite(factor) || factor <= 0) throw new InvalidOperationException("Verified unit scale is required");
            entities.Clear(); resolved.Clear(); unsupported.Clear(); warnings.Clear(); blockers.Clear();
            annotationCounts.Clear(); annotationWarningKeys.Clear();
            chordError = .5 / factor;
            var db = doc.Database;
            var fingerprint = db.FingerprintGuid.ToString();
            var payload = Extract(db);
            var finalSelection = doc.Editor.SelectImplied();
            if (Application.DocumentManager.MdiActiveDocument != doc ||
                (finalSelection.Status == PromptStatus.OK && finalSelection.Value.Count != 0) ||
                db.FingerprintGuid.ToString() != fingerprint)
                throw new InvalidOperationException("CAD document or selection changed during the complete read");
            var result = new {
                schema = "dwg-cad-model-snapshot-v1", success = true,
                request_id = r.GetProperty("request_id").GetString(),
                observed_at_utc = DateTimeOffset.UtcNow.ToString("O"),
                document = new { path = sourcePath, name = Path.GetFileName(sourcePath), fingerprint_guid = fingerprint },
                scope = "complete_database_including_definitions_and_placed_instances",
                selection_source = "Editor.SelectImplied", selection_count = 0,
                truncated = false, complete = blockers.Count == 0,
                entity_count = entities.Count, resolved_instance_count = resolved.Count,
                native_extract = payload,
                reader = "active_database_read_only_open_close_transaction_no_save_no_xref_reload"
            };
            using var stream = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            JsonSerializer.Serialize(stream, result);
            doc.Editor.WriteMessage("\nDWGLAYEREDSNAPSHOT OK\n");
        }
        catch (System.Exception ex)
        {
            if (output.Length > 0 && !File.Exists(output))
            {
                using var stream = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
                JsonSerializer.Serialize(stream, new { success = false, error = ex.Message });
            }
            doc.Editor.WriteMessage("\nDWGLAYEREDSNAPSHOT ERROR: " + ex.Message + "\n");
        }
    }

    Dictionary<string, object?> Extract(Database db)
    {
        using var transaction = db.TransactionManager.StartOpenCloseTransaction(); tr = transaction;
        var layers = new List<object>();
        foreach (ObjectId id in (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead))
        {
            var layer = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
            layers.Add(new { name = layer.Name, handle = layer.Handle.ToString(), is_off = layer.IsOff,
                is_frozen = layer.IsFrozen, is_locked = layer.IsLocked, is_plottable = layer.IsPlottable });
        }
        var blocks = new List<object>();
        var table = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        foreach (ObjectId id in table)
        {
            var block = (BlockTableRecord)tr.GetObject(id, OpenMode.ForRead);
            bool external = block.IsFromExternalReference || block.IsFromOverlayReference;
            blocks.Add(new { name = block.Name, handle = block.Handle.ToString(), is_xref = external,
                path = external ? block.PathName : null, xref_status = external ? block.XrefStatus.ToString() : null });
            // Keep definition entities for classification evidence; expand only layouts below.
            foreach (ObjectId eid in block)
            {
                try
                {
                    if (tr.GetObject(eid, OpenMode.ForRead) is not Entity entity) continue;
                    var record = Geometry(entity, new List<Matrix3d>(), block.Name, "", entity.Layer, null, sourcePath, false);
                    entities.Add(record);
                }
                catch (System.Exception ex) { blockers.Add("entity_read_failed " + eid.Handle + ": " + ex.Message); }
            }
            if (block.IsLayout)
                Expand(block, new List<Matrix3d>(), block.Name, "", "0", sourcePath, new HashSet<ObjectId>(), 0);
        }
        return new() { ["schema"] = "dwg-native-extract-v3", ["database"] = new { filename = sourcePath,
            insertion_units = db.Insunits.ToString(), insunits = (int)db.Insunits }, ["layers"] = layers,
            ["blocks"] = blocks, ["entities"] = entities, ["resolved_instances"] = resolved,
            ["unsupported"] = unsupported, ["warnings"] = warnings, ["blockers"] = blockers,
            ["annotation_extraction"] = new {
                schema = "dwg-annotation-instances-v1", counts = annotationCounts,
                attached_attributes = "Actual BlockReference.AttributeCollection values; inline attributes and placed ATTRIB rows; ancestor transforms only.",
                attribute_definitions = "ATTDEF default_text is definition evidence, never a substituted instance value.",
                mleader_text = "Embedded cloned MText with leader/leader-line indexes and source/world vertices.",
                mleader_block_attributes = "Content block identity retained; block-content attribute values are not extracted.",
                source_access = "read_only_no_regeneration_no_attribute_default_fallback" } };
    }

    void Expand(BlockTableRecord block, List<Matrix3d> transforms, string root, string path,
                string inheritedLayer, string file, HashSet<ObjectId> stack, int depth)
    {
        if (depth > MaxDepth || stack.Contains(block.ObjectId))
        { blockers.Add("cyclic_or_too_deep_block: " + path); return; }
        stack = new HashSet<ObjectId>(stack) { block.ObjectId };
        foreach (ObjectId id in block)
        {
            if (resolved.Count >= MaxExpanded) { blockers.Add("expanded_entity_limit_exceeded"); return; }
            try
            {
                if (tr.GetObject(id, OpenMode.ForRead) is not Entity entity) continue;
                string effective = entity.Layer == "0" ? inheritedLayer : entity.Layer;
                if (entity is BlockReference br)
                {
                    var child = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead);
                    string childPath = path.Length == 0 ? br.Handle.ToString() : path + "/" + br.Handle;
                    var chain = new List<Matrix3d>(transforms) { br.BlockTransform };
                    if (IsModel(root))
                    {
                        CountAnnotation("placed_block_instances_scanned");
                        var attributes = AttachedAttributes(br, transforms, root, childPath, effective, file, true);
                        if (attributes.Count > 0) CountAnnotation("placed_block_instances_with_attached_attributes");
                        foreach (var attribute in attributes)
                        {
                            if (resolved.Count >= MaxExpanded) { blockers.Add("expanded_entity_limit_exceeded"); return; }
                            attribute["source_block"] = child.Name;
                            resolved.Add(attribute);
                        }
                    }
                    // Direct INSERT stays in entities; expanded reference gives nested semantic evidence.
                    if (path.Length > 0)
                    {
                        var reference = Geometry(br, transforms, root, childPath, effective, inheritedLayer, file, true);
                        reference["type"] = "BLOCK_REFERENCE"; resolved.Add(reference);
                    }
                    string childFile = file;
                    if (child.IsFromExternalReference || child.IsFromOverlayReference)
                    {
                        childFile = child.PathName;
                        if (!Path.IsPathRooted(childFile)) childFile = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(file)!, childFile));
                        if (child.XrefStatus != XrefStatus.Resolved) { blockers.Add("unresolved_xref: " + childFile + ": " + child.XrefStatus); continue; }
                    }
                    Expand(child, chain, root, childPath, effective, childFile, stack, depth + 1);
                }
                else if (path.Length > 0)
                {
                    var leaf = Geometry(entity, transforms, root, path, effective, inheritedLayer, file, true);
                    leaf["source_block"] = block.Name;
                    resolved.Add(leaf);
                }
            }
            catch (System.Exception ex) { blockers.Add("instance_read_failed " + path + "/" + id.Handle + ": " + ex.Message); }
        }
    }

    Dictionary<string, object?> Geometry(Entity entity, List<Matrix3d> transforms, string owner, string path,
                                        string effective, string? blockLayer, string file, bool isResolved)
    {
        var d = new Dictionary<string, object?> { ["type"] = entity.GetRXClass().DxfName, ["handle"] = entity.Handle.ToString(),
            ["source_handle"] = entity.Handle.ToString(), ["owner_block"] = owner, ["root_space"] = owner,
            ["layer"] = effective, ["original_layer"] = entity.Layer, ["effective_layer"] = effective,
            ["block_layer"] = blockLayer, ["instance_path"] = path, ["source_file"] = file };
        Point3d W(Point3d p) => World(p, transforms);
        void Points(string name, IEnumerable<Point3d> points)
        {
            var local = points.ToList(); var world = local.Select(W).ToList();
            d[name] = local.Select(P).ToList(); d[name + "_world"] = world.Select(P).ToList();
            if (local.Count > 0) { d["local_extents"] = Bounds(local); d["world_extents"] = Bounds(world); }
        }
        try
        {
            var ex = entity.GeometricExtents;
            d["local_extents"] = new { min = P(ex.MinPoint), max = P(ex.MaxPoint) };
            var corners = new List<Point3d>();
            foreach (double x in new[] { ex.MinPoint.X, ex.MaxPoint.X })
            foreach (double y in new[] { ex.MinPoint.Y, ex.MaxPoint.Y })
            foreach (double z in new[] { ex.MinPoint.Z, ex.MaxPoint.Z }) corners.Add(W(new Point3d(x,y,z)));
            d["world_extents"] = Bounds(corners);
        } catch { }
        switch (entity)
        {
            case Line line:
                d["start"] = P(line.StartPoint); d["end"] = P(line.EndPoint);
                d["start_world"] = P(W(line.StartPoint)); d["end_world"] = P(W(line.EndPoint)); break;
            case Polyline poly:
                d["type"] = "LWPOLYLINE"; d["closed"] = poly.Closed;
                var vertices = Enumerable.Range(0, poly.NumberOfVertices).Select(poly.GetPoint3dAt).ToList();
                Points("vertices", vertices); d["bulges"] = Enumerable.Range(0, poly.NumberOfVertices).Select(poly.GetBulgeAt).ToArray();
                d["area"] = poly.Area;
                var plane = Matrix3d.PlaneToWorld(poly.Normal);
                var axisX = Vector3d.XAxis.TransformBy(plane); var axisY = Vector3d.YAxis.TransformBy(plane);
                for (int ti = transforms.Count - 1; ti >= 0; ti--)
                { axisX = axisX.TransformBy(transforms[ti]); axisY = axisY.TransformBy(transforms[ti]); }
                d["area_world"] = poly.Area * axisX.CrossProduct(axisY).Length;
                if (Enumerable.Range(0, poly.NumberOfVertices).Any(i => Math.Abs(poly.GetBulgeAt(i)) > 1e-12))
                {
                    var sampled = new List<Point3d>(); int count = poly.Closed ? vertices.Count : vertices.Count - 1;
                    for (int i=0; i<count; i++)
                    {
                        double sweep = 4 * Math.Atan(Math.Abs(poly.GetBulgeAt(i)));
                        int steps = 1;
                        if (sweep > 1e-12)
                        {
                            double chord = vertices[i].DistanceTo(vertices[(i+1)%vertices.Count]);
                            double radius = chord / (2 * Math.Sin(sweep/2));
                            steps = CurveSteps(radius, sweep, transforms);
                        }
                        if (sampled.Count + steps + 1 > MaxCurvePoints) throw new InvalidOperationException("polyline_tessellation_limit_exceeded");
                        for(int j=0;j<steps;j++) sampled.Add(poly.GetPointAtParameter(i+(double)j/steps));
                    }
                    if (count > 0) sampled.Add(poly.GetPointAtParameter(count));
                    Points("curve_points", sampled);
                } break;
            case Polyline2d poly2:
                d["type"] = "POLYLINE"; d["polyline_kind"] = "legacy_2d"; d["closed"] = poly2.Closed;
                var pv2 = new List<Point3d>(); var pb2 = new List<double>();
                foreach (ObjectId vertexId in poly2)
                {
                    if (tr.GetObject(vertexId, OpenMode.ForRead) is not Vertex2d vertex) continue;
                    // Parameters return WCS points, including OCS elevation/normal.
                    pv2.Add(poly2.GetPointAtParameter(pv2.Count)); pb2.Add(vertex.Bulge);
                }
                Points("vertices", pv2); d["bulges"] = pb2;
                if (pb2.Any(b => Math.Abs(b) > 1e-12))
                {
                    var samples2 = new List<Point3d>(); int edges2 = poly2.Closed ? pv2.Count : pv2.Count - 1;
                    for (int i=0; i<edges2; i++)
                    {
                        double sweep2 = 4*Math.Atan(Math.Abs(pb2[i])); int steps2 = 1;
                        if (sweep2 > 1e-12)
                        {
                            double chord2 = pv2[i].DistanceTo(pv2[(i+1)%pv2.Count]);
                            steps2 = CurveSteps(chord2/(2*Math.Sin(sweep2/2)), sweep2, transforms);
                        }
                        if (samples2.Count + steps2 + 1 > MaxCurvePoints) throw new InvalidOperationException("legacy_polyline_tessellation_limit_exceeded");
                        for(int j=0;j<steps2;j++) samples2.Add(poly2.GetPointAtParameter(i+(double)j/steps2));
                    }
                    if (edges2 > 0) samples2.Add(poly2.GetPointAtParameter(edges2));
                    Points("curve_points", samples2);
                }
                break;
            case Polyline3d poly3:
                d["type"] = "POLYLINE"; d["polyline_kind"] = "legacy_3d"; d["closed"] = poly3.Closed;
                var pv3 = new List<Point3d>();
                foreach (ObjectId vertexId in poly3)
                    if (tr.GetObject(vertexId, OpenMode.ForRead) is PolylineVertex3d vertex3) pv3.Add(vertex3.Position);
                Points("vertices", pv3); d["bulges"] = pv3.Select(_ => 0.0).ToArray();
                break;
            case Arc arc:
                d["type"] = "ARC"; d["center"] = P(arc.Center); d["center_world"] = P(W(arc.Center));
                d["radius"] = arc.Radius; d["start_angle"] = arc.StartAngle; d["end_angle"] = arc.EndAngle;
                d["start_world"] = P(W(arc.StartPoint)); d["end_world"] = P(W(arc.EndPoint));
                int arcSteps = CurveSteps(arc.Radius, arc.TotalAngle, transforms);
                Points("curve_points", Enumerable.Range(0, arcSteps+1).Select(i => arc.GetPointAtParameter(arc.StartParam+(arc.EndParam-arc.StartParam)*i/arcSteps))); break;
            case Circle circle:
                d["center"] = P(circle.Center); d["center_world"] = P(W(circle.Center)); d["radius"] = circle.Radius;
                d["closed"] = true; int circleSteps = CurveSteps(circle.Radius, 2*Math.PI, transforms);
                Points("curve_points", Enumerable.Range(0, circleSteps+1).Select(i => circle.GetPointAtParameter(2*Math.PI*i/circleSteps))); break;
            case BlockReference br:
                var child = (BlockTableRecord)tr.GetObject(br.BlockTableRecord, OpenMode.ForRead);
                d["type"] = "INSERT"; d["block_name"] = child.Name; d["position"] = P(br.Position); d["position_world"] = P(W(br.Position));
                if (br.IsDynamicBlock) d["dynamic_block_name"] = ((BlockTableRecord)tr.GetObject(br.DynamicBlockTableRecord, OpenMode.ForRead)).Name;
                string attributePath = path.Length == 0 ? br.Handle.ToString() : path;
                d["attributes"] = AttachedAttributes(br, transforms, owner, attributePath, effective, file, false);
                d["attributes_are_instance_values"] = true;
                d["attribute_coordinates_resolved"] = isResolved || IsModel(owner);
                break;
            case AttributeDefinition definition:
                d["type"] = "ATTDEF"; d["tag"] = definition.Tag; d["text"] = definition.TextString;
                d["default_text"] = definition.TextString; d["is_instance_value"] = false;
                d["annotation_role"] = "attribute_definition_default";
                d["position"] = P(definition.Position); d["position_world"] = P(W(definition.Position)); break;
            case AttributeReference attribute:
                d["type"] = "ATTRIB"; d["tag"] = attribute.Tag; d["text"] = attribute.TextString;
                d["text_raw"] = attribute.TextString; d["is_instance_value"] = true;
                d["annotation_role"] = "attached_attribute_instance"; d["invisible"] = attribute.Invisible;
                d["position"] = P(attribute.Position); d["position_world"] = P(W(attribute.Position));
                if (attribute.IsMTextAttribute)
                {
                    try
                    {
                        using var attributeText = attribute.MTextAttribute;
                        if (attributeText != null) { d["text"] = attributeText.Text; d["text_contents"] = attributeText.Contents; }
                    }
                    catch (System.Exception ex) { AnnotationWarning("attribute_mtext_unavailable", path, entity.Handle.ToString(), ex); }
                }
                break;
            case DBText text: d["text"] = text.TextString; d["position"] = P(text.Position); d["position_world"] = P(W(text.Position)); break;
            case MText text: d["text"] = text.Text; d["position"] = P(text.Location); d["position_world"] = P(W(text.Location)); break;
            case MLeader leader:
                ReadMLeader(leader, transforms, d, IsModel(owner), path); break;
            case Dimension dimension:
                d["dimension_text"] = dimension.DimensionText;
                // Some legacy dimensions lazily regenerate on Measurement and
                // demand write access. Preserve the annotation without upgrading
                // the source object to write mode.
                try { d["measurement"] = dimension.Measurement; }
                catch (System.Exception ex) { warnings.Add("dimension_measurement_unavailable " + entity.Handle + ": " + ex.Message); }
                break;
            case Hatch hatch:
                // Boundary evidence only: a hatch is not automatically a slab
                // region or material solid. Keep its unsupported appearance flag.
                var hatchLoops = new List<object>();
                for (int hi = 0; hi < hatch.NumberOfLoops; hi++)
                {
                    try
                    {
                        var hl = hatch.GetLoopAt(hi);
                        if (!hl.IsPolyline)
                        {
                            var curves = new List<object>(); bool supportedLoop = true;
                            foreach (Curve2d curve in hl.Curves)
                            {
                                List<Point2d> cp;
                                if (curve is LineSegment2d) cp = new() { curve.StartPoint, curve.EndPoint };
                                else if (curve is CircularArc2d ca)
                                {
                                    var interval = ca.GetInterval();
                                    int count = CurveSteps(ca.Radius, Math.Abs(interval.UpperBound-interval.LowerBound), transforms);
                                    cp = ca.GetSamplePoints(count+1).ToList();
                                }
                                else
                                {
                                    supportedLoop = false;
                                    curves.Add(new { type = curve.GetType().Name, supported = false });
                                    continue;
                                }
                                var p3 = cp.Select(p => new Point3d(p.X,p.Y,hatch.Elevation).TransformBy(Matrix3d.PlaneToWorld(hatch.Normal))).ToList();
                                curves.Add(new { type = curve.GetType().Name, supported = true,
                                    curve_points = p3.Select(P).ToList(), curve_points_world = p3.Select(p => P(W(p))).ToList() });
                            }
                            hatchLoops.Add(new { loop_index = hi, supported = supportedLoop, closed = true,
                                loop_type = hatch.LoopTypeAt(hi).ToString(), curves });
                            if (!supportedLoop) warnings.Add("hatch_boundary_curve_type_unavailable " + entity.Handle + ":" + hi);
                            continue;
                        }
                        var hp = new List<Point3d>(); var hb = new List<double>();
                        foreach (BulgeVertex vertex in hl.Polyline)
                        {
                            hp.Add(new Point3d(vertex.Vertex.X, vertex.Vertex.Y, hatch.Elevation).TransformBy(Matrix3d.PlaneToWorld(hatch.Normal)));
                            hb.Add(vertex.Bulge);
                        }
                        hatchLoops.Add(new { loop_index = hi, supported = true, closed = true,
                            loop_type = hatch.LoopTypeAt(hi).ToString(), vertices = hp.Select(P).ToList(),
                            vertices_world = hp.Select(p => P(W(p))).ToList(), bulges = hb });
                    }
                    catch (System.Exception ex) { warnings.Add("hatch_boundary_unavailable " + entity.Handle + ":" + hi + ": " + ex.Message); }
                }
                d["boundary_loops"] = hatchLoops;
                d["pattern_name"] = hatch.PatternName; d["solid_fill"] = hatch.IsSolidFill;
                d["boundary_role"] = "source_reference_evidence_only";
                d["unsupported"] = true;
                if (IsModel(owner))
                    unsupported.Add(new() { ["type"] = d["type"], ["handle"] = entity.Handle.ToString(), ["layer"] = effective,
                        ["instance_path"] = path, ["reason"] = "reference_boundary_only", ["source_file"] = file });
                break;
            default:
                d["unsupported"] = true;
                // Definition entities are inventoried; only placed model geometry blocks the workflow.
                if (IsModel(owner))
                    unsupported.Add(new() { ["type"] = d["type"], ["handle"] = entity.Handle.ToString(), ["layer"] = effective,
                        ["instance_path"] = path, ["reason"] = "no_geometry_adapter", ["source_file"] = file });
                break;
        }
        return d;
    }

    void CountAnnotation(string key)
    { annotationCounts[key] = annotationCounts.GetValueOrDefault(key) + 1; }

    void AnnotationWarning(string kind, string path, string handle, System.Exception? error = null)
    {
        string key = kind + " " + path + "/" + handle;
        if (annotationWarningKeys.Add(key)) warnings.Add(key + (error == null ? "" : ": " + error.Message));
    }

    List<Dictionary<string, object?>> AttachedAttributes(BlockReference br, List<Matrix3d> transforms,
        string owner, string parentPath, string parentLayer, string file, bool placedRows)
    {
        var result = new List<Dictionary<string, object?>>();
        try
        {
            foreach (ObjectId attributeId in br.AttributeCollection)
            {
                if (placedRows) CountAnnotation("placed_attached_attribute_ids_seen");
                try
                {
                    if (tr.GetObject(attributeId, OpenMode.ForRead) is not AttributeReference attribute)
                        throw new InvalidOperationException("AttributeCollection member is not AttributeReference");
                    string layer = attribute.Layer == "0" ? parentLayer : attribute.Layer;
                    // AttributeReference.Position already includes this INSERT's
                    // BlockTransform. Apply only its ancestor transforms.
                    var record = Geometry(attribute, transforms, owner, parentPath, layer, parentLayer, file, placedRows);
                    record["parent_block_handle"] = br.Handle.ToString();
                    record["parent_instance_path"] = parentPath;
                    record["attribute_coordinates_resolved"] = placedRows || IsModel(owner);
                    result.Add(record);
                    if (placedRows) CountAnnotation("placed_attached_attribute_values_read");
                }
                catch (System.Exception ex)
                {
                    if (placedRows) CountAnnotation("placed_attached_attribute_values_failed");
                    AnnotationWarning("attached_attribute_read_failed", parentPath, attributeId.Handle.ToString(), ex);
                }
            }
        }
        catch (System.Exception ex)
        {
            if (placedRows) CountAnnotation("placed_attribute_collection_reads_failed");
            AnnotationWarning("attribute_collection_unavailable", parentPath, br.Handle.ToString(), ex);
        }
        return result;
    }

    void ReadMLeader(MLeader leader, List<Matrix3d> transforms, Dictionary<string, object?> d, bool placed, string path)
    {
        Point3d W(Point3d point) => World(point, transforms);
        string handle = leader.Handle.ToString();
        d["type"] = "MLEADER"; d["annotation_role"] = "leader_linked_instance_annotation";
        if (placed) CountAnnotation("placed_mleader_instances_seen");
        try
        {
            d["content_type"] = leader.ContentType.ToString();
            if (leader.ContentType == ContentType.MTextContent)
            {
                using var text = leader.MText;
                if (text == null) throw new InvalidOperationException("MLeader has no embedded MText value");
                d["text"] = text.Text; d["text_contents"] = text.Contents;
                d["position"] = P(text.Location); d["position_world"] = P(W(text.Location));
                d["annotation_content_read_status"] = "embedded_mtext_read";
                if (placed) CountAnnotation("placed_mleader_mtext_read");
            }
            else if (leader.ContentType == ContentType.BlockContent)
            {
                var content = (BlockTableRecord)tr.GetObject(leader.BlockContentId, OpenMode.ForRead);
                d["block_content_handle"] = content.Handle.ToString(); d["block_content_name"] = content.Name;
                d["position"] = P(leader.BlockPosition); d["position_world"] = P(W(leader.BlockPosition));
                d["annotation_content_read_status"] = "block_identity_only_attribute_values_not_extracted";
                if (placed) CountAnnotation("placed_mleader_block_content_identity_only");
                AnnotationWarning("mleader_block_attribute_values_not_extracted", path, handle);
            }
        }
        catch (System.Exception ex)
        {
            d["annotation_content_read_status"] = "unavailable";
            if (placed) CountAnnotation("placed_mleader_content_failed");
            AnnotationWarning("mleader_content_unavailable", path, handle, ex);
        }
        var lines = new List<object>();
        try
        {
            foreach (int leaderIndex in leader.GetLeaderIndexes())
            foreach (int lineIndex in leader.GetLeaderLineIndexes(leaderIndex))
            {
                try
                {
                    var points = Enumerable.Range(0, leader.VerticesCount(lineIndex)).Select(index => leader.GetVertex(lineIndex, index)).ToList();
                    lines.Add(new { leader_index = leaderIndex, leader_line_index = lineIndex,
                        annotation_owner_handle = handle, content_owner_handle = handle,
                        vertices = points.Select(P).ToList(), vertices_world = points.Select(W).Select(P).ToList(),
                        start_world = points.Count == 0 ? null : P(W(points[0])),
                        end_world = points.Count == 0 ? null : P(W(points[^1])) });
                    if (placed) CountAnnotation("placed_mleader_lines_read");
                }
                catch (System.Exception ex)
                {
                    if (placed) CountAnnotation("placed_mleader_lines_failed");
                    AnnotationWarning("mleader_line_unavailable_" + lineIndex, path, handle, ex);
                }
            }
        }
        catch (System.Exception ex) { AnnotationWarning("mleader_line_indexes_unavailable", path, handle, ex); }
        d["leader_lines"] = lines;
        d["leader_text_association"] = "Leader lines and embedded content belong to the same MLeader handle; no nearest-text guess.";
    }

    int CurveSteps(double radius, double sweep, List<Matrix3d> transforms)
    {
        double stretch = 1;
        // Frobenius norm is an upper bound for any affine in-plane stretch.
        foreach(var matrix in transforms)
        {
            var x = Vector3d.XAxis.TransformBy(matrix); var y = Vector3d.YAxis.TransformBy(matrix); var z = Vector3d.ZAxis.TransformBy(matrix);
            stretch *= Math.Sqrt(x.LengthSqrd + y.LengthSqrd + z.LengthSqrd);
        }
        double error = chordError / Math.Max(stretch, 1e-12);
        double angle = radius <= error ? Math.PI/8 : 2*Math.Acos(Math.Max(-1, 1-error/radius));
        int count = Math.Max(2, (int)Math.Ceiling(sweep/Math.Max(angle, 1e-8)));
        if (count > MaxCurvePoints) throw new InvalidOperationException("curve_tessellation_limit_exceeded");
        return count;
    }
}
