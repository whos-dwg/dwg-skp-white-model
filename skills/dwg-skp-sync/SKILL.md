---
name: dwg-skp-sync
description: Sync CAD changes to the open source-bound SketchUp white model. With a CAD selection, update that local scope; with an actual empty selection, compare the complete CAD and SKP and update only changed walls, columns, curtains, source-bound openings and whole slabs, including bound holes and drop zones. Use for dwg-skp-sync,「同步我目前選取的牆」or whole-model delta sync. Initial modeling and explicit batch rebuilds use dwg-layered-skp.
---

# dwg-skp-sync

使用 `dwg-skp-sync` 或 `$dwg-skp-sync` 同步到目前開啟的來源綁定白模。有 CAD 選取時維持局部同步；實際沒有選取時比對整模，只改有差異的構件。這是 skill 呼叫名稱，不代表新增 Codex 原生斜線命令。

## 日常執行

1. 以本技能的 `scripts/dwg_skp_sync.py` 執行已發布動作；`status` 檢查橋接能力。局部需要 `partial_snapshot_version: 1.0`，整模需要 `whole_model_sync_version: 1.0` 及 `slab_sync_version: 1.0`。缺能力就停止，不擅自改模式。`doctor` 僅檢查本機；日常同步不改引擎、不重裝、不跑開發測試。
2. 從目前 SKP 根屬性的來源路徑／雜湊、使用者已指定的工作資料夾辨識既有 `document.json`；核對同一主 DWG 與參照，不修改原始來源、核定或交付。缺工作資料時列出缺項，不重建整棟、不偽造正規化文件。
3. 除明示 `remove_wrong` 外，用 AutoCAD 唯讀 `cad_inspect_selection(include_vertices=true)` 保存真實完整 Pickfirst 快照，須在30秒內。不篩選、不把缺少快照當作沒有選取。
4. 有選取時讀 [局部同步流程](references/live-repair.md)，沿用既有四種動作與局部網格讀取。直牆改長／重畫用 `--live-cad-geometry --auto-match-position`；複合牆用已確認的 `--wall-system-scope-json`。套用前再次真實讀取完整選取，使用 `--current-selection-json`。
5. 真實 `EMPTY_SELECTION`、數量0且完整時讀 [整模同步流程](references/whole-model-sync.md)。`plan` 預設自動路由，完整讀取目前 CAD 記憶體與 SKP；檢查差異數量及 `snapshot_mode: whole_model`。套用前用 `cad-read` 再完整重讀，使用 `--current-cad-model-json`。`up_to_date` 的驗證收據為 `unchanged`，不產生 Undo。
6. 計畫寫入新的操作資料夾；核對實際來源、目標、輪廓、孔洞與高度。既有同步授權涵蓋該模式及範圍時可繼續；只有缺尺寸、歧義或超出授權範圍才詢問。檔案／模型／選取漂移就停止重讀，不重放舊計畫，不改時間冒充新讀取。
7. 回報實際收據、變更件數、Undo 名稱與 `model_saved: false`、`new_skp_created: false`。`receipt_persistence_error` 表示模型可能已提交，保留結果並停止，不能重試套用。未完成必要驗證不能宣稱成功。

## 必須保留的邊界

- 只處理 `dwg-layered-skp` 生成並保有 `DWG_LAYERED_SKP` 來源屬性的白模。來源、根座標、唯一宿主／目標、模型 GUID/path、實際幾何簽章及固定規範 marker 都由共用核心核對。
- 未儲存的 CAD 新輪廓另記 live revision；原始 `document.json`、來源雜湊及核定資料保持不變。只接受唯一的來源關係或符合世界座標／方向／厚度／重疊／定位條件的目標，不用最近距離選牆、不平移或吸附新 CAD 幾何。
- 牆系完整選取要逐物件分派已支援角色；保留內腔、門洞與過梁，設備管線保持參考。門洞淨高來自已確認構件與實際過梁，不採2D門線Z。固定規範的牆柱與全高帷幕仍按實際板底收高，缺證據時停止。
- 整模以唯一來源實例對應既有構件，處理新增／修改／刪除，保留未變件。既有可追溯開口保留淨空；新門窗或已變的模糊側界需局部確認。整片樓板可按已綁定外框、孔洞及降板決策更新；頂板改高時，相關牆柱按更新後實際板底一起收高，合併為一次 Undo。樓板局部選取仍不支援；缺孔洞角色、作用板或標高綁定時停止，不猜樓層與高度。樓梯、設備、未綁定註記與參考線保持原樣，不反向改寫 CAD。
- 微小幾何可有局部放大與葉群組逆縮放；實際體積及底面三角網格以世界座標回讀。只建立一筆可復原交易，驗證後提交，失敗整筆 abort；不自動儲存、不換模、不重啟、不重建整棟、不碰未授權構件。

## 入口、相依與資料

`dwg_skp_sync.py` 是薄入口，提供 `doctor`、`status`、`cad-read`、`plan`、`apply` 與維護用 `install-bridge`。它驗證同層 `dwg-layered-skp` 的發布雜湊，共用其 runtime、Shapely 與 Ruby 橋接器，不載入對方整套建模流程。`--mode auto` 是日常路由；`--mode model` 仍須真實空選取。`--full-model` 是舊版局部計畫的完整讀取診斷，並非整模同步開關。整模即時 CAD 讀取目前使用 Windows AutoCAD 2026 與已安裝 .NET 8 SDK，離線編譯隨包唯讀器；不改安全設定。

[同步資料契約](references/sync-contract.md) 說明兩種讀取、漂移檢查與安裝相依。局部模式仍用每次重建的世界外框與輕量清單，不讀取遠端完整網格；整模模式確實掃描完整幾何。均不依賴持久網格快取。來源葉件內仍在編輯時停止，分類容器可保持開啟；不自動切換編輯狀態。

使用範例：`$dwg-skp-sync 同步我目前在 AutoCAD 選取的牆到目前開啟的 SketchUp 白模，保留既有門洞，其他構件不動。`
