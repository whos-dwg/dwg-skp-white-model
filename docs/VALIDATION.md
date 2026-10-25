# v0.2.0 預覽版驗證紀錄

驗證日期：2026-10-25。測試對象為準備公開的乾淨來源快照，未包含客戶圖面、歷史案例、實際模型或既有本機 Git 歷史。

## 已完成

| 檢查 | 結果 |
|---|---|
| 完整 Python 測試，未啟用選用原生測試 | 814 passed、34 skipped；112 subtests passed |
| 另行啟用上述可用的 SketchUp 原生測試 | 28 passed；15 subtests passed |
| 本次合計 | 842 個測試與 127 個子測試通過；6 個註記 fixture 測試未執行 |
| 技能發布副本 | 48 個共用 runtime 檔案及同步入口相依雜湊已重新打包；打包／啟動器測試納入完整測試 |
| 兩個 CLI 的 doctor | 可載入核心並取得本機依賴診斷 |
| Python 套件建置 | `dwg_semantic_rules-0.2.0-py3-none-any.whl` 建置成功，用於驗證套件設定 |
| 合成平面 | 產生候選平面；預期維持 `blocked`，沒有核定或交付 SKP |
| 分類 CLI | 使用 inspect 產出的正規化文件，正常產生分類及報告 |
| 文件 | 相對連結、PowerShell 語法、Issue YAML、SVG 及 README 桌面顯示已檢查 |

## 測試環境

- Windows、Python 3.13.13、Shapely 2.2.0。
- SketchUp 2021 C API，用於隔離的合成模型寫出與重新讀入測試。
- 本機可找到 AutoCAD 2026 Core Console 與 .NET；環境偵測與實際 DWG 解碼分別處理。
- 原生測試的暫存檔設定在獨立工作目錄，避免受測試執行環境的系統暫存權限影響。

## 重跑方式

依 README 安裝測試依賴後，在專案根目錄執行：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:DWG_RUN_NATIVE_TESTS = "0"
.\.venv\Scripts\python.exe -m pytest tests -q
```

具備 SketchUp 原生環境時，可設定 `DWG_RUN_NATIVE_TESTS=1` 執行選用測試。需要原生註記資料的測試，另由 `DWG_ANNOTATION_TEST_JSON` 指向專用的合成註記 fixture。

## 尚未涵蓋

6 個原生註記 fixture 測試因未提供專用 `DWG_ANNOTATION_TEST_JSON` 而跳過。本次沒有另外執行 DWG／DXF 解碼或操作使用者正在開啟的 AutoCAD／SketchUp 模型；即時同步的契約及 Ruby 交易測試在隔離環境執行。

以上結果不代表所有原生軟體版本、任意來源圖面或真實專案均已完成驗證；本次以 Pre-release 提供使用者先行驗證。
