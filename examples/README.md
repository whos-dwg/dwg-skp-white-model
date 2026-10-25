# 合成平面範例

[synthetic-plan.json](synthetic-plan.json) 是為公開文件建立的合成來源，無客戶圖面或既有專案資料。單位為 mm，外框 6000 × 4000 mm，由四段 200 mm 厚的閉合矩形牆界及中央 400 × 400 mm 柱形輪廓組成。這些尺寸只描述本範例。

## 執行

先依 [README](../README.md#quickstart) 安裝依賴，在倉庫根目錄執行：

```powershell
.\.venv\Scripts\python.exe skills/dwg-layered-skp/scripts/dwg_to_skp.py inspect `
  examples/synthetic-plan.json --job work/demo-001
```

`--job` 指定新的資料夾，重跑時改用其他名稱。本範例使用 JSON，不會呼叫 AutoCAD 解碼。

## 預期結果

平面辨識產生一組外圈牆材、一個內腔及一個柱候選。來源共有五個物件，候選材料數量與來源物件數不同。

預期保留 `review.md`、`review.json`、`recognition.json`、`recognition-plan.svg` 與 `recognition.html` 等判讀檔案。安裝 `review` 額外依賴後可另外產生 PNG。README 的[候選平面圖](../docs/assets/synthetic-plan.svg)即由本範例實際執行輸出。

`review.json` 預期為 `blocked`，程序以非零代碼結束。材料拓樸需核定，柱形來源用途與高度等決策未完成，也沒有可據以求出牆頂的上方樓板方案，因此不生成 SKP，也沒有 `approval.json`。

可先看 `blockers`、`decisions.json` 與[完整流程](../skills/dwg-layered-skp/references/workflow.md)理解缺項。請勿替範例建立虛構核定文字或把 `blocked` 改成成功。

## 只產生分類報告

先完成上方的 `inspect`，再將產出的正規化 `document.json` 交給分類 CLI。直接分類使用的是正規化資料格式，不是任意原始 JSON。

```powershell
.\.venv\Scripts\python.exe -m dwg_semantic.cli work/demo-001/document.json `
  --rules config/default_rules.json --source-name synthetic-plan.json `
  --output work/classification-demo.json --report work/classification-demo.md
```

此命令只執行分類，不提供 SKP 交付或原生檔 QA。
