# 貢獻指南

可從來源解析、平面辨識、幾何與來源驗證、同步資料契約或文件範例著手。先用小型、可公開的合成資料重現問題，再提出修改。專案採用 [MIT License](LICENSE)。

## 開發環境

在專案根目錄執行；Python 需為 3.11 以上：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[review]"
.\.venv\Scripts\python.exe -m pip install pytest PyYAML
$env:PYTHONPATH = (Resolve-Path src).Path
```

Python 幾何／契約測試可先獨立執行。原生測試可能另外需要 AutoCAD、.NET、SketchUp C API 或 Ruby，缺少的環境與跳過案例須如實記錄。

## 修改位置

- 核心修改在 `src/dwg_semantic/`。不要直接修改 `skills/dwg-layered-skp/scripts/runtime/` 後當成來源提交。
- 通用分類設定在 `config/default_rules.json`；打包工具會同步至核心與技能內的規則檔。
- 特定來源的核定與修補存於該工作的 `decisions.json` 或外部 profile，不在通用演算法中加入特定 handle、絕對座標或固定案例數量。
- SketchUp 端同步行為在 `skills/dwg-layered-skp/scripts/sketchup/dwg_live_repair_bridge.rb`，須維持資料契約與一次 Undo 的交易邊界。

相關模組位置見[架構說明](docs/ARCHITECTURE.md)。

## 驗證修改

先跑與變更相關的測試。例如修改同步路由／資料契約：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sync_cli.py tests/test_sync_bridge_contract.py -q
```

核心功能變更完成後執行完整 Python 測試：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

只用 `unittest discover` 會漏掉 pytest 函式與 fixture。請在 PR 記錄實際命令、通過／失敗／跳過數及原生環境。變更來源規則時保留已有回歸證據，新增能表達問題的合成案例；不要只改預期數量使測試通過。

文件或範例修改則檢查相對連結、命令參數與範例輸出，不需為單純措辭變更執行原生建模。

## 更新技能發布副本

核心或橋接器變更通過相關檢查後：

```powershell
.\.venv\Scripts\python.exe tools/package_skill.py
.\.venv\Scripts\python.exe skills/dwg-layered-skp/scripts/dwg_to_skp.py doctor
.\.venv\Scripts\python.exe skills/dwg-skp-sync/scripts/dwg_skp_sync.py doctor
.\.venv\Scripts\python.exe -m pytest tests/test_sync_packaging.py tests/test_sync_launcher.py -q
```

打包會更新共用 runtime、manifest 與同步技能的相依雜湊。同步技能不攜帶第二份 runtime。檢查 diff，確保只包含預期原始碼與發布副本；不要手改雜湊以略過檢查。

## 安裝技能

`--target` 必須指向獨立安裝位置，最末層名稱與技能名稱一致。以下以工作目錄內的安裝驗證為例，不修改個人技能目錄：

```powershell
.\.venv\Scripts\python.exe tools/install_skill.py `
  --target work/skill-install-check/dwg-layered-skp --dry-run
.\.venv\Scripts\python.exe tools/install_skill.py `
  --target work/skill-install-check/dwg-layered-skp

.\.venv\Scripts\python.exe tools/install_skill.py --skill dwg-skp-sync `
  --target work/skill-install-check/dwg-skp-sync --dry-run
.\.venv\Scripts\python.exe tools/install_skill.py --skill dwg-skp-sync `
  --target work/skill-install-check/dwg-skp-sync
```

先安裝匹配的核心，再檢查與安裝同步入口。既有安裝更新時會備份；舊版檔案的退役必須明確指定並保留備份，不整批刪除其他技能。

## 提交 PR 或回報問題

說明具體觸發條件、原本結果與修改後結果，附上驗證命令及未覆蓋的環境。涉及白模規範、來源版本、開口、樓板或同步交易時，寫清楚受影響範圍。

公開附件使用合成或已取得發布授權的資料。請移除客戶名稱、實際圖面、完整本機路徑及機密資訊。需要私下處理的問題見 [SECURITY.md](SECURITY.md)。
