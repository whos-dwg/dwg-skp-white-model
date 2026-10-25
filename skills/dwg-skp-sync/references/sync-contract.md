# 同步資料契約

## 共用核心與入口

唯一權威原始碼為 `src/dwg_semantic`。`tools/package_skill.py` 發布到 `dwg-layered-skp/scripts/runtime/dwg_semantic`；`dwg-skp-sync` 僅攜帶入口、相依清單與同步文件。新 `dwg_semantic.sync` CLI 呼叫 `live_workflow`，舊 `pipeline.live_repair_*` 是同一函式的相容匯入。幾何規劃、live CAD、牆系、SketchUp client 與世界座標讀回橋接均共用，不另寫幾何求解器。

`scripts/core-dependency.json` 採 `dwg-skp-sync-core-dependency-v1`，指定同層技能 `dwg-layered-skp`、`scripts/runtime-manifest.json` 原始檔SHA256、全部runtime與橋接器的逐檔SHA256。安裝與薄入口都先驗證，缺失／漂移時停止。共用 `.deps` 或 Python 環境提供 Shapely；`DWG_SKP_DEPENDENCIES` 可提供第三方依賴，不能替換已驗證的幾何核心。不依賴此專案的絕對路徑或歷史work。

開發發布：以 `tools/package_skill.py` 重新打包共用核心及同步相依清單；先用 `tools/install_skill.py --target <skills>/dwg-layered-skp --dry-run` 檢查，再正式安裝核心；接著以 `--skill dwg-skp-sync --target <skills>/dwg-skp-sync` 同樣先dry-run再安裝。安裝器備份既有技能、檢查確切目標與逐檔雜湊。維護發生在開發發布，日常同步不執行它。更新技能不會更新正在運作的SketchUp外掛；`status` 若版本不符需另外安排橋接維護，不在未儲存模型上自動重啟。

## 已發布輸入／輸出

| 資料 | 權威與檢查 |
|---|---|
| 既有 `job/document.json` | immutable base；`intake.source_path/source_sha256/units/scale_to_mm/dependencies` 與模型根一致；每次plan/apply重新核對來源及參照檔案實際SHA256 |
| CAD Pickfirst JSON | 真實 `success/status/document/selection_source/selection_count/returned_count/truncated/context/objects/observed_at_utc`；完整物件handle、索引、圖層、原生geometry與WCS bbox；30秒新鮮度；不過濾原始快照 |
| `cad-wall-system-scope-v1` | 本次真實確認的user_scope_quote、wall_layer、preserve_openings、每個handle的roles及opening配對；詳見同步流程 |
| `dwg-skp-live-snapshot-v1` | 橋接器從目前模型實讀GUID/path、root payload／座標、容器、目標與非目標實體、受保護樓板及mesh簽章；一般模式只輸出局部完整網格 |
| `dwg-skp-live-repair-plan-v1` | 既有同一schema；model/source/targets/create/write_policy、canonical_body_json與plan_sha256全部綁定；不寫模型，不代表已套用 |
| `source.live_cad_revision` | 新CAD輪廓的獨立覆寫，含原生快照、幾何簽章、來源與被替換來源關係；不把新handle插入舊正規化document |
| `source.sync_selection` | 新同步入口的選取觀察證據；geometry_signature及完整selection_snapshot都納入plan雜湊；所有CAD驅動動作在套用前重讀完整選取，live revision另加原生幾何驗證 |
| `dwg-skp-local-query-v1` | 由同一CAD幾何核心產生世界XY範圍、完整來源識別碼、顯式目標與選取策略；橋接器以實際來源匹配、完整牆系、舊目標外框擴大查詢，不採最近距離猜測 |
| `local_read`／`model.local_read` | `dwg-skp-local-read-v1`：canonical query、伺服端擴大XY範圍、全域輕量inventory_sha256、實際納入entity_ids及protected_entity_ids；所有欄位納入plan canonical hash；套用前重新查詢與逐欄比較 |
| apply receipt | 共用橋接器回傳實際新增persistent IDs、Undo、驗證與未儲存狀態；CLI要求新的receipt路徑；提交後持久化失敗須報回結果，不能當未套用再重試 |

plan/apply不可把已錄製快照視為當前新鮮選取。核對雜湊只證明資料一致，真正的讀取仍由受控CAD／SketchUp工具完成。`remove_wrong` 使用當前SketchUp明選葉群組及簽章，不要求CAD選取。

## 局部讀取與漂移檢查

有 CAD 選取時，`plan` 預設呼叫窄介面 `inspect_white_model_local`；`health` 除既有協定2.0與固定規範版本外，必須宣告 `partial_snapshot_version: 1.0` 及該operation。缺能力時停止，不能自動回退全模型。`--full-model` 保留為局部計畫的舊版診斷；實際空選取的整模同步使用下節獨立完整證據。

每次查詢都重掃目前根與群組的輕量清單：persistent ID、父層、名稱、Tag、材質、鎖定／隱藏、世界變換、材料／受保護葉件的實際世界外框、definition GUID、子物件數與provenance hash。全域清單的簽章處理結構／版本漂移；不將definition GUID或modified布林值冒充完整面網格簽章。Group.bounds已含自身變換，僅再套父層世界變換；逆縮放葉件與旋轉容器也按實際世界範圍查詢。

範圍種子聯集包含CAD新輪廓、所有來源識別碼匹配（包括遠端同來源件，防止重複補建）、明選目標及完整來源牆系。橋接器先擴大到舊目標完整外框，再納入實際XY外框相交的修正構件與受保護樓板，跨全部Z高度以保留板底／開口依賴。大型樓板只作依賴，不以整片樓板外框擴大種子。遠端群組及參考線不作完整面網格讀取；所有真正相關構件都仍使用同一套世界座標mesh／材積驗證。

套用前重新從當前模型建立同一局部查詢、全域清單簽章、範圍與成員，以及實際局部mesh context。移入／新增／刪除、來源／Tag／變換或局部同外框幾何變動都會阻擋；新構件的所有mesh頂點與目標須在已證明範圍內，Python與Ruby雙邊檢查，避免小查詢漏掉鄰件。來源檔／參照SHA256及兩次新鮮CAD選取仍各自核對。通過後才開始同一筆Undo交易，回讀、失敗abort與不儲存政策維持原樣。

全域清單是輕量資訊，並非每個遠端面的逐點比對；完整幾何漂移檢查針對所有局部依賴。外部程式未提交交易、且遠端幾何在同一不相交外框內變動時，不能藉GUID宣稱已逐面證明遠端幾何不變；遠端幾何保持不相交就不影響本次寫入。一般來源葉件仍在編輯中時局部模式直接阻擋，請使用者退出；分類容器可保持編輯層級。依據：[definition GUID的更新時機](https://ruby.sketchup.com/Sketchup/ComponentDefinition.html#guid-instance_method)、[observer交易通知時機](https://ruby.sketchup.com/Sketchup/ModelObserver.html)、[instance bounds座標](https://ruby.sketchup.com/Sketchup/Drawingelement.html#bounds-instance_method)。本流程不自動關閉編輯狀態、不重啟目前模型。

`doctor.contract.snapshot_mode: local_geometry`、`inventory_mode: stateless_actual_bounds` 表示此發布能力。每份計畫／收據的 `read_stats` 揭露實際掃描群組、完整網格構件及參考件數，供檢查個案是否仍覆蓋很大範圍。來源或同牆系本身覆蓋整層時，安全閉包仍可能需要讀取很多構件；不以固定縮小範圍換取加速。

## 整模差異讀取

空選取路由與完整操作見 [整模同步](whole-model-sync.md)。`dwg-cad-model-snapshot-v1` 要求完整目前記憶體資料庫、同源path/fingerprint、完整native-extract計數、complete=true、truncated=false、Editor.SelectImplied數量0與30秒新鮮度。未完成讀取、未解參照、截斷或無法解析都阻擋，不能當作來源已刪除。

`model.global_read` 為 `dwg-skp-global-read-v1`，綁定完整模型（含參考邊與components）的實際幾何、變換與屬性清單SHA256。health須有whole_model_sync_version=1.0與inspect_white_model_global。計畫action=synchronize_model，差異為targets/create與必要create_containers；`source.sync_model`綁定完整CAD幾何簽章、圖面及讀取時間。套用前第二次完整CAD必須較新且內容相同，Ruby另驗證新鮮觀察摘要；SKP也完整重讀比對，之後才啟動單筆Undo。

無差異計畫status=up_to_date、targets/create均空，通過新鮮度與漂移檢查後回傳unchanged，不開交易、不新增Undo。未變構件不作metadata刷新；變更構件保存model_sync_source_state作下一次來源基線。原intake與來源檔案雜湊不被改寫。來源檔、新門窗缺淨空及樓板語意邊界仍見整模流程。

整模發布另需 `slab_sync_version: 1.0`。樓板在讀取時保留於 protected_entities，但只有 synchronize_model 的完整計畫可將來源綁定的板列為目標。`model_sync_slab_state` 保存可重建的原始來源設定與本次來源基線；孔洞／降板實際材料仍由模型面網格驗證。頂板更新後的實際候選板底供牆柱收高，兩者共用一筆交易。局部修補的分類範圍維持不含樓板。

## 持久索引的後續掛鉤

目前不建立 `components.sqlite` 或持久網格快取，因此 `component_index: false` 仍屬實。初次建模未來可另輸出 `manifest.json`（來源與模型版本）、`components.sqlite`（來源對應、空間索引、幾何快取）、`relations.json`（牆系／開口／樓板依賴）及 `revisions.jsonl`（追加修訂）；這些不是本次局部讀取的前提。將來若採快取，仍須由來源／參照雜湊、模型GUID／root payload及實際修改狀態失效，並保留目前提交前的真實範圍查詢與局部mesh驗證。
