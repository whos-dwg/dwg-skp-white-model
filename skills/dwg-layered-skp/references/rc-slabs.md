# RC 樓板：從平面、註記與區域推導

來圖常只表達留洞與降板，沒有完整樓板輪廓。板域是需要推導的新構件；不可沿用牆柱「找到封閉材料輪廓再拉高」的入口，也不把頂／底板圖層一律歸為輔助參考後結束工作。

## 已採納的白模預設與能力界線

- 名義板厚預設200 mm，屬使用者白模工作設定，不是普世結構法規。明示尺寸、有效的板頂／板底差及本案覆寫優先；特殊板採explicit_required而沒有厚度時才列具體缺項。一般缺厚不再一律詢問，未知標高仍不能用200補成Z。
- 已定位的有界折線／叉號樓板符號先按開口處理，特例再改。這不表示所有polyline都會開洞，也不要求每個合格符號另有「留洞」文字。先載入來源綁定的既有project／object explicit overrides；來源明確降板、無孔或其他用途時，採其證據。這是符號用途的優先序，不能取消整間樓梯含平台挖空等固定規範。
- 新工作與新版修訂固定採whole_uniform_thickness：由來源板底高程域形成名義等厚的單一材料殼，原外框、孔位及孔界不動。整片不等於父群組包裝，主要Tag及材料差帳都納入同一review／approve與實檔QA。prisms與whole_union只保留底層計算／歷史重跑相容，不是新交付可選的降級模式。只支援分區水平板與垂直轉折，斜板或一般自由曲面仍不在範圍內。

## 判讀順序

1. **先辨明樓層與樓板身分。** 分開本層腳下的 RC 板、上方／下一層 RC 板及天花。本層 H 與 H± 註記通常描述本層，不能直接移到上方頂板；核定牆高也不等於已證明 RC 板底。上方板可由對應上一層／剖面／板底註記證明，或提出明示的白模假設供核定。

   記錄圖面標題／剖面／板件說明或使用者對作用對象的確認。註記與引線能證明數值對應哪個區域，不能單憑此對應、圖層名稱含「底板」，或 H 數值為負就斷定是腳下樓板。使用者更正為頂板時，保留原始 H、原始層名與更正原話，重新綁定模型角色；一般區板底基準及各區相對高差要分開換算，不能直接把負值當模型絕對 Z。
2. **跨圖層讀註記。** 搜尋 TEXT、MTEXT、圖塊的實例 ATTRIB、標高符號、引線與圖說；保留原文、位置、來源 ID 及圖塊路徑。先讀全圖的一般說明，即使文字在平面範圍外。原始 Z=0 不代表沒有可讀標高；XY 單位與標高文字單位分開判斷。不能把 ATTDEF 的模板文字當作插入實例值。
3. **分清標高語意。** 分開建築完成面 FFL、結構板頂 SSL/TOS、板底 BOS/soffit、絕對與相對標高、板厚及面層厚度。H-0.550 表示相對 H 低 550 mm（當文字單位已確定為 m），不一定表示相對一般結構板降 550 mm。先判明一般板頂，例如 H-0.100，才能算區域落差。成對標高須依標高符號的上下排列、「建／結」與引線關係辨明；附近一個「結」不能套到所有相鄰數字。欄杆與幕牆高度另列；A.F.F.保留為淨高／相對高度候選，確認起算面、作用對象及單位後才轉成板底Z。使用者已確認為RC板下淨高的本次來源，不再以「天花」名義排除。覆土、面層厚度或排水坡度也不直接當板厚或板坡。

   圖塊屬性的 THICK／BG_D／BG_T 等名稱也要讀取其來源構件；例如立管的厚度、底／頂值不是樓板參數。標高按 m 表示時，板厚文字仍可能按 mm 表示，不能共用單一文字倍率。

   A.F.F.適用範圍先用原牆、門上段、房名、門檻與區界縮小，不能僅用最近文字或同一大連通域決定。補問前先檢查門洞及功能分隔；沿用已核定的一般值時，分欄記「來源未標」與「沿用建模值」。在原註點用FFL＋AFF算功能域板底，不把每條底板H界都造出一條新天花階差。來源防火門檻可作2D語意界線，不因此解除其3D排除。
4. **推導板域。** 結合外牆、帷幕、建築外輪廓及明確板緣線形成候選包絡；門洞造成的牆線中斷不應把樓板切斷。保留退縮、凹角與不同外緣候選；需區分外牆完成面、帷幕面、結構軸與實際板邊。不能用包圍盒或凸包填掉退縮、天井或挑空。沒有可閉合的外緣網絡時提出有證據的連接，不自行補成大矩形。
5. **定位符號與實際孔域。** 圖層、文字、X／折線、引線及區界共同判讀，按已採納的開口預設處理合格板符號。現有判讀器辨認有四角閉合框的完整X，或由相對角與內膝點構成、具有折角的兩段bent-diagonal標記；一般closed polyline、單根斜線或只有面積不代表符號。取原閉合淨界或牆內邊，不取符號bbox；兩端各有標記及局部框時分別處理，不能跨中間未標區合成大孔。無法定位板域、輪廓或作用層時，列出該幾何缺項，而不是先猜孔再放寬QA。

   井道尤其要分開「井道外圍粗線／圍塑邊界」與「內部車廂或設備打叉」。若來源可辨完整井道界，扣板候選採井道包覆的整個範圍，不以較小車廂X界取代；保存兩者的來源ID、面積及差異。跨圖層的紫色填充、開放折線或共用牆邊也要盤點，局部開放符號可由有來源的相鄰界線補足候選，但修補與作用板仍須審查。重複或互含的符號先去重，不能重複扣材；每個來源標記都要有已採用、被另一標記涵蓋或待解的紀錄。

   既有明確opening／level_change／retain／exclude覆寫先行；明文降板或無孔也優先於符號預設。純H是高程證據，不能單獨取消已定位的符號開口預設。來源降板特例需另保留板頂／板底與厚度，不能挖空。房間、家具、設備與門扇不因有邊框就變洞；井道用途及跨層連續性是判讀證據，不能代替實際邊界。

   每孔指定作用板。上下板明確輪廓不同時各按其原界開孔，再提醒差異，不強求面積一致；沒有對應來源或板域時不擴板、不默投另一層。只有大小重疊框仍有實質多解、作用層衝突等真缺項才確認，已清楚或已覆寫的範圍不逐一重問。

   樓梯間固定取整間範圍，包含平台；以實際牆內邊、門檻等形成唯一封閉淨界，再綁作用樓板。梯段、步級線與定位框只用來辨用途，不取其bbox作孔。來源不足時列「缺精確封閉界／作用板」，不再問是否包含平台。防火門及捲簾不建3D本體，仍讀其門檻／洞口作2D區界證據。
6. **組成可確認的方案。** 預覽包含總板域、開口、降板分區與原圖註記；各項編號在總覽、局部圖及問題清單一致。表列每區板頂／板底／厚度／落差／適用樓層，分開「來源明示」「幾何推導」「待核定假設」。先盡量讀圖，只補問影響成模但仍無依據的項目。

   底／頂板角色更正時需重算覆蓋範圍與孔洞適用對象。若舊分區輪廓已扣掉某層孔洞，不能只刪除 openings 清單便稱為新樓層；應從來源重建未扣洞區域，再僅扣新作用樓板的孔。舊裁切範圍若會排除本次應納入的標高區，要畫出差異並釐清，不能靜默遺失區域。

## 可執行的判讀入口

```powershell
python scripts/dwg_to_skp.py slab-review D:/Models/job-001 --output D:/Models/slab-review-001
```

輸出 `slab-review.json`、`slab-review.svg`、`樓板判讀.html`。不更動原工作核定、DWG 或既有 SKP。每次指定新的輸出資料夾。

- `--elevation-unit m`：只在標高文字的單位已確認時指定；不因幾何為 mm 就把 H-0.050 當作 -0.05 mm。
- 重複 `--boundary-layer <原圖層>`：以已判讀的外緣線層形成板域候選。未指定時使用分類中的建築帷幕外圍網絡。多個斷開的封閉外圈各自保留，內部房間面合回gross候選；來源ID只綁定實際支持該外圈邊線的物件，不把其他板或僅碰到端點的線混入。保留來源 ID 與輪廓取整精度；工具只提出封閉包絡候選，不證明那就是結構板邊。若圖面沒有帷幕外圍，Codex 仍需判讀外牆／板緣來源後提供這個輸入。
- `--document <新document.json>`：可用相同 DWG 雜湊的新版唯讀抽取補齊 ATTRIB／MLEADER；不能挪用不同版本圖面的文字。
- 工具列出的標高、附近「結」字與封閉區域都保留來源證據，不以最近文字直接核定。合格有界符號可按工作預設先列開口；Codex仍檢查來源用途及explicit overrides，不把每個符號重新變成必答題。
- 舊正規化資料未記錄實例屬性與引線覆蓋時，明示抽取限制；必要時重新抽取，不能把缺欄位解釋成原 DWG 沒有資訊。

判讀API為 `prepare_slab_review(..., slab_decisions=None)`。在傳入的slab_decisions中，`region_overrides`以完整frame/entity ID對應 `{action:"opening"|"level_change"|"retain"|"exclude", reason:"..."}`；exact-ID決策先於預設。案例ID及理由留在案例決策，不能寫進通用圖層規則。判讀結果仍須轉成有效的systems／zones／openings方案；符號預設不是任意mesh寫入入口。

`slabs.symbol_endpoint_tolerance_mm`僅控制符號端點與框角的歸屬，預設採review的precision_mm並保存實測距離；它不改來源精度或opening footprint。需要較大歸屬容差時保存測量及原因，不能因此放大孔域或沿用某案例數值。

梯線與實際牆內邊有細微出入時，可在來源綁定的recognition.settings指定stair_mark_boundary_tolerance_mm，並保存實測偏差與本次判讀理由；預設0表示不增加共同精度以外的歸屬餘量。此值只判斷梯線屬於哪個已閉合淨區，實際開孔仍用原牆內邊／門檻形成的完整區界，絕不buffer孔界，也不能補出缺失的圍牆。

`slab-review` 現另含 `automatic_opening_recognition`：整個電梯井外界、閉框與開放折線符號、整併後的唯一孔域，以及樓梯精確孔界待核清單。開放符號須四邊均有實際來源線支撐，保存各邊來源、支撐比例與符號至界線的偏移；不直接把折線bbox當洞。這些XY候選仍需綁 `applies_to_system_id` 與有效板域／Z，不能因辨識成功便自動挪用到上層或下層樓板。整合圖及來源追溯入口見 [完整流程](workflow.md)。

### 樓梯整間孔的作用板綁定

樓梯的完整淨界與「包含平台」不是待選方案；只剩實際區界及作用板須有依據。候選已帶 `applies_to_system_id`（或複數 `applies_to_system_ids`）及非空、可追溯的 `slab_owner_evidence` 時直接沿用，不重問；既有 `slabs.region_overrides[完整候選ID或完整source_entity_id]` 亦可用 `action:"opening"`、作用板ID及非空 `reason` 表達來源／本次已核定決策。單有XY相交或樓梯用途不算作用板證據。

需要另明列候選級決策時，在 `decisions.slabs.stair_opening_bindings` 以辨識結果 `staircase_openings` 的完整候選ID為key；不能用raw handle、定位框或bbox代替：

```json
{
  "slabs": {
    "stair_opening_bindings": {
      "<staircase_openings中的完整候選ID>": {
        "applies_to_system_ids": ["<本次systems中的實際ID>"],
        "reason": "填入實際來源或本次既核定的作用板依據"
      }
    }
  }
}
```

這是欄位例，不是可直接核定的決策。`applies_to_system_ids` 必須非空、沒有重複、每項均為本次有效的system ID，`reason` 不可空白；單數 `applies_to_system_id` 僅作相容欄位。此專用候選級決策最具體，優先於source-ID層的覆寫；不同exact-ID來源決策互相矛盾仍須釐清，不能任取一個。多片板都須開此孔時，先證明各板身分及來源對應後明列多個ID，不能把固定「含平台」的規範當成扣全部上下板的依據。

缺精確封閉界列 `whole_stair_enclosure_boundary_unresolved`；缺作用板證據／綁定列 `whole_stair_opening_system_binding_required`；未知system、缺理由或來源作用板互相衝突列 `whole_stair_opening_system_binding_invalid`；完全未落入目標板域或目標extent缺界列 `whole_stair_opening_misses_target`。部分超出板域時保留完整孔界與板外差帳，只扣和已綁定目標板的交集，不擴板。均保留候選及局部位置，只補齊該項依據，不再詢問是否挖平台，也不跨板補扣或放寬QA。實際採用孔保留候選來源、作用板及完整孔界。

## 接入可編輯的原生 SKP

樓板純幾何引擎在 runtime 的 slab_modeling.py。新工作直接準備 `decisions.slabs`，與建築構件一併review，不必先核定一個沒有可信板底的建築方案；已有核定工作才使用下方slab-attach修訂流程。所有 XY／Z／厚度以 mm 表示；範圍、一般板面、局部降板及留洞的來源分開保存。一般板的缺／null厚度按default_200補200並保存工作預設來源；explicit_required特殊板缺厚及未知Z仍為缺項，不能以0或牆高補值。

```json
{
  "enabled": true,
  "confirmed_scope": false,
  "assembly_mode": "whole_uniform_thickness",
  "derived_tag": {"tag": "RC樓板_推導", "source_layer": "RC樓板_推導", "kind": "derived"},
  "floor_datum_m": null,
  "systems": [{
    "id": "FLOOR-01", "role": "floor",
    "extent": {"outer": [], "holes": []},
    "source_entity_ids": [], "boundary_source_layers": [], "annotation_source_ids": [],
    "default_top_z_mm": null, "default_thickness_mm": 200,
    "thickness_policy": "default_200",
    "zones": [], "openings": [], "evidence": []
  }]
}
```

以上為 `decisions.slabs` 的欄位說明，不是可核定的空白方案；extent 必須填入有效的來源綁定平面。此例只示意floor，若本次有牆柱或全高帷幕，仍須補齊其上方overhead系統及可信板底覆蓋，不能以floor系統替代。

- 系統 role 為 floor 或 overhead；本層底板與上方頂板分開，不跨層繼承標高或扣洞。
- zone 含 id、footprint、source_entity_ids、annotation_source_ids。method: constant_thickness／explicit 指定 top_z_mm 與 thickness_mm；uniform_shift 指定 shift_z_mm（相對一般板頂偏移，等厚）；explicit_bottom 以 top_z_mm／bottom_z_mm 或 bottom_z_mm／thickness_mm 定義。system／zone的thickness_policy可為default_200或explicit_required：明示厚度／有效底頂差優先，前者缺厚補200並記錄default provenance，後者未提供厚度仍阻擋。這不補未知標高，也不把面層、土厚當板厚。
- review另列來源厚度候選的明確單位、缺單位及衝突狀態，不能把default200假設標成來源證實。caller需將已判讀的源厚轉入system.default_thickness_mm或zone.thickness_mm；prepare_slabs不會自動跨區尋找／綁定附近文字。
- opening 含 id、footprint、source_entity_ids、applies_to_system_id。只有該系統扣洞；超出部分留在稽核、僅扣與目標板域的交集，完全未落入板域或無來源時阻擋。重疊但同剖面的區域可整併並保留來源；不同標高／厚度的重疊區域必須先解決，不按輸入先後蓋過。
- step_risers 以 enabled、thickness_mm 及 profile 明示接法，可由 system 覆寫。接法為低板側完整立板時，指定 profile="edge_aligned_full_depth"：從低板底至高板頂，厚度全落在低板側，外面對齊原共享邊；低板扣除被立板取代的邊帶，保留水平板及立板合併後的原XY包絡。同剖面、同Tag相連板帶合併，冗餘共線點清理不移動邊界；轉角以最高板頂優先分配XY，不沿Z切成多層。留洞不填，窄區可全由整高立板取代，仍保留原幾何快照及實際替換ID。舊profile="centered_gap_only"僅供重跑已採置中補縫的歷史方案，不作本接法的替代。

  此profile只說明底層分件組裝與歷史相容；新方案一律以whole_uniform_thickness按來源板底重建等厚材料域，不是讓使用者另選prism交付。孔邊只剩窄帶或多高交點時，僅有thickness_mm=200不證明實際全長皆200；不能默交局部薄帶。
- 每系統可指定 tag_binding：kind=source 時使用有真實來源支持的原圖層 Tag，並可與原圖參考共用相同 Tag；kind=derived 時使用新推導名稱，不能覆蓋原圖既有 Tag。zone 可另記有該區來源支持的 tag_binding；整片仍只有一個主要Tag，其餘板區原Tag保存於來源屬性，不為多Tag拆開或複製同一材料。立板跨多來源時依實際連接材料貢獻面積記錄候選與依據。source_layers保留完整真實來源層；沒有完整原始板框時不冒充既有圖層，有實際來源時也不強迫改用衍生層。derived_tag為相容的衍生模式欄位。
- 新方案assembly_mode固定whole_uniform_thickness，slabs及每個system不得改為prisms／whole_union。整片系統必須實際連通且為單殼；不相接的板域分成不同system。每個system要求一個一致的名義厚度，不平均衝突厚度。多個原Tag合為整片時，必須以whole_primary_tag指定真實來源Tag之一，完整保留其餘Tag及分區／孔來源，不複製材料。

### A.F.F.的來源綁定

slab-review逐token分開H、FFL與A.F.F.的用途、單位及資格；同一段文字含A.F.F.不應抹掉其中FFL。明確RC板底、完成天花、欄杆頂、帷幕頂及門楣分型；不以最近文字猜功能區，也不將所有淨高轉成板底。

需要由A.F.F.產生板底分區時，在slabs.functional_regions提供有真實source_entity_ids的footprint；annotation_bindings逐筆指定id、source_entity_id、token_kind="aff_height"、token_index、target_surface="structural_bottom"、reference_surface="finished_floor"、reference_z_mm、reference_annotation_source_ids、applies_to_system_id、functional_region_id、basis及reason。basis為source_explicit或confirmed_job_decision；前者必須與來源文字的用途相符。來源缺單位才可補unit；不能覆蓋文字已標單位。以floor_datum_m或source_to_model_z_offset_mm證明來源FFL到模型Z的換算。

已解析的RC板底綁定會交給prepare_slabs形成explicit_bottom候選區；其他用途只保留相應高度候選。未知參照、未知作用板、無效區界、互相重疊但異高的明確綁定都阻擋。annotation_binding_audit與原文納入prepared重算及實檔屬性；候選解析成功不替代confirmed_scope、當版review及真實核定。

### 共同接點與原生幾何精度

來源交疊運算可能形成小於 SketchUp 可保留尺度的尖角或偽孔。不能放寬封閉性、材積、留洞或交疊檢查來交付這些構件。edge_aligned_full_depth 可明列 geometry_precision_mm，以最小已實際驗證的精度建立共同接點；先取整來源，將所有來源邊界一起節點化、分配來源區域，再建立板帶並共同分配交會區。禁止各板件分別取整後直接拼合，也不能在寫入 SKP 時另移板邊。

slab_audit.geometry_precision 保存原始、直接取整、來源共同接點、板帶交會及最終構件各階段，包含來源／Tag／Z 對應、位移界限、逐孔變化與整體包絡差。prepared 與原生 QA 必須由原始快照獨立重算，不信任自填的 canonical 結果。共同精度只處理數值表達與接點一致性，不授權更改原外框或孔界；數值誤差在驗證容差內另列，不能以「已給位移上限」或核定文字合理化實質移孔、縮孔、補孔或移外框。無法保留固定邊界便阻擋，一般方案也不自動套用外部專案的精度值。

只有修訂已有核定建築工作時才建立獨立版本；原版本必須有來源一致且可驗證的核定方案：

```powershell
python scripts/dwg_to_skp.py slab-attach D:/Models/job-001 --to D:/Models/job-002 --slab-decisions D:/Models/slabs.json
```

可用 --document 指定同一 DWG 版本的新版註記抽取。此步驗證原工作核定及來源新鮮度，複製原核定的 base-model.plan.json，保留原建築構件及原交付雜湊；不帶入舊 approval。原工作已有通過QA的SKP時，新版以其雜湊綁定複本更新，不重建一個遺失既有場景／Tag的空白檔；原檔不變。接著 review／approve／build 使用原本同一套版本綁定。來源、舊交付、分區、厚度或預覽更動都會受檢查。

確認前先展示總板域、各區標高、降板量、孔洞及局部剖面。一般板明示使用名義200工作預設，不再當缺厚必答；只有explicit_required特殊板仍缺厚、缺Z或來源衝突才列問題。來源結構尺寸與白模工作預設分開列示。已能由來源判讀的外框、標高及各板不同孔域先完成方案並提醒，不逐條轉交使用者判圖。

使用者填妥／確認後，更新本次 slabs 決策（confirmed_scope=true 並記錄真實確認），重新 review；無阻擋時以實際原話 approve 該版本，再 build 到新檔。額外 HTML、PNG 與待確認資訊可用工作根目錄 presentation-files.json 的 files 陣列納入雜湊綁定。

## 成模與驗證契約

- 從未扣孔gross高程域準備，再依當版各作用板開口扣材；不能僅清空舊openings而沿用已藏孔的netparts，也不能為維持gross完整而補回已確認孔。新方案把來源分區屬性保存於單一真正整片材料體；prism分件只作底層計算／歷史相容，不能降級交付。計算板頂、板底與厚度保留參考基準，不以牆高取代板厚。
- 原 DWG 板域、開口／降板來源保留原圖層；有實際板域或板區來源時，可核定以該原圖層作主要 Tag。無適用原圖層時使用核定的衍生 Tag，並記錄 boundary_source_layers、annotation_source_ids、region_source_ids、推導方法與假設。不把推導名稱冒充為原 DWG 既有圖層，也不為了多層來源複製重疊樓板。
- 檢查總板域覆蓋、開口貫通、降板側面、分區體積重疊、厚度、標高及和牆柱的交界。樓板碰到牆柱的合理接合按核定構造處理，不能用「所有構件零交疊」的一條規則誤刪構造。
- 每條異高共享邊須核對高低板與垂直轉折的接觸、各Z區間缺口覆蓋及實際厚度。水平板全部封閉仍可能有板間裂縫；件數／體積通過不能代替連續性。新方案把垂直轉折併入整片材料體並保存來源與主要Tag；底層獨立立板不是最終多件交付選項。
- 確認後另建新版本 SKP；保留既有場景及原Tag。新頂板交付保存 presentation.overhead_visible=true，開檔顯示頂板與所屬立板，另有室內視圖；使用者明確選隱藏時才設false。舊payload無此設定時保留舊顯示邏輯，不能修改歷史交付。

原生寫出後重新讀入實際 SKP，按 slab_audit 的 assembly／zone 核對淨水平板域、孔洞、底頂、厚度及體積；立板取代的邊帶另行計量，不能誤稱水平板淨面積仍包含立板投影。edge_aligned_replacements 保存原水平件、assembly／zone與替換ID，需從原件重算原共邊、低板側全厚帶、完整高差剖面、XY包絡與每個Z區間材料。逐實際立板面核對只有自身底頂兩組Z，且乾淨共面側面無分層接縫。每個葉群組可獨立編輯，原始面／邊皆 Untagged；重新讀入也要核對實際可見狀態。

一般體積重疊仍阻擋。只有核定的 slab_join_policy 可處理有來源的特定局部接合：明列兩件真實 ID、來源、允許的 XY／Z 接合包絡及最大交疊體積；不能免除樓板彼此、牆彼此或未列出的交疊，更不能豁免新方案牆柱與全高帷幕收到當地板底的固定要求。驗證以實際重新讀入幾何重算，不只看文字理由。

## 整片與名義等厚的模式契約

整片指每個樓板系統有真正連通的材料聯集與閉合殼，不能只把多件放入一個父群組。固定的whole_uniform_thickness使水平板身與垂直轉折按本次名義厚度相等，保留高差、實際孔、凹角及原外框；一般預設厚度200可被明確尺寸／特例覆寫。由通用prepare_slabs、prepared驗證及同一native writer執行，不依賴案例腳本；whole_union只作歷史相容，不是新方案的替代。

等厚修正可能需要改變舊接邊材料。保存「舊基準−核定孔＋等厚補材−被替代材料」的差帳，並由新的正確材料域作驗證目標；不能強求與舊分件總體積不變而保住錯孔或薄帶。孔邊空間不足時，只在原高低板的真實共享邊採有來源的改側，記錄原可用寬、來源件、增減體積與理由；兩側均不足就阻擋。這項改側須展示於本次預覽後核定，仍保留外框和孔，不用未受來源限制的全域buffer補洞。

原生重讀後沿全部來源細分、轉角、孔邊交會及端部連續核對厚度及全Z孔域，不只量直段中點。對名義200與實際微小數值差分欄報告；共同精度需有raw重算及位移界限，不能拿它掩蓋實質100薄段。獨立驗證讀actual triangles／mesh，不以planner自稱的canonical或相同配方重算代替；同時檢查單殼、法線、正體積、內部分隔、未授權材料差與可見狀態。

單一整片葉群組只能有一個主要Tag，保留全部來源層／分區及孔修飾來源；牆內邊形成的孔可來自牆層，不因此把宿主樓板Tag改成牆層，也不複製重疊材料冒充多Tag。whole_slab_audit保存原始分件、共同精度、材料域、外殼與差帳，prepared從來源決策重算；native QA獨立讀取保存後三角面，核對全Z截面、單殼、法線、水平及轉折厚度與開孔。歷史交付的驗證狀態不因技能升級改寫。

## 帷幕與RC的材料交界

降板或立板仍可能與板底收高後的帷幕玻璃／配件有局部材料相交。必要時明列building_slab_cuts={enabled:true,operation:"subtract_rc_material_only",target_component_ids:[...],target_categories:["curtain","curtain_accessory"]}，只扣實際RC體積，保留其餘材料、來源與主要Tag。它是板底收高後的局部材料差集，不取代所有牆柱帷幕必須到板底的固定要求，也不能以join policy豁免玻璃穿板。

幾何引擎依原構件與RC的Z事件切片，逐片做平面差集，合併可合併的相鄰等形片。切件可獨立編輯；RC來源保存為modifier證據，不能冒稱原帷幕來源。未指定或沒有正體積相交的構件逐欄保持，原模型不更動。

compose保存 base_component_cut_audit；review的prepared驗證必須從已核對雜湊的base-model.plan.json重算切件，不能信任payload自帶的原件快照。component_height_rules填入確定性切片產物的實際base／height，驗證會與重算值逐件比對。原生重讀另按每個Z區間驗「原材料減去實際RC」與實際剩餘材料完全相符，核對移除／保留體積及來源Tag，阻擋過挖、漏挖與未列出的構件改動。

slab-review 的候選輸出成功不代表可建模；slab-attach 的 blocked 也不代表原模型失敗。只有補齊本次樓板決策、完成真實核定及原生重讀 QA 後，才宣稱新版 SKP 交付成功。

## 牆柱與帷幕收到頂板底部

所有新工作固定啟用wall_under_roof={enabled:true,operation:"fit_to_lowest_overhead_underside"}，涵蓋 architectural_wall、structural_wall、column、curtain、全高curtain_accessory及lintel；不因案件類型選擇是否收高。獨立欄杆及非全高配件依semantic_subrole與實際用途保留其高度。按各XY位置最低的實際上方水平板／立板底分區，保留原件基底及孔洞；跨高程件分成獨立prism，必要時延高以貼合較高板底。門洞上方段保留原門洞淨高；板底不高於原件基底時阻擋，不造零厚或負高構件。

實際板域外或留洞內的構件，使用明確來源綁定的 height_projection_regions，包含id、footprint、cap_z_mm、真實horizontal roof_component_ids及source_entity_ids。此設定只延續高度區，不延伸RC板域或封孔。不同高度不得在同一未覆蓋區重疊，不以整件最低值取代分區；新版固定規範要求完整覆蓋，缺該依據時阻擋，不能保留任意原高。

compose先由可信原base重算修高，再做仍需明列的RC材料扣除；prepared逐件重算兩個配方、audit與component_height_rules，不能信任payload自稱的原件快照。wall_under_roof_audit保存原件、實際板件、高度區、投影來源、裁短／延高體積及替換ID；原生SKP重讀核對實際各區板底、門洞、體積、來源與原Tag。採收到板底時不再沿用先前允許牆柱穿板的接合豁免。
