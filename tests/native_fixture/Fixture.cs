// Test-only fixture writer. Production AutoCadExtract.cs has no write commands.
using System;
using System.IO;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;

[assembly: CommandClass(typeof(DwgIntakeFixture.Commands))]
namespace DwgIntakeFixture;

public class Commands
{
    static Polyline Rectangle(double x, double y, double width, double depth, string layer)
    {
        var poly = new Polyline { Layer = layer, Closed = true };
        var points = new[] { new Point2d(x,y), new Point2d(x+width,y), new Point2d(x+width,y+depth), new Point2d(x,y+depth) };
        for (int i=0;i<points.Length;i++) poly.AddVertexAt(i, points[i], 0, 0, 0);
        return poly;
    }

    [CommandMethod("CREATEINTAKEFIXTURE")]
    public void Generate()
    {
        string folder = Environment.GetEnvironmentVariable("DWG_FIXTURE_OUTPUT") ?? throw new InvalidOperationException("DWG_FIXTURE_OUTPUT required");
        Directory.CreateDirectory(folder);
        string dwg = Path.Combine(folder, "vendor-plan.dwg"), dxf = Path.Combine(folder, "vendor-plan.dxf");
        // Refuse replacement even for test assets.
        if (File.Exists(dwg) || File.Exists(dxf)) throw new IOException("Fixture target already exists; choose a fresh output folder");
        using var db = new Database(true, true);
        db.Insunits = UnitsValue.Millimeters;
        var previous = HostApplicationServices.WorkingDatabase;
        HostApplicationServices.WorkingDatabase = db;
        try {
        using (var tr = db.TransactionManager.StartTransaction())
        {
            var layers = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForWrite);
            foreach(string name in new[] {"VENDOR_WALL", "CONS_COL"})
            { var layer = new LayerTableRecord { Name = name }; layers.Add(layer); tr.AddNewlyCreatedDBObject(layer, true); }
            var blocks = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForWrite);
            var model = (BlockTableRecord)tr.GetObject(blocks[BlockTableRecord.ModelSpace], OpenMode.ForWrite);
            void Add(BlockTableRecord owner, Entity entity) { owner.AppendEntity(entity); tr.AddNewlyCreatedDBObject(entity, true); }
            Add(model, Rectangle(0,0,200,4000,"VENDOR_WALL"));
            Add(model, Rectangle(0,4000,6000,200,"VENDOR_WALL"));
            Add(model, Rectangle(2500,2500,500,500,"CONS_COL"));
            var inner = new BlockTableRecord { Name = "Vendor_Component" }; blocks.Add(inner); tr.AddNewlyCreatedDBObject(inner, true);
            Add(inner, Rectangle(0,0,2000,150,"0"));
            var outer = new BlockTableRecord { Name = "Vendor_Assembly" }; blocks.Add(outer); tr.AddNewlyCreatedDBObject(outer, true);
            Add(outer, new BlockReference(Point3d.Origin, inner.ObjectId) { Layer = "0", Rotation = Math.PI/2 });
            Add(model, new BlockReference(new Point3d(6000,1000,0), outer.ObjectId) { Layer = "VENDOR_WALL" });
            // Same definition in a second, mirrored/scaled instance exercises identity and affine geometry.
            Add(model, new BlockReference(new Point3d(3500,0,0), inner.ObjectId) { Layer = "VENDOR_WALL", ScaleFactors = new Scale3d(-1,2,1) });
            tr.Commit();
        }
        db.SaveAs(dwg, DwgVersion.Current);
        db.DxfOut(dxf, 16, DwgVersion.Current);
        } finally { HostApplicationServices.WorkingDatabase = previous; }
        Application.DocumentManager.MdiActiveDocument.Editor.WriteMessage("\nFIXTURE OK: " + folder + "\n");
    }
}
