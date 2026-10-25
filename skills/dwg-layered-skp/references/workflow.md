# 解析、核定與 SKP 的執行流程

以下命令相對於本 skill 目錄，也可從任何工作目錄呼叫 scripts/dwg_to_skp.py 的絕對路徑。每份來圖建立新的 --job 資料夾，不將既有交付作為暫存。

## 工作預設與覆寫順序

使用者已採納以下白模工作預設：可定位到板域內有界區域的折線／叉號樓板符號先按開口處理，特例再改；樓板名義厚度預設200 mm，特殊構造可覆寫；柱牆依平面示意判讀；既有可追溯的輪廓修補流程沿用。這些是使用者的建模偏好，200 mm不是通用法規或來源圖面已明示的結構尺寸。

另有四項固定規範，不按案件切換：樓板整片等厚且原外框、孔位及孔界不動；所有牆柱與全高帷幕收到當地上方板底；樓梯整間含平台挖空；防火門／防火捲簾不建3D本體但保留2D洞口／門檻及分區證據。新inspect、一般revise與slab-attach保存modeling_standard版本，review強制檢查，不以prisms或whole_union降級交付。缺板底或精確樓梯區界時只列該資料缺項，不重新詢問已固定的規範。

上述四項固定規範先行；以下優先順序只解決尚未固定的符號用途、數值及作用對象：本次明確指示及來源綁定的既有物件／專案決策 → 來源的降板、無孔、分層孔界等證據 → 工作預設。既有explicit overrides只從本案獨立工作資料恢復，不能藉舊決策切換整片模式、移動外框／孔界、縮小樓梯孔、保留短牆或重建防火門／捲簾本體。不用新的符號預設推翻已確認降板，也不因重新載入而重問仍適用的已核定值；記錄各決策來自來源、人類覆寫或工作預設。

「折線／叉號」須先辨明為樓板符號並綁定實際輪廓及作用樓板；普通LWPOLYLINE、牆線、家具線與門扇不是符號。輪廓清楚時依預設提出／執行開口方案並在摘要提醒，不逐個要求另給留洞文字。若符號沒有可定位的界線、作用層仍不明，或存在互相衝突的明確證據，列出具體缺項與局部定位，才確認該項。幾何、模式與能力界線見[樓板流程](rc-slabs.md)。

## 環境與依賴

```powershell
python scripts/dwg_to_skp.py doctor
```

- Python 3.11+、Shapely 2.x 及其 NumPy 依賴。可安裝專案套件，或在本 skill scripts/.deps 放置相符 Python 版本的套件；亦可用 DWG_SKP_DEPENDENCIES 指定套件目錄。
- DWG／DXF：本機 AutoCAD Core Console 與相符 .NET SDK。抽取器來源包含於 runtime 的 adapters。引擎或 SDK 不可用時保留具體診斷，JSON 接續流程仍可使用。
- SKP：本機 SketchUp C API。可用 SKETCHUP_API_DLL 指定 SketchUpAPI.dll。寫出及讀回均在同一主執行緒執行。

抽取器只讀來源 DWG，使用獨立工作資料夾；不儲存、不清理、不覆寫來源。外部參照納入來源清單，缺檔不可忽略。

## 1. 接收與解析

```powershell
python scripts/dwg_to_skp.py inspect D:/Incoming/architect.dwg --job D:/Models/job-001
```

來源為 unitless 或正規化 JSON 沒有可信單位時，先確認，再用 --unit mm、cm、m 或 inch 指定。此參數代表來源幾何單位，不是輸出顯示格式。程式正規化為 mm 並記錄倍率。

inspect 即使回傳 blocked 仍保留判讀資料，先讀具體 blocker。產物：

| 檔案 | 用途 |
|---|---|
| document.json | 幾何、來源、原始／有效圖層與抽取診斷 |
| classification.json | 分類、證據及未分類清單 |
| layers.json | 全部考慮物件的圖層、類型、來源及候選用途 |
| decisions.json | 可編輯的本次圖層決策及單件覆寫 |
| model.plan.json | 候選幾何，尚未核定 |
| review.md / review.svg | 使用者核定摘要及彩色平面 |
| review.json | 預覽狀態、阻擋項目及版本雜湊 |

### 已接入的自動平面辨識

新 `inspect` 會由同一份正規化來源執行底層辨識；其後 `review` 會按當版來源與辨識決策重算。另輸出 `recognition.json`、`recognition.md`、`recognition-plan.svg`、可切換構件的 `recognition.html`，有繪圖依賴時加上整合與核心區PNG。這些檔案納入review雜湊；牆柱、四側帷幕、樓板孔與門上段共用同一組候選ID，不分開拼成互相遺漏的圖。

- 來源索引包含每個放置葉件的原始／有效層與完整巢狀路徑。父子用途衝突列 `layer_conflict_requests`；有衝突的來源不能默用父層忽略或直接成模。
- 牆線按圖層及實際Z平面分網，外圈減內圈；封閉內腔與再內層材料島依拓樸交替保留。舊式 `POLYLINE` 直接抽取完整2D／3D頂點；非共面來源另列，不能湊成XY閉環。原內圈遇到其他來源材料侵入時，分存原界、實際淨空與交界差帳。
- 帷幕同時讀取共享長線、短框料與原生HATCH邊界。沒有閉環或存在未解周界仍列待核；狹長空腔不因最小外接矩形近似框料就自動填實。HATCH的分離外圈與內洞分別保留。微小來源對齊補接預設停用；本案已授權時才指定上限，記錄每段補線與來源。
- 門以完整圖塊組件辨識全部旋轉葉片，亦可由兩側框件辨識滑門／折門；門型無法證明時保留門窗候選，不虛報特定門型。門洞與過梁分存定位、完整跨度、兩側宿主面及實測厚度。核定的拓樸及完整組件會交給原成模管線，不能又回到逐閉環填滿或只取首片門扇的舊路徑。
- 樓板辨識包括井道整個圍塑外界、閉框標記及有四側來源支撐的開放折線符號；重複／互含符號整併並保留來源。樓梯採牆內邊／門檻圍成的整間來源區域，包含平台；定位框不是孔界。無唯一實際封閉界或作用樓板未證明時仍待核，不問是否包含平台。

已有、來源綁定的系統選擇及修補授權可用 `inspect ... --recognition-decisions <json>` 載入；檔案須含相同 `source_sha256`。`settings.collinear_gap_repair_mm`、`collinear_gap_minimum_source_length_mm` 只授權有限共線補接；`system_decisions` 以完整實例路徑保存本案 `action` 與 `reason`，不得把某圖的座標、handle或數量寫入通用規則。review使用 `decisions.recognition` 中相同資料。這不替代分類／高度決策或approve。

已有來源綁定的歷史檢查輪廓時，可獨立比較本次辨識：

```powershell
python scripts/dwg_to_skp.py recognition-check D:/Models/job-001 --reference D:/Models/case-reference.json --output D:/Models/check-001.json
```

參考檔只交給檢查器，不輸入辨識演算法；檢查涵蓋留空、材料覆蓋、完整門跨度與來源完整性。來源不同則拒用，舊基準若證實誤判須保留原測試、證據與修正理由，不能改預期數量掩蓋漏件。通過幾何回歸不是任意新來圖均可自動確定用途，更不是SKP實檔QA。舊job若沒有 `automatic_plan`，另開新job重新inspect，不更動已核定工作。

候選分類仍需看圖核對。一般牆／結構牆／柱／帷幕與開口語意互斥，來源圖層可以相同。分開統計已讀取、已辨識及可建模，不以分類數量替代讀取完整性。

每個放置實例保留來源檔SHA、完整entity_id、instance_path、raw handle、原始／插入／有效圖層及世界XY；來源ID、候選ID、顯示順序與模型分件數分開。逐來源記錄已建模、被父件涵蓋、參考、排除或具體待解原因，不以有Tag或物件總數當完整性證明。巢狀葉件不能被父層reference無聲略過；同形多次放置也不能合成一個來源。保留曲線近似誤差、HATCH真邊界與unsupported類型／圖層盤點；缺抽取欄位先補抽取，不把它寫成原圖沒有資訊。

RC 樓板先執行 `slab-review <job> --output <新資料夾>` 主動判讀。新工作直接在 `decisions.slabs` 完成板域、標高、降板、留洞及厚度，與建築構件一併 review／approve／build；牆柱與全高帷幕所需的上方板底未齊，不得先核定一個任意牆高的無板方案。只有已有核定建築工作時，才用 `slab-attach <原核定job> --to <新job> --slab-decisions <json>` 建立獨立修訂，原模型不覆寫。樓梯整間含平台已固定，作用板沿用有明確來源的候選證據／exact-ID決策，或以 `slabs.stair_opening_bindings` 將完整候選ID綁到實際system IDs；已能證明的不重問，缺作用板時只列該缺項，不向所有相交上下板默扣。欄位例、門檻及成模契約見 [RC 樓板流程](rc-slabs.md)。

## 2. 修改決策並更新預覽

在工作資料夾 decisions.json 修改圖層設定，保留 inspect 產生的 source_sha256。以下為欄位範例，數值不能當作新圖面預設：

```json
{
  "default_height_mm": 3200,
  "opening_height_mm": 2100,
  "source_tag_policy": "effective",
  "layer_mapping": {
    "A-WALL": {
      "category": "architectural_wall",
      "height_mm": 3200,
      "base_z_mm": 0,
      "footprint_confirmed": true
    },
    "A-COLUMN": {"category": "column", "height_mm": 3200},
    "A-NOTE": {"category": "reference"},
    "A-FURN": {"category": "exclude", "reason": "使用者確認本次不建家具"}
  },
  "object_overrides": {}
}
```

類別：architectural_wall、structural_wall、column、curtain、curtain_accessory、opening、reference、exclude。未知用途保留 null，不以 reference 偷渡未確認的排除。

範例的 `default_height_mm`、`height_mm` 僅供來源候選／初始幾何，不是最終牆柱或全高帷幕的任意固定高度；新工作仍須有實際上方板底並按各區收高。開口淨高及構件原基底分開保留。

- footprint_confirmed: true 記錄已接受的材料輪廓。柱牆先按平面線、厚度、填色及相鄰關係判讀，納入同一預覽核對；不要求使用者逐個點選每根柱牆。不能把房間外框直接填成牆。thickness_mm 可驗證實際線距及配對，單線來源缺少可用厚度時仍須列出缺尺寸。
- 同層混用時，以 object_overrides 的完整 entity_id 指定單件類別、尺寸或 footprint: {outer: [[x,y],...], holes: [...]}。handle 只有本次來源唯一時才可作簡寫；重複圖塊必須用完整 ID。
- 巢狀金屬葉件另列 components.curtain_accessory_candidates／audits.nested_accessory_decision_requests，不能只因父圖塊圖層不符或整層列reference而略過。以object_overrides[完整source_entity_id]明選curtain_accessory，提供base_z_mm及footprint_confirmed（或實際footprint），並以semantic_subrole及來源用途區分全高帷幕配件與獨立欄杆／非全高配件。前者按實際板底求高，不再要求任意固定牆高；後者須有自身正值height_mm。reference／exclude也須明示reason。純線或曲線仍需可用輪廓，不能以確認文字代替幾何。prepare_model及prepared驗證均重算semantic_review_audit，核對決策與實際分件／參考／排除結果；全域高度不能代替用途判讀或獨立配件的高度依據。
- 開口需確認 footprint、高度及宿主，可用 host_ids 指定宿主 ID。缺少足跡或兩側宿主時，保留參考並確認處理方式。門扇掃掠範圍不能直接當成洞口足跡。
- 防火門／防火捲簾只排除3D本體，仍解析其2D門框、洞口及門檻；不能刪掉整層來源或將整個來源圖塊無條件排除，導致宿主牆未開洞或功能域界線遺失。
- 多來源圖層共同形成構件時，保留 source_layers 並在決策中指定 primary_source_layer，不任選第一層。
- 已核對的來源材料彼此重疊時，可在本案material_partition明列enabled=true、priority_categories及reason，依使用者確認的材料優先序扣除重疊；不可默設通用優先序。只分配同一來源、同一Z區間的材料，保存原／分配後輪廓、差帳與覆蓋來源；總材料聯集與孔不變，分斷後各段仍獨立群組。兩側門肢材質不同時，只有宿主幾何及高度均已證明，才以該開口的lintel_primary_source_layer指定實際宿主圖層作門上段主要Tag，其餘來源仍保留；此選擇也列入當版預覽，不能以Tag選擇補造宿主。
- source_tag_policy: effective 只對圖塊內 Layer 0 採有效圖層；其他原始圖層仍保留。original 使用字面原圖層，Layer 0 內容會共用其對應 Tag。
- 父圖塊與每個子 LINE／ARC／輪廓都要保存原始圖層、有效圖層及完整實例路徑。父層與子層若暗示不同構件類別，先列「父層推定／子件證據／局部影響」給使用者確認；Tag 的 Layer 0 繼承規則不是構件語意衝突的裁決規則。
- 傾斜或非平面來源預設阻擋。只有使用者明確接受平面投影時，才指定 project_to_xy: true 或核定新的 footprint，並在摘要保留來源 Z。floor_datum_m 可指定本層地坪絕對標高；base_z_mm 為該基準下的相對高度。
- final_source_accounting從全部已放置葉幾何反查實際最終材料／洞口，不能僅列source ID便算覆蓋。未成模來源若本次明確列參考或排除，可在source_dispositions[完整entity_id]提供status="reference"或"exclude"及reason；未知ID、無理由的排除或無實際材料涵蓋仍阻擋。此欄不取代父子層衝突裁決或提供構件高度。
- 一條已用作材料依據的來源線，若只有部分支撐實體、其餘為已展示並確認不成模的短線，可逐ID使用source_dispositions的status="reference"、scope="unmodeled_remainder"及實際reason。這不宣稱原線全長已覆蓋：稽核仍列完整原長、實際未覆蓋長及其參考決策。不得只因未覆蓋便自動套用，也不能用此欄合理化漏建未知材料、改孔或未證明的宿主；無現存材料來源關聯時此scope無效。

```powershell
python scripts/dwg_to_skp.py review D:/Models/job-001
```

review.json 的 blocked 需先解決。ready_for_confirmation 表示可以請使用者核定，尚未取得核定。圖與文字均綁定本次雜湊。

多物件共同形成牆體時，可使用經測量重建的複合輪廓：`review <job> --prepared-plan <json>`。輸入須為 dwg-skp-model-v2，綁定本次文件、分類及決策雜湊；每件保留真實 source_entity_ids、主要與完整來源圖層、輪廓修補證據。decisions.json 的 prepared_categories_by_primary_layer 明列本次核定材料範圍（圖層名稱對應允許類別陣列）。驗證器檢查來源、圖層、類別、實際核定高度及全類別體積重疊；此介面不授予核定，也不在寫出階段修補。具體數值依本次決策，不採案例常數。輪廓檔另納入版本雜湊。若使用者已明確授權完成該方案的輪廓閉合與宿主檢查，沿用這項授權；若用途、範圍或尺寸因此改變，再確認受影響內容。

修補保存原始／修補輪廓、端點或共享邊依據、位移／材料變化與來源ID；在review前完成，writer不另發明補線。沿用已授權的閉合、去重與宿主檢查，不把每次可追溯修補變成新的確認題；超出來源界限、改變用途或擴大範圍時才展示差異。數值共同精度與語意接線分開記錄，不能把案例的固定容差當通用預設。

牆腔體及門上段另作拓樸核對：

- 牆線可由多個線段／圖塊實例共同形成外圈與被包覆的內圈。只有確認同一牆系、同一適用平面與閉合關係後，才提出 `outer` 加 `holes` 的材料輪廓；驗證牆材在圈間、每個內圈仍為空。非零Z或3D線段先核對共面性，投影必須有本次授權與位移紀錄；不同標高的線不可湊成一個2D閉環。
- 帷幕或其他周界抽取後若有未閉合段，先保留原端點與缺口位置請使用者確認。已核定的微小來源對齊修補可沿用，但不能以外包矩形取代缺失的周界。
- 門以門框與兩側宿主牆定位。雙扇要收齊兩片；滑門、折門不能因沒有旋轉弧就被排除。門扇範圍與門洞、過梁材料分開保存；過梁平面長度到兩側牆體外側面，平面深度等於該處宿主牆厚。兩側牆體尺寸不一致、無法求外側面或淨高／梁頂缺來源時，保留候選並列具體缺項，不按門扇bbox生成實體。

總覽、局部圖、文字及確認清單由同一候選ID對應；排序或改視圖不能改被核定的物件。先結合全圖註記、圖塊、原牆內邊、門檻及用途縮小方案，只問讀圖後真正缺少的資訊。已確認或來源清楚的差異在摘要提醒即可；未知用途保留為待解，不用reference偷偷排除。

### 從 AutoCAD 選取修正目前開啟的 SketchUp 模型

使用者要求直接修改目前開啟的白模時，不使用下方另存修訂命令；切至獨立的 [dwg-skp-sync](../../dwg-skp-sync/SKILL.md)；四種局部動作與單次 Undo 的流程由該技能維護，不載入此初次建模流程。

### 批次重建／另存版本（只有使用者選擇此模式時）

先在與原工作相同的 DWG 中選取要補入的門或開口物件，再以 AutoCAD 唯讀選取工具保存完整 Pickfirst 快照。隔離圖層或物件只幫助觀看，沒有實際選取時不能當成修補範圍。接著從原核定工作建立新版本：

```powershell
python scripts/dwg_to_skp.py opening-selection-attach D:/Models/job-001 `
  --selection-json D:/Models/selection.json `
  --to D:/Models/job-002 `
  --height-mm 2100 `
  --base-z-mm 0 `
  --opening-type door
```

高度與基底是本次方案值，須來自圖面、既有明確決策或使用者指示；不能把範例 2100 mm 當成通用門高。命令會驗證來源圖路徑、單位、選取是否截斷，將頂層選取 handle 對照正規化來源物件或圖塊後代，並以其 WCS 足跡裁切已核定的連續牆體。每個範圍必須唯一命中宿主；零個、多個或不連續宿主、超出牆高、與既有補洞重疊均列為 blocker。

原工作必須有可驗證的當版核定，來源與既有計畫不得漂移。新版本保存 `opening-selection.json`、不可變的`opening-base-model.plan.json`及`opening-base-origin.json`，更新decisions及model plan，並產生帶選取高亮的review.svg／review.md。檢視牆肢、窗台／上樑、洞口淨高、來源handle、宿主lineage與淨空稽核後，仍須對新review重新approve，再build到新的SKP路徑。已有通過QA的舊SKP時，由綁定其雜湊的複本保留場景及原Tag；原檔不改動。這是刻意另存的批次模式，不可拿來回應「直接修改目前開啟模型」。

## 3. 保存真實核定

先展示預覽與摘要，收到使用者明確同意後，將實際原話與 review.json 中的 review_sha256 傳入：

```powershell
python scripts/dwg_to_skp.py approve D:/Models/job-001 --review-sha256 <本次預覽雜湊> --confirmation <使用者實際確認原話>
```

文字參數是核定紀錄，不會代替人作決定。保存實際原話；整理的摘錄標為摘錄，不能冒稱逐字原文。Codex 不得自寫使用者已確認、挪用舊案例核定或以測試同意生成正式模型。已採納的工作預設可用於準備本次方案，不需要每個符號重問；approve仍綁定本次可檢視的方案。

來源 DWG、外部參照、候選幾何、決策、預覽或程式變更會使舊核定失效。重新整理受影響內容並確認，不手改雜湊繞過檢查。

## 4. 生成與檢查

```powershell
python scripts/dwg_to_skp.py build D:/Models/job-001
```

預設輸出 delivery/model.skp，可用 --output 指定新的 SKP 路徑，不覆寫既有交付。輸出後實際重讀，確認來源 Tag、群組、屬性、Untagged 幾何、封閉性、座標、高度、體積與洞口。delivery_qa.json 保存結果與核定關係。

同目錄輸出 model.overview.svg／model.plan.svg，以及可用渲染器產生的 PNG。這些圖來自實際重新讀入的 SKP。逐項 QA 另保存為 model.skp.qa.json，失敗時保留診斷，不保留通過交付狀態。

預覽從重讀結果製作。QA 失敗時保留診斷以修訂，不宣稱模型完成。

以既有交付作為來源綁定版本底檔時，native_base保存原SKP路徑／SHA及原payload摘要；檢查後載入複本，保留原根、未變葉件、Tag及場景。被替換葉件的場景隱藏關係按明確lineage移接；另驗相機、圖層可見性及未變檔案雜湊。實際白模材質、群組材質／面材質及透明度另列QA，不只檢查材質名稱。

驗證器直接核對實際保存的幾何及來源，不能只呼叫planner重算同一配方就宣稱通過。開口按宿主、作用樓板及全Z範圍檢查；轉折厚度連續涵蓋來源細分、孔邊交會及端部，不只抽直段中點。群組數、單殼／封閉性、材積、來源覆蓋與開檔可見狀態分開核對，幾何全過也不能取代用途判讀。預覽綁實際SKP SHA，另保留本次工作預設及所有覆寫。

原生引擎可能合併極短邊。writer可用局部放大建構及葉群組逆縮放保留同一世界座標，不在寫出時移界或簡化來源；保存實際建構變換，重讀QA依存檔變換量測世界座標、厚度、孔域與體積。不能只量放大後的局部幾何，也不能縮回頂點導致引擎再次合併短邊。無法保存精確材料仍阻擋。

## 5. 修訂

```powershell
python scripts/dwg_to_skp.py revise D:/Models/job-001 --to D:/Models/job-002
```

新版本複製輸入與決策，不帶入舊 approval。修改後再 review／核定／build。若來源已改變，先對新來源 inspect，再移植仍適用決策，不沿用過期幾何。

若使用者明確選擇批次重建／另存，AutoCAD 已選取的漏建門洞可使用上節 `opening-selection-attach`，使選取 handle、範圍、宿主與幾何變更一同進入雜湊及稽核。使用者指定直接修改目前開啟模型時則使用 `live-repair-*`，不可自行改成另存版本；兩種模式都不把座標或 handle 寫進通用規則。

## 個案與通用規則

通用規則在 scripts/runtime/dwg_semantic/config/default_rules.json；固定白模規範由modeling_standard.py執行。技能包不保留專案案例、絕對座標或歷史數量。外部專案決策只存其獨立工作資料，不能取代上述固定規範或本次核定。

API 依據：[AutoCAD API](https://aps.autodesk.com/developer/overview/autocad-api)、[SketchUp C API](https://extensions.sketchup.com/developers/sketchup_c_api/sketchup/index.html)、[SketchUp Tag](https://ruby.sketchup.com/Sketchup/Drawingelement.html)。
