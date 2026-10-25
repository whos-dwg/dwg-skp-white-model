"""Create NEW native DWG/DXF fixtures. Requires local AutoCAD 2025+ and .NET 8.

Run with PYTHONPATH=src: python tests/native_fixture/generate.py NEW_OUTPUT_DIR
The separate test-only command refuses to overwrite either drawing.
"""
from pathlib import Path
import os
import shutil
import sys
from xml.sax.saxutils import escape

from dwg_semantic.intake import _find_autocad, _run


def main():
    folder = Path(sys.argv[1]).resolve()
    if folder.exists():
        raise SystemExit("Fixture output directory must be new")
    folder.mkdir(parents=True)
    core = _find_autocad()
    if core is None:
        raise SystemExit("AutoCAD Core Console unavailable")
    build = folder / "test-only-writer"
    build.mkdir()
    shutil.copyfile(Path(__file__).with_name("Fixture.cs"), build / "Fixture.cs")
    refs = "".join(f'<Reference Include="{name}"><HintPath>{escape(str(core.parent/(name.lower()+".dll")))}</HintPath><Private>false</Private></Reference>'
                   for name in ("AcCoreMgd", "AcDbMgd", "AcMgd"))
    (build / "Fixture.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0-windows</TargetFramework>'
        '<ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable><GenerateAssemblyInfo>false</GenerateAssemblyInfo>'
        '<NuGetAudit>false</NuGetAudit></PropertyGroup><ItemGroup>'+refs+'</ItemGroup></Project>',encoding="utf-8")
    (build / "NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>',encoding="utf-8")
    env = dict(os.environ)
    env.update({"DWG_FIXTURE_OUTPUT": str(folder), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "DOTNET_CLI_HOME": str(build / "dotnet-home")})
    result = _run([shutil.which("dotnet"),"build",str(build / "Fixture.csproj"),"--configuration","Release","--nologo","--disable-build-servers"],
                 directory=build,env=env,timeout=120,log=folder/"fixture-build.log")
    if result.returncode:
        raise SystemExit("Fixture compilation failed; inspect fixture-build.log")
    dll = (build / "bin/Release/net8.0-windows/Fixture.dll").as_posix()
    script = build / "create.scr"
    script.write_text(f'SECURELOAD\n0\n_.NETLOAD\n"{dll}"\nCREATEINTAKEFIXTURE\n_.QUIT\n_Y\n',encoding="utf-8")
    scratch = build / "context.dwg"
    shutil.copyfile(core.parent / "Support/chroma.dwg", scratch)
    result = _run([str(core),"/i",str(scratch),"/readonly","/s",str(script),"/l","en-US","/isolate","dwglayered_fixture_"+folder.name,str(build/"profile")],
                 directory=build,env=env,timeout=120,log=folder/"fixture-create.log")
    if result.returncode or not (folder/"vendor-plan.dwg").is_file() or not (folder/"vendor-plan.dxf").is_file():
        raise SystemExit("Fixture generation failed; inspect fixture-create.log")
    print(folder/"vendor-plan.dwg")
    print(folder/"vendor-plan.dxf")


if __name__ == "__main__":
    main()
