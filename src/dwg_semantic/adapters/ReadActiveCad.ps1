param([Parameter(Mandatory=$true)][string]$SourcePath,[Parameter(Mandatory=$true)][string]$AssemblyPath,[Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BoundCadReader {
 [DllImport("ole32.dll", CharSet=CharSet.Unicode, PreserveSig=false)] private static extern void CLSIDFromProgID(string name,out Guid id);
 [DllImport("oleaut32.dll", PreserveSig=false)] private static extern void GetActiveObject(ref Guid id,IntPtr reserved,[MarshalAs(UnmanagedType.IUnknown)]out object app);
 public static object Connect(){Guid id;object app;CLSIDFromProgID("AutoCAD.Application",out id);GetActiveObject(ref id,IntPtr.Zero,out app);return app;}
}
'@
$cadApplication=[BoundCadReader]::Connect()
$cadDocument=$cadApplication.ActiveDocument
if(-not [string]::Equals([IO.Path]::GetFullPath($cadDocument.FullName),[IO.Path]::GetFullPath($SourcePath),[StringComparison]::OrdinalIgnoreCase)){throw 'Active drawing differs from the bound source'}
if($cadDocument.PickfirstSelectionSet.Count -ne 0){throw 'Whole-model read requires empty Pickfirst'}
function LispString([string]$value){'"'+$value.Replace('\','/').Replace('"','\"')+'"'}
$cadReadCommand='(progn (command "_.NETLOAD" '+(LispString $AssemblyPath)+') (command "DWGLAYEREDSNAPSHOT" '+(LispString $RequestPath)+'))'
$cadDocument.SendCommand($cadReadCommand+[char]10)
