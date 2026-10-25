// Test-only DWG writer. Never loads or modifies a user drawing.
using System;
using System.IO;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(DwgAnnotationFixture.Commands))]
namespace DwgAnnotationFixture;

public sealed class Commands
{
    [CommandMethod("CREATEANNOTATIONFIXTURE")]
    public void Create()
    {
        string folder = Environment.GetEnvironmentVariable("DWG_ANNOTATION_FIXTURE_OUTPUT")
            ?? throw new InvalidOperationException("DWG_ANNOTATION_FIXTURE_OUTPUT required");
        Directory.CreateDirectory(folder);
        string target = Path.Combine(folder, "annotation-plan.dwg");
        if (File.Exists(target)) throw new IOException("Test fixture already exists; choose a new folder");
        using var database = new Database(true, true);
        database.Insunits = UnitsValue.Millimeters;
        var previous = HostApplicationServices.WorkingDatabase;
        HostApplicationServices.WorkingDatabase = database;
        try
        {
            using (var tr = database.TransactionManager.StartTransaction())
            {
                var layers = (LayerTable)tr.GetObject(database.LayerTableId, OpenMode.ForWrite);
                foreach (string name in new[] { "ANNO-SLAB", "ANNO-EXPLICIT", "A-WALL" })
                {
                    var layer = new LayerTableRecord { Name = name };
                    layers.Add(layer); tr.AddNewlyCreatedDBObject(layer, true);
                }
                var blocks = (BlockTable)tr.GetObject(database.BlockTableId, OpenMode.ForWrite);
                var model = (BlockTableRecord)tr.GetObject(blocks[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
                void Add(BlockTableRecord owner, Entity entity)
                { owner.AppendEntity(entity); tr.AddNewlyCreatedDBObject(entity, true); }
                var inner = new BlockTableRecord { Name = "SLAB-TAG" };
                blocks.Add(inner); tr.AddNewlyCreatedDBObject(inner, true);
                var thickness = new AttributeDefinition
                { Tag = "SLAB_THK", TextString = "DEFAULT-999", Prompt = "Thickness", Position = new Point3d(10, 20, 0), Height = 2.5, Layer = "0" };
                var level = new AttributeDefinition
                { Tag = "SLAB_LEVEL", TextString = "DEFAULT-LEVEL", Prompt = "Level", Position = new Point3d(10, 30, 0), Height = 2.5, Layer = "ANNO-EXPLICIT" };
                Add(inner, thickness); Add(inner, level);
                BlockReference Insert(BlockTableRecord owner, Point3d position, Scale3d scale, double rotation, string thk, string elevation)
                {
                    var reference = new BlockReference(position, inner.ObjectId)
                    { Layer = owner == model ? "ANNO-SLAB" : "0", ScaleFactors = scale, Rotation = rotation };
                    Add(owner, reference);
                    foreach (var pair in new[] { (Definition: thickness, Value: thk), (Definition: level, Value: elevation) })
                    {
                        var attribute = new AttributeReference();
                        attribute.SetAttributeFromBlock(pair.Definition, reference.BlockTransform);
                        attribute.TextString = pair.Value;
                        attribute.Layer = pair.Definition.Layer;
                        reference.AttributeCollection.AppendAttribute(attribute);
                        tr.AddNewlyCreatedDBObject(attribute, true);
                    }
                    return reference;
                }
                Insert(model, new Point3d(1000, 2000, 0), new Scale3d(2, 3, 1), Math.PI / 2, "THK=180", "FL+204.450");
                Insert(model, new Point3d(7000, 1000, 0), new Scale3d(-1, 2, 1), 0, "THK=220", "FL+203.950");
                var outer = new BlockTableRecord { Name = "ANNO-ASSEMBLY" };
                blocks.Add(outer); tr.AddNewlyCreatedDBObject(outer, true);
                Insert(outer, new Point3d(200, 300, 0), new Scale3d(1.5, .5, 1), Math.PI / 2, "THK=200", "FL+204.300");
                void AddLeader(BlockTableRecord owner, Point3d head, Point3d tail, Point3d textPosition, string text)
                {
                    var leader = new MLeader(); leader.SetDatabaseDefaults(database);
                    leader.Layer = owner == model ? "ANNO-SLAB" : "0";
                    leader.ContentType = ContentType.MTextContent;
                    using var content = new MText { Contents = text, Location = textPosition, TextHeight = 100 };
                    leader.MText = content;
                    int index = leader.AddLeaderLine(leader.AddLeader());
                    leader.AddFirstVertex(index, head); leader.AddLastVertex(index, tail);
                    Add(owner, leader);
                }
                AddLeader(outer, new Point3d(10, 50, 0), new Point3d(550, 300, 0), new Point3d(600, 300, 0), "SLAB THK=250; LEVEL+204.600");
                Add(model, new BlockReference(new Point3d(10000, 6000, 0), outer.ObjectId)
                { Layer = "ANNO-SLAB", Rotation = Math.PI / 2, ScaleFactors = new Scale3d(2) });
                Add(model, new BlockReference(new Point3d(20000, 4000, 0), outer.ObjectId)
                { Layer = "ANNO-SLAB" });
                AddLeader(model, new Point3d(1000, 1000, 0), new Point3d(2700, 4000, 0), new Point3d(3000, 4000, 0), "SLAB THK=180; LEVEL+204.450");
                var outline = new Polyline { Closed = true, Layer = "A-WALL" };
                foreach (var point in new[] { new Point2d(0, 0), new Point2d(8000, 0), new Point2d(8000, 4000), new Point2d(0, 4000) })
                    outline.AddVertexAt(outline.NumberOfVertices, point, 0, 0, 0);
                Add(model, outline);
                tr.Commit();
            }
            database.SaveAs(target, DwgVersion.Current);
        }
        finally { HostApplicationServices.WorkingDatabase = previous; }
        Application.DocumentManager.MdiActiveDocument.Editor.WriteMessage("\nANNOTATION FIXTURE OK: " + target + "\n");
    }
}
