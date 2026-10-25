"""Client and installer for the purpose-built SketchUp live-repair bridge."""
from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any

from .live_repair import validate_plan_digest


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 51236
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class SketchUpLiveError(RuntimeError):
    """The active SketchUp repair bridge rejected or could not serve a request."""


def _call(method: str, params: dict | None = None, *, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT, timeout: float = 15.0) -> dict:
    request_id = str(uuid.uuid4())
    request = {"request_id": request_id, "method": method, "params": params or {}}
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    chunks: list[bytes] = []
    size = 0
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as client:
            client.settimeout(timeout)
            client.sendall(encoded)
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise SketchUpLiveError("SketchUp 即時修正回應超過安全大小限制。")
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as error:
        raise SketchUpLiveError(
            f"無法連線到目前 SketchUp 即時修正橋接器 {host}:{port}；請確認已安裝並重新啟動 SketchUp。"
        ) from error
    try:
        response = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
        raise SketchUpLiveError("SketchUp 即時修正橋接器回傳無效資料。") from error
    if response.get("request_id") != request_id:
        raise SketchUpLiveError("SketchUp 即時修正回應識別不符。")
    if response.get("ok") is not True:
        detail = response.get("error") or {}
        raise SketchUpLiveError(f"{detail.get('code', 'LIVE_REPAIR_FAILED')}: {detail.get('message', '未知錯誤')}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise SketchUpLiveError("SketchUp 即時修正橋接器未回傳物件資料。")
    return data


def live_status(**connection: Any) -> dict:
    return _call("health", {}, **connection)


def _require_current_bridge(**connection: Any) -> dict:
    """Fail before mutation when an older bridge cannot enforce fixed policy.

    An old inspector omitted root policy attributes; that must never make a
    new fixed-standard native model look like a genuinely historical model.
    """
    from .modeling_standard import VERSION
    health=live_status(**connection)
    if (health.get('protocol')!='dwg-live-repair' or health.get('protocol_version')!='2.0'
            or health.get('fixed_modeling_standard_version')!=VERSION):
        raise SketchUpLiveError('即時修正橋接器版本過舊或不支援固定規範；請執行 live-install 後重新啟動 SketchUp。')
    return health


def _require_partial_capability(health: dict) -> None:
    if not isinstance(health, dict) or health.get('partial_snapshot_version') != '1.0' or 'inspect_white_model_local' not in health.get('supported_operations', []):
        raise SketchUpLiveError('目前橋接器不支援局部讀取；請安排橋接器更新，不能靜默改用全模型讀取。')


def _require_whole_capability(health: dict) -> None:
    if health.get('whole_model_sync_version')!='1.0' or 'inspect_white_model_global' not in health.get('supported_operations',[]):
        raise SketchUpLiveError('橋接器尚不支援整模差異同步；需更新後才能使用，不能將舊完整讀取當成整模證據。')


def inspect_active_white_model(*, local_query: dict | None = None, whole_model: bool = False, **connection: Any) -> dict:
    health = _require_current_bridge(**connection)
    if whole_model:
        if local_query is not None:raise SketchUpLiveError('局部查詢與整模讀取不可同時指定。')
        _require_whole_capability(health)
        return _call('inspect_white_model_global', {}, **connection)
    if local_query is None:
        return _call("inspect_white_model", {}, **connection)
    _require_partial_capability(health)
    snapshot = _call('inspect_white_model_local', {'query': local_query}, **connection)
    from .local_snapshot import validate_local_snapshot
    validate_local_snapshot(snapshot, local_query)
    return snapshot


def apply_live_repair_plan(plan: dict, *, confirmation_sha256: str,
                           current_selection: dict | None = None, current_cad_model: dict | None = None, **connection: Any) -> dict:
    actual = validate_plan_digest(plan)
    if confirmation_sha256 != actual:
        raise SketchUpLiveError("確認雜湊與即時修正計畫不一致。")
    from .live_cad_geometry import validate_current_selection
    validate_current_selection(plan,current_selection)
    from .cad_model import validate_current_model,model_signature
    validate_current_model(plan,current_cad_model)
    from .slab_sync import validate_slab_rows
    validate_slab_rows(plan)
    health = _require_current_bridge(**connection)
    if plan.get('model', {}).get('local_read') is not None:
        _require_partial_capability(health)
    params = {
        "plan": plan,
        "confirmation_sha256": confirmation_sha256,
    }
    if plan.get('model',{}).get('global_read') is not None:
        _require_whole_capability(health)
        if plan.get('slab_sync_version')and health.get('slab_sync_version')!='1.0':
            raise SketchUpLiveError('目前橋接器缺少樓板整模同步能力，請更新後再執行。')
        params['current_cad_model']={'schema':'dwg-cad-model-observation-v1','geometry_signature':model_signature(current_cad_model),
            'document':current_cad_model['document'],'observed_at_utc':current_cad_model['observed_at_utc'],'selection_count':current_cad_model['selection_count']}
    return _call('apply_live_repair',params,**connection)


def _plugin_roots() -> list[Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    base = Path(appdata) / "SketchUp"
    if not base.is_dir():
        return []
    return sorted(
        [folder / "SketchUp" / "Plugins" for folder in base.glob("SketchUp *")
         if (folder / "SketchUp").is_dir()],
        key=lambda path: path.parts[-3],
    )


def install_live_bridge(source: Path | str, *, sketchup_version: str | None = None) -> dict:
    """Install the narrow bridge into existing per-user SketchUp plugin folders."""
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"找不到即時修正橋接器來源：{source}")
    marker = "DWG_LIVE_REPAIR_BRIDGE"
    content = source.read_text(encoding="utf-8")
    if marker not in content:
        raise SketchUpLiveError("即時修正橋接器來源缺少身分標記。")
    roots = _plugin_roots()
    if sketchup_version:
        wanted = str(sketchup_version).replace("SketchUp ", "").strip()
        roots = [path for path in roots if path.parts[-3] == f"SketchUp {wanted}"]
    if not roots:
        raise SketchUpLiveError("找不到符合條件的 SketchUp 使用者 Plugins 資料夾。")
    installed = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "dwg_live_repair_bridge.rb"
        if destination.exists() and marker not in destination.read_text(encoding="utf-8", errors="ignore"):
            raise SketchUpLiveError(f"拒絕覆寫不屬於本工具的檔案：{destination}")
        shutil.copy2(source, destination)
        installed.append(str(destination))
    return {
        "status": "installed",
        "files": installed,
        "restart_required": True,
        "port": DEFAULT_PORT,
        "note": "關閉並重新開啟 SketchUp 後生效；不會修改任何 SKP。",
    }
