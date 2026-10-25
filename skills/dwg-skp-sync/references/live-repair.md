# AutoCAD 選取驅動的 SketchUp 即時修正

本頁是 dwg-skp-sync 有 CAD 選取時的局部流程。實際空選取改讀 [整模差異同步](whole-model-sync.md)。技能只修改由 dwg-layered-skp 建立、仍保有 `DWG_LAYERED_SKP` 來源 ID／provenance 的牆、柱、帷幕與帷幕配件葉群組。

## 可修正的情況

| 動作 | 用途 | CAD／SketchUp 證據 |
|---|---|---|
| `cut_opening` | 漏開門洞、窗洞、一般開口 | CAD 實際選取開口；唯一交會連續宿主牆 |
| `add_missing` | CAD 有、SKP 漏建的封閉平面構件 | CAD 實際封閉輪廓；明確類別、基底、高度 |
| `replace_from_cad` | 抓到錯輪廓、牆身長度／範圍判斷錯誤 | CAD 正確封閉輪廓；來源 ID 自動命中或 SketchUp 明選錯誤葉群組 |
| `remove_wrong` | SKP 多建或不該存在的構件 | SketchUp 明選具來源屬性的葉群組 |

這四項涵蓋目前白模「平面輪廓 × 垂直擠出」的局部幾何錯誤。斜面、自由曲面、任意手工巢狀元件、材質／家具編輯與無來源屬性的第三方模型不在本模式內；不要把「所有錯誤」解讀為可任意執行 Ruby 或修改任何 SketchUp 物件。

## 安全不變量

- 來源必須是原工作的同一主 DWG；Pickfirst 數量與回傳數一致且不得截斷。隔離有助觀看，但沒有實際選取不構成修正證據。
- 新增／替換只接受正規化來源中的確切封閉輪廓或可閉合線網；外接框只能用於開口與既有宿主材料的交集，不能拿來猜新增牆體。
- 使用者明確要求同步 CAD 已改長／重畫的牆時，可在 replace_from_cad 啟用 live_cad_geometry。此窄模式一次只接受一個完整、封閉、正Z法向、無寬度／厚度的原生2D多段線；頂點直線周長、面積、OCS高程及WCS範圍必須互相吻合，含弧或不足資料仍阻擋。保留原模型來源檔雜湊不變，live Pickfirst 作獨立修訂覆寫，完整原生快照、幾何雜湊及被替換的舊來源均進入新構件屬性／計畫；不改寫舊document.json或假稱新handle存在於舊檔。來源DWG若已儲存成不同檔案雜湊，仍先停止作來源版本核對，不能用局部選取豁免全檔漂移。
- auto_match_position 可免手選 SKP 目標，但只自動定位直線矩形牆：同一來源／未移動根座標、平行、等厚、同中心線、足夠的軸向與材料重疊，且固定端或中心一致。只接受唯一候選，不用最近者打破歧義；原圖精度造成的微小對齊差只用於辨識，不移動新CAD界線。顯式target ID仍優先，自動定位模式不採偶然殘留的SKP選取。含門窗分段或多個目標的live修訂需另確認完整範圍，不能填回洞口。幾何限定修訂沿用目標主要Tag、分類及基底，其餘新CAD來源層另存屬性。
- 新版固定規範以根payload的 `modeling_standard` 與 `fixed_standard_audit` 綁定，兩者須完整一致；只剩其中一項或值不符時阻擋，不能當成歷史模型繞過。完全沒有這兩項marker的歷史模型保留既有局部即時行為，不因局部修正自動採新版規範或重改整棟。
- 採固定規範的模型，補建／替換牆柱及全高帷幕一律依實際mesh重新求各區最低上方板底，保留原基底、門洞淨高並按高差分段；傳入或繼承的height-mm不是短牆豁免。缺可信板底覆蓋、落在板孔內卻無可信高度依據，或現有模式無法表達時阻擋，保留目前模型。防火門／捲簾本體不可透過補建或換類別繞過，2D洞口修正仍可進行。即時模式不編輯整片樓板外框、孔界或樓梯範圍，也不重問樓梯是否含平台。
- 目前開啟的 SKP 必須已儲存、只有一個來源綁定的 `DWG model` 根群組，且根群組保持來源座標、未移動／旋轉／縮放。
- 計畫綁定 SketchUp model GUID／path、根 payload、每個目標 persistent ID 與實際面幾何簽章。計畫後若切換檔案、編輯目標或移動根群組，套用必須停止並重讀。
- Python與Ruby雙邊核對來源檔／參照新鮮度、完整根與容器狀態、目標和非目標的實際幾何；受保護的整片樓板可讀取實際mesh檢查碰撞，但不在此模式編輯。共同選取LINE可形成封閉材料網，內圈留空；非共面或混合Z、不明分類、重疊新件及懸空未採用來源都阻擋。Layer0依本模型既有Tag策略及可逆對照處理。
- 回讀核對實際封閉體積、全高擠出、面／邊Untagged、Tag、材質、名稱、來源屬性及父層座標，並阻擋鎖定／手工巢狀／斜頂等不支援目標。套用前先確認receipt可新建；已存在的receipt不得覆寫。若提交後receipt因競爭寫入失敗，回傳已套用的結果及持久化錯誤，不能當成未套用再執行一次。
- 寫入為單一 `start_operation`／Undo。先解析、檢查、建立雜湊計畫；套用時重新檢查，再刪除目標與建立完整替代群組，回讀 manifold、材積與 metadata 後才 commit。任何一步失敗即 abort。
- 不呼叫 `save`、不另存、不建立新 SKP。套用後目前檔案處於已修改狀態，新構件被選取；由使用者檢查後決定儲存或 Undo。

## 安裝與啟動

即時寫入需要隨 skill 提供的窄介面橋接器。安裝只複製 `dwg_live_repair_bridge.rb` 到既有 SketchUp 使用者 Plugins 資料夾，不修改 SKP：

```powershell
python scripts/dwg_skp_sync.py install-bridge --sketchup-version 2021
```

安裝後完整關閉再開啟 SketchUp。橋接器只監聽 `127.0.0.1:51236`，只接受 `health`、`inspect_white_model`、`inspect_white_model_local`、`inspect_white_model_global`、`apply_live_repair`，不提供任意 Ruby／script 執行。確認狀態：

新版橋接協定保留2.0，health須回報相同fixed_modeling_standard_version及局部讀取能力partial_snapshot_version=1.0；規劃與套用前都先檢查能力。舊1.0橋接器缺少固定規範讀回／檢查時會停止，須更新並重啟，不能把「舊橋接器沒讀到marker」誤當歷史模型。此技能更新不會自行安裝外掛、重啟SketchUp或修改目前模型。

```powershell
python scripts/dwg_skp_sync.py status
```

## 執行

1. AutoCAD 開啟原工作同一主 DWG，實際選取正確來源物件並保持選取；用唯讀 `cad_inspect_selection(include_vertices=true)` 保存完整快照。
2. SketchUp 開啟要直接修正的既有 SKP。`replace_from_cad` 若無法由來源 ID 唯一命中，進入群組層級並選取錯誤的白模葉群組；`remove_wrong` 必須這樣明選。
3. 先建立不寫入的局部計畫。`plan` 預設使用局部網格讀取，`read_stats` 回報清單與完整網格件數。局部範圍在橋接器按真實外框擴大到完整來源目標／牆系，再納入鄰件與所有相關樓板；套用前再次從目前模型計算，變動即停止。只有明確的舊版診斷才用 `--full-model`。以下數值只示範命令形狀，不是尺寸預設：

```powershell
python scripts/dwg_skp_sync.py plan D:/Models/job-001 `
  --selection-json D:/Models/cad-plan-current.json `
  --action cut_opening `
  --height-mm 2100 `
  --base-z-mm 0 `
  --opening-type door `
  --output D:/Models/live-repair-plan.json
```

補漏建使用 `--action add_missing --category architectural_wall`；固定規範模型可不填height-mm，由當地實際板底求高。歷史模型才需 `--height-mm <來源候選高度>`。輪廓／牆體範圍錯誤用 `--action replace_from_cad`，未指定基底、類別、Tag時沿用唯一SketchUp目標；固定規範仍重算最終板底高度，不能以傳參或繼承短牆代替。唯一來源綁定的真窗台洞下段保留其實際窗底，避免填窗；不能自行宣稱窗台豁免。缺板底時只列缺項，不再問是否收到板底。需要刪除時用 `--action remove_wrong`，不需要selection JSON。

4. 核對輸出的 action、目前模型、targets、create、輪廓、基底與高度。除 remove_wrong 外，套用前再次以真實 CAD 選取工具取得完整快照，另存 cad-apply-current.json；每次讀取須在30秒內，不得複製舊JSON或更新時間冒充重讀。沿用使用者已明確授權的範圍；只對尚未確認且會改變範圍或結果的缺項再詢問。將同一份計畫的雜湊傳回才可套用：

```powershell
python scripts/dwg_skp_sync.py apply D:/Models/live-repair-plan.json `
  --confirm-sha256 <plan_sha256> `
  --receipt D:/Models/live-repair-receipt.json `
  --current-selection-json D:/Models/cad-apply-current.json
```

完成時回傳 `model_saved: false`、`new_skp_created: false`、Undo 名稱與新增 persistent IDs。直接檢查 SketchUp 目前選取；正確則儲存目前檔案，不正確可立即 Undo。JSON plan／receipt 是稽核資料，不是另一份模型。

### 同步 CAD 改後的直牆輪廓

使用者已要求同步這段修改時，先以 cad_inspect_selection 取得目前真實選取，再使用：

```powershell
python scripts/dwg_skp_sync.py plan D:/Models/job-001 `
  --selection-json D:/Models/cad-current.json --action replace_from_cad `
  --live-cad-geometry --auto-match-position --output D:/Models/wall-plan.json
```

CAD選取讀取時間須在30秒內。規劃不修改任何模型；先核對唯一命中的原牆、長度差與其餘不變條件。套用前必須再次呼叫真實 CAD 選取讀取工具，另存新快照，不可只複製原JSON或更新時間。以 --current-selection-json 將第二次讀取傳給 apply；客戶端會重新核對同一圖、完整輪廓／圖層／選取雜湊及新鮮度，再交由橋接器核對SKP簽章並提交單筆Undo。CAD已變、未重讀、讀取過期或SKP已漂移皆停止。

原生寫入器保留逆縮放葉件以避免微小來源邊被SketchUp合併時，即時讀回必須用目前世界座標的實際底面三角網與封閉mesh材積，不能把group.volume的局部放大材積當實際值；面環中零面積回折接縫也不靠buffer修形。這項讀回修正需更新Ruby橋接器，不改模型。當前模型有未儲存修改時，不得以重啟丟棄修改；經使用者授權的維護可在原視窗受限地重載本橋接器，或由使用者安排儲存後重啟。

### 已確認範圍的整組牆系

使用者明確確認「只同步這組牆、保留內腔與現有門洞、其他設備管線不動」後，可在上述 live CAD 命令加入 `--wall-system-scope-json <本次範圍JSON>`。原始完整選取仍全部保存與重讀，不把排除設備後的子集偽裝成實際 Pickfirst。

範圍JSON採 `schema: cad-wall-system-scope-v1`、`wall_layer`、實際 `user_scope_quote`、`preserve_openings: true`、涵蓋每個選取 handle 的 `roles`，及 `openings` 配對。角色為 `outer_boundary`、`inner_boundary`、`door_jamb`、`duplicate_boundary_reference`、`opening_edge`、`opening_symbol`、`unchanged_reference`；各洞以兩個 `jamb_handles`、兩個 `edge_handles` 及一個 `symbol_handle` 配對。角色判讀由實際原生幾何證明，使用者確認範圍不等於逐 handle 的人工核定。牆圖層不能改列未處理參考；重複界必須由實際內圈完整涵蓋。門洞2D證據的高程保留，不能充作淨高。

此窄模式以同Tag、同父分類及可追溯的既有來源家族定位；新舊完整外框／內圈只允許既有數值精度內的對應，不用最近候選解歧義。材料取外框扣內圈，門洞匹配現有實際過梁以保留淨高，再依實際板底收高。相鄰材料移除後露出的本牆範圍可恢復；因此舊門側分件可能重新連成一件，來源綁定仍完整保留。複合牆跨層、來源族不唯一、洞口證據缺失或碰撞時停止；不將這個模式泛化為任意移牆或新增未確認門窗。
