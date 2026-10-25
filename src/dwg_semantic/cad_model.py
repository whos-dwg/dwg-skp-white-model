"""Fresh complete in-memory CAD evidence, distinct from a Pickfirst selection."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,os,shutil,subprocess,time,uuid
from pathlib import Path
from xml.sax.saxutils import escape

from .workflow_io import WorkflowError,_source_freshness
from .selection_openings import _path_key
from .live_repair import _digest

SCHEMA='dwg-cad-model-snapshot-v1'
SCOPE='complete_database_including_definitions_and_placed_instances'

def require_empty_selection(selection,document):
    from .live_cad_geometry import require_fresh_selection
    require_fresh_selection(selection or {})
    if (not isinstance(selection,dict) or selection.get('success') is not True or
        selection.get('selection_source')!='PickfirstSelectionSet' or selection.get('status') not in ('EMPTY_SELECTION','EMPTY','NO_SELECTION') or
        selection.get('selection_count')!=0 or selection.get('returned_count')!=0 or
        selection.get('objects')!=[] or selection.get('truncated') is not False or
        _path_key(selection.get('document',{}).get('path'))!=_path_key(document['intake']['source_path'])):
        raise WorkflowError('整模同步需要同一來源圖、實際完整且為空的 Pickfirst 選取；缺少快照不等於沒有選取。')

def model_signature(snapshot):
    return _digest({k:snapshot.get(k)for k in ('schema','document','scope','selection_source','selection_count',
        'truncated','complete','entity_count','resolved_instance_count','native_extract','reader')})

def validate_model_snapshot(snapshot,document):
    from .live_cad_geometry import require_fresh_selection
    require_fresh_selection(snapshot or {})
    intake=document['intake'];native=snapshot.get('native_extract')or{}
    if (snapshot.get('schema')!=SCHEMA or snapshot.get('success') is not True or snapshot.get('complete') is not True or
        snapshot.get('truncated') is not False or snapshot.get('scope')!=SCOPE or
        snapshot.get('selection_source')!='Editor.SelectImplied' or snapshot.get('selection_count')!=0 or
        not snapshot.get('document',{}).get('fingerprint_guid') or
        _path_key(snapshot.get('document',{}).get('path'))!=_path_key(intake['source_path']) or
        _path_key(native.get('database',{}).get('filename'))!=_path_key(intake['source_path']) or
        native.get('schema')!='dwg-native-extract-v3' or native.get('blockers') or
        not isinstance(native.get('entities'),list) or not isinstance(native.get('resolved_instances'),list) or
        snapshot.get('entity_count')!=len(native['entities']) or
        snapshot.get('resolved_instance_count')!=len(native['resolved_instances'])):
        raise WorkflowError('CAD 整模讀取不完整、圖面不符、選取已變或缺少完整性證據；不能用來判斷新增與刪除。')
    if _source_freshness(document):raise WorkflowError('整模同步來源或參照版本已失效；請先核對來源版本。')
    return model_signature(snapshot)

def normalized_model(snapshot,document):
    validate_model_snapshot(snapshot,document)
    from .intake import _scale_geometry,_canonical_points,_provenance
    raw=deepcopy(snapshot['native_extract']);factor=document['intake']['scale_to_mm']
    raw=_canonical_points(_scale_geometry(raw,factor));raw['units']='mm'
    raw['intake']=deepcopy(document['intake'])
    _provenance(raw,Path(document['intake']['source_path']),document['intake']['source_sha256'],document['intake'].get('dependencies',[]))
    return raw

def validate_current_model(plan,current):
    observation=plan.get('source',{}).get('sync_model')
    if observation is None:return
    if current is None:raise WorkflowError('套用整模同步前必須重新完整讀取目前 CAD。')
    signature=validate_model_snapshot(current,{'intake':plan['source']['manifest']})
    try:
        old=datetime.fromisoformat(observation['observed_at_utc'].replace('Z','+00:00'))
        now=datetime.fromisoformat(current['observed_at_utc'].replace('Z','+00:00'))
        if now<=old:raise ValueError('not a later read')
    except (KeyError,ValueError,TypeError):raise WorkflowError('整模套用必須使用規劃後重新讀取的 CAD，不能重用快照。')
    if signature!=observation['geometry_signature']:raise WorkflowError('CAD 整模內容、圖面身分或選取已改變；請重新比對。')

def build_reader(directory):
    """Compile the packaged read-only adapter using the installed offline SDK."""
    from .intake import _find_autocad,_run
    directory=Path(directory).resolve()
    core=_find_autocad();dotnet=shutil.which('dotnet')
    if core is None or not dotnet:raise WorkflowError('整模讀取需要已安裝 AutoCAD 與 .NET SDK。')
    source=Path(__file__).with_name('adapters')/'AutoCadExtract.cs'
    identity=hashlib.sha256(source.read_bytes()+str(core).encode()).hexdigest()[:20]
    name='WholeCadReader_'+identity
    build=Path(directory)/('cad-reader-'+identity);assembly=build/('bin/Release/net8.0-windows/'+name+'.dll')
    cache=build/'compiled-reader.json'
    if assembly.is_file() and cache.is_file():
        record=json.loads(cache.read_text(encoding='utf-8'))
        if record.get('source_sha256')==hashlib.sha256(source.read_bytes()).hexdigest() and record.get('assembly_sha256')==hashlib.sha256(assembly.read_bytes()).hexdigest():return assembly
        raise WorkflowError('CAD 唯讀器快取已變更；不載入不符的組件。')
    build.mkdir(parents=True,exist_ok=True);shutil.copy2(source,build/source.name)
    refs=''.join(f'<Reference Include="{n}"><HintPath>{escape(str(core.parent/(n.lower()+".dll")))}</HintPath><Private>false</Private></Reference>'for n in ('AcCoreMgd','AcDbMgd','AcMgd'))
    (build/'WholeCadReader.csproj').write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0-windows</TargetFramework><AssemblyName>'+name+'</AssemblyName><Nullable>enable</Nullable><GenerateAssemblyInfo>false</GenerateAssemblyInfo><NuGetAudit>false</NuGetAudit></PropertyGroup><ItemGroup>'+refs+'</ItemGroup></Project>',encoding='utf-8')
    (build/'NuGet.Config').write_text('<configuration><packageSources><clear /></packageSources></configuration>',encoding='utf-8')
    env={**os.environ,'DOTNET_CLI_TELEMETRY_OPTOUT':'1','DOTNET_SKIP_FIRST_TIME_EXPERIENCE':'1','DOTNET_CLI_HOME':str(build/'dotnet-home'),'MSBUILDDISABLENODEREUSE':'1'}
    result=_run([dotnet,'build',str(build/'WholeCadReader.csproj'),'--configuration','Release','--nologo','--disable-build-servers'],directory=build,env=env,timeout=120,log=build/'build.log')
    if result.returncode or not assembly.is_file():raise WorkflowError('CAD 整模唯讀器編譯失敗：'+str(build/'build.log'))
    cache.write_text(json.dumps({'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'assembly_sha256':hashlib.sha256(assembly.read_bytes()).hexdigest()}),encoding='utf-8')
    return assembly

def capture_model(document,output,*,reader_directory=None,timeout=45):
    """No source command text is evaluated. Only fixed native read commands."""
    if os.name!='nt':raise WorkflowError('目前 CAD 整模即時讀取只支援 Windows AutoCAD。')
    output=Path(output).resolve()
    if output.exists():raise WorkflowError('拒絕覆寫 CAD 整模快照。')
    issues=_source_freshness(document)
    if issues:raise WorkflowError('CAD 來源已失效：'+'; '.join(issues))
    output.parent.mkdir(parents=True,exist_ok=True)
    assembly=build_reader(Path(reader_directory or output.parent).resolve())
    identity=uuid.uuid4().hex;request=output.parent/('cad-read-'+identity+'.json')
    request.write_text(json.dumps({'schema':'dwg-cad-model-read-request-v1','request_id':identity,'output':str(output),
        'source_path':document['intake']['source_path'],'scale_to_mm':document['intake']['scale_to_mm']}),encoding='utf-8')
    helper=Path(__file__).with_name('adapters')/'ReadActiveCad.ps1'
    shell=shutil.which('pwsh') or shutil.which('powershell.exe')
    if not shell:raise WorkflowError('找不到已安裝的 PowerShell；不修改執行原則。')
    result=subprocess.run([shell,'-NoProfile','-File',str(helper),'-SourcePath',document['intake']['source_path'],
        '-AssemblyPath',str(assembly),'-RequestPath',str(request)],capture_output=True,timeout=15,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise WorkflowError('無法要求 AutoCAD 唯讀整模讀取：'+result.stderr.decode('utf-8',errors='replace'))
    until=time.monotonic()+timeout
    while not output.exists() and time.monotonic()<until:time.sleep(.1)
    if not output.exists():raise WorkflowError('AutoCAD 整模讀取未完成；保留診斷，不以空資料繼續。')
    # FileShare.Read allows a reader before serialization finishes; retry only
    # parsing the same read result, never replay the native operation.
    while True:
        try:value=json.loads(output.read_text(encoding='utf-8-sig'));break
        except (json.JSONDecodeError,PermissionError):
            if time.monotonic()>=until:raise WorkflowError('CAD 整模快照未完整寫入。')
            time.sleep(.1)
    if value.get('request_id')!=identity:raise WorkflowError('CAD 整模回應身分不符。')
    validate_model_snapshot(value,document)
    return value
