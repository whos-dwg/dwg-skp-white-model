# v0.2.1 預覽版驗證紀錄

發布候選驗證日期：2026-10-27。測試使用隔離的公開來源快照、Git 封存及合成資料。

| 檢查 | 結果 |
| --- | --- |
| 完整套件，啟用可用原生環境與註記 fixture | 852 passed、127 subtests passed；0 failed、0 skipped |
| 原五項失敗 | 同步 doctor、help、拒絕 build、外部依賴覆蓋防護、已載入異版核心拒絕均通過 |
| 新增回歸 | LF／CRLF 來源、檔案建立順序一致性、真正 Git commit → ZIP → 解壓 → 成對安裝及竄改拒絕；原打包器四項皆失敗，修正後通過 |
| 發布封存檔 | 實際來源 ZIP 與技能 ZIP 分別解壓，先 dry-run／安裝核心，再 dry-run／安裝同步，從無關工作目錄執行兩個入口的 --help／doctor |
| 完整性 | 48 個 runtime 與 Ruby bridge 的內容與 v0.2.0 LF 內容相同；清單重新產生，保留逐檔驗證 |
| 合成 JSON | 產生候選預覽並維持預期 blocked；未核定時不建立 SKP |

環境為 Windows、Python 3.13.13、Shapely 2.2.0、SketchUp 2021 C API；原生註記資料使用先前由 AutoCAD 2026／.NET 8 解碼的合成 DWG。測試依賴與暫存目錄獨立設定。

原 v0.2.0 發布後驗證另完成實際合成 DWG／DXF 幾何等價、六項註記測試，以及使用者核定的七個材料群組 SKP 寫出、重開、來源 Tags、尺寸、單柱副本移動還原與瀏覽器下載一致性檢查。該輪共 843 項通過、5 項因本次打包缺陷失敗；原發布前 842 項通過的紀錄未涵蓋 GitHub 換行正規化，此版補上封存驗證。

## 重跑

依 README 安裝測試依賴後執行：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m pytest tests -q
```

具備 SketchUp 原生環境時設定 `DWG_RUN_NATIVE_TESTS=1`；六項註記測試需以 `DWG_ANNOTATION_TEST_JSON` 指向相符的合成註記抽取資料。缺少環境或 fixture 時須如實記錄 skipped。

## 範圍

本次未重新進行開啟中的 AutoCAD／SketchUp UI 同步與 Undo／Redo；相關契約及 Ruby 交易測試在隔離環境執行。這些結果不代表任意圖面、所有原生軟體版本或真實專案均已完成驗證。
