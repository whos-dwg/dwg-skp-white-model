# Whole-model delta sync

只有實際空選取才進入此模式。使用者呼叫 `dwg-skp-sync` 時，先取得完整 Pickfirst 快照；`EMPTY_SELECTION`、count/returned_count為0、objects為空、truncated為false、同一來源路徑及30秒新鮮度均須成立。缺快照、工具失敗、截斷不代表沒有選取。有選取仍走既有局部模式。

## 規劃與套用

```powershell
python scripts/dwg_skp_sync.py plan D:/Models/job-001 `
  --selection-json D:/Models/op-001/actual-empty-selection.json `
  --output D:/Models/op-001/plan.json
```

預設 `--mode auto`。完整讀取目前 AutoCAD 的記憶體資料庫，包含未儲存修改、定義與放置實例，不使用舊正規化文件冒充現況；不全選、不儲存、不重載參照。隨包 .NET 唯讀器使用已安裝 AutoCAD 2026／.NET 8 SDK離線編譯，來源與組件雜湊固定快取，不改 SECURELOAD、TrustedPaths 或執行原則。缺環境就停止。

SketchUp 使用 `inspect_white_model_global`，health須有 `whole_model_sync_version: 1.0` 及 `slab_sync_version: 1.0`。完整讀取實體與樓板mesh；對模型內全部群組、components、面、邊、參考線、變換與屬性另作完整簽章，供提交前偵測整模漂移。計畫的 `action: synchronize_model`、`snapshot_mode: whole_model`、`difference_summary` 分別列出替換／刪除目標、新建件及未變件數。詳細來源家族與未變件ID保存在計畫內。

有差異只刪除 `targets`、建立 `create`；未變件不為更新屬性而重寫。需要的新支援分類容器也在同一筆交易內建立。`up_to_date` 代表無差異，仍可做第二次讀取與驗證，回傳 `unchanged`，不開交易、不新增 Undo。

套用前再次完整讀取目前 CAD，另存新檔，必須晚於規劃讀取且在30秒內：

```powershell
python scripts/dwg_skp_sync.py cad-read D:/Models/job-001 `
  --output D:/Models/op-001/cad-before-apply.json
python scripts/dwg_skp_sync.py apply D:/Models/op-001/plan.json `
  --confirm-sha256 <plan_sha256> --receipt D:/Models/op-001/receipt.json `
  --current-cad-model-json D:/Models/op-001/cad-before-apply.json
```

第二次完整CAD內容、圖面fingerprint、路徑及空選取須相同；不能重用規劃快照。SKP重新完整讀取，模型身分、根、來源、容器、所有幾何與屬性須與計畫一致。來源及參照檔案SHA256仍在Python/Ruby兩側核對。任一漂移就重新規劃，不套用舊計畫。所有驗證通過才開始一筆Undo，幾何／體積／metadata回讀成功後commit，失敗abort。此流程不儲存、不另建SKP、不寫回CAD。

## 構件與語意邊界

- 依唯一來源實例ID／唯一handle追溯已有牆、柱、帷幕、配件；不用最近距離。完整CAD缺少已綁定的來源才可刪除其模型件；空／截斷讀取不能作刪除依據。
- 原生封閉材料輪廓／封閉線網才可補建。新內圈須證明同一來源實例、同圖層、同Z並唯一位於牆外圈內，作空腔處理。無唯一分類或高度證據、非共面、碰撞、來源歧義會停止整筆計畫。
- 固定規範按同步完成後的實際板底求高；頂板變更與受影響牆柱、全高帷幕同一筆提交。既有可追溯門洞、過梁、窗台垂直淨空保持。已綁定封閉開口來源可更新平面尺寸並沿用已證明淨空。新門窗或模糊側界／符號須以局部開口／牆系流程確認，不猜新淨高。
- 整片樓板支援規則見下節。樓梯、設備、未綁定註記與CAD參考線不改寫。其他受保護構件來源變更會停止；參考來源另計 `reference_only_source_count`，不宣稱已更新參考圖面。
- `document.json` 與原來源版本保持不可變。未儲存CAD改動記在新構件的 `model_sync_source_state`，供下一次正確比對。DWG／參照已儲存成不同SHA256仍須先核對來源版本，不能以整模掃描豁免新鮮度。

整模模式需要完整掃描，因此保留有選取時的局部加速。它是既有來源白模的差異同步，不是整棟重建，也不是雙向CAD編輯。

## 整片樓板、孔洞與降板

已有來源綁定的 `floor_slab`／`overhead_slab` 按原建模的整片 recipe 更新外框、孔洞及降板區界。來源幾何 Z 的差值保持既有模型標高偏移；已明確綁定的 AFF 註記沿用既有數值解析。不能以2D線的零Z猜頂板高度，不能用外接框、舊網格或最近樓層推導新決策。

變更樓板必須能重建為 `whole_uniform_thickness` 單一閉合殼，保留原來源厚度（通常200 mm）、孔洞穿透、降板與階差側壁；沿用同一樓板引擎的材料差集及名義等厚檢查。對比使用實際回讀三角網格各Z區間的材料，而非僅比材積或屬性。候選網格、完整邊界面與材料 cells 必須一致；原生面朝向、材積、座標及來源屬性回讀通過才提交。

新增板可使用既有 `decisions.json` 的明確來源綁定系統；新畫的平板只在同來源圖層有唯一平板模板、角色及來源平面到模型標高關係時可繼承。新增孔洞、降板或多個可能樓層不能自動猜，需先確認系統決策。未保留完整 recipe 的舊樓板可保持未變；若來源變了則停止。鎖定、巢狀手工幾何、部分來源遺失、不連通單殼或材料碰撞均停止整筆同步。

同步仍不修改既有 `document.json`／核定決策；變更板將已證明的設定與當次來源保存在 `model_sync_slab_state`。新增、刪除及連帶牆柱都在同一次 Undo；無差異的板保持其原生實體與屬性。局部模式仍把樓板當板底依賴，不能藉局部 action 寫入樓板。
