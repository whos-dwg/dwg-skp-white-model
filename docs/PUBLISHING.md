# GitHub 發布內容與操作清單

本文件供維護者設定倉庫、建立 Release 及整理公開附件。首個公開版本採 `v0.2.0` Pre-release，目的地為 [whos-dwg/dwg-skp-white-model](https://github.com/whos-dwg/dwg-skp-white-model)，授權為 [MIT](../LICENSE)。

發布使用乾淨快照，包含程式、兩個技能、測試、文件與合成範例；既有本機 Git 歷史、客戶圖面、實際模型、歷史案例及工作暫存均不納入。

## 可直接使用的倉庫欄位

| 欄位 | 建議內容 |
|---|---|
| Repository name | `whos-dwg/dwg-skp-white-model`，Public |
| 顯示名稱／README 標題 | DWG → SKP White Model |
| Website | 尚無獨立網站時留空 |
| 主要文件語言 | 繁體中文，附英文簡介 |
| License | MIT；Copyright (c) 2026 whos-dwg |

**About description，英文：**

```text
Reviewable DWG/DXF to source-layered SketchUp white models, with approval-bound builds, native-file validation, and CAD-driven local or whole-model delta sync.
```

**About description，中文替代版本：**

```text
將 DWG／DXF 建築平面轉為可核定、保留原圖層的 SketchUp 白模，支援原生檔驗證，以及既有來源白模的 CAD 局部與整模差異同步。
```

**Topics：**

```text
autocad sketchup dwg dxf cad architecture 3d-modeling python ruby geometry white-model codex-skills
```

## 倉庫首頁與社群檔案

| 檔案 | 放置位置與用途 |
|---|---|
| [README.md](../README.md) | 根目錄，專案首頁、環境、快速開始、流程、限制與授權狀態 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模組導覽、資料契約與同步交易 |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | 開發、測試、打包及成對安裝 |
| [SECURITY.md](../SECURITY.md) | 敏感資料及安全問題回報方式 |
| [CHANGELOG.md](../CHANGELOG.md) | 目前以 Unreleased 保存待發布內容 |
| [範例](../examples/README.md) | 合成輸入及可重跑命令 |
| [Bug 範本](../.github/ISSUE_TEMPLATE/bug_report.yml) | Issues 表單：重現步驟、環境與實際錯誤 |
| [功能提案範本](../.github/ISSUE_TEMPLATE/feature_request.yml) | Issues 表單：使用情境與驗收結果 |
| [PR 範本](../.github/pull_request_template.md) | 問題、變更與驗證欄位 |
| [v0.2.0 Release 文案](releases/v0.2.0.md) | GitHub Release description，可直接貼上 |

圖片採相對路徑，從同一倉庫讀取。流程圖為文件圖解，平面範例由合成 JSON 實際產生；不使用客戶案例截圖。

## Release 欄位

| 欄位 | 建議設定 |
|---|---|
| Tag | `v0.2.0`；先確認遠端尚無同名 tag，且套件版本與待發布 commit 一致 |
| Release title | `v0.2.0 — DWG 判讀、分層白模與 CAD 差異同步` |
| Description | 使用 [releases/v0.2.0.md](releases/v0.2.0.md) 內文 |
| Target | 實際通過發布驗證的 commit／分支，不預設分支名稱 |
| Pre-release | 本次勾選，供使用者先行驗證 |
| Latest | 待確定維護策略及實際 Release 狀態後設定 |

Release 文案描述目前功能，不憑本機歷史 commit 推定完整版本差異。發布時核對 `CHANGELOG.md` 的版本、日期、驗證結果與附件一致。

### 附件安排

- 原始碼由 GitHub tag 的 source archives 提供；須先確認該 commit 的內容適合公開。
- 如另附技能包，兩個技能使用同一次打包結果，保留同層資料夾結構、runtime manifest、同步相依檔與 Ruby 橋接器。Python 及原生依賴仍依 README 安裝。
- 公開示範使用 `examples/` 與 `docs/assets/` 的合成資料。可另附由同一版本原生流程完成並取得公開授權的 SKP 與實檔 QA，但須有真實驗證結果。
- 不隨附 AutoCAD、SketchUp、商用 SDK DLL、本機虛擬環境或未取得授權的圖面。

Release 附件以本次實際上傳的清單為準；原生示範 SKP 只有完成獨立原生驗證後才另行提供。

## 發布前檢查

### 1. 確認公開內容與授權

- [x] 確認公開倉庫名稱、擁有者及可公開範圍。
- [x] 採用 MIT，加入 `LICENSE`，同步更新 README 與套件授權資訊。
- [ ] 檢查既有 Git 追蹤檔案。此工作區已有歷史案例、模型及輸出，不可只憑 `.gitignore` 判定它們不會被發布。
- [ ] 逐項核對 `config/profiles/`、`outputs/`、`work/`、`jobs/` 與歷史 commit；排除或取得客戶圖面、案例圖片及本機路徑的公開許可。
- [ ] 如需移除歷史敏感內容，先決定使用乾淨的新公開倉庫或處理既有歷史，不直接覆寫既有 Git 歷史。

以下只讀命令可用來盤點；不要直接 `git add .`：

```powershell
git status --short
git remote -v
git ls-files config/profiles outputs work jobs
git ls-files '*.dwg' '*.dxf' '*.skp' '*.zip' '*.dll'
git diff --check
```

### 2. 驗證待發布版本

- [ ] 依 README 安裝乾淨環境，確認兩個入口的 `--help` 與 `doctor`。
- [ ] 執行合成 JSON 範例，確認有候選輸出，且因缺決策而維持 `blocked`。
- [ ] 核心或橋接器有改動時，依貢獻指南完成相關及完整測試，再更新技能 runtime 與相依雜湊。
- [ ] 執行打包／啟動器測試，確認同步技能使用同一份核心。
- [ ] 記錄實際原生建模、重新讀入及同步結果，標示未驗證或跳過的環境。
- [ ] 檢查 README 相對連結、圖片及 GitHub Mermaid 顯示。

`doctor` 找到依賴路徑，不等於 DWG 已成功解析、SKP 已通過讀回或同步交易已驗證。不要加入未執行的 CI／coverage／下載數徽章。

### 3. 設定並發布

- [ ] 在 GitHub 建立或選定倉庫，填入前述 About description 與 Topics。
- [ ] 明確選取要發布的檔案，檢查 staged diff，建立 commit。
- [ ] 設定實際遠端並推送已確認的分支；再核對 GitHub 上的檔案、圖片與 Issues 範本。
- [ ] 選擇通過驗證的 commit 建立 tag 與 Release，貼上已核對的文案並上傳必要附件。
- [ ] 啟用需要的 Issues 功能；若採私人安全回報，先開啟功能，再更新 `SECURITY.md` 的實際管道。

首個公開版本使用經驗證的工作樹快照建立獨立歷史。後續發布須先核對預計納入的原始碼與對應測試，再選擇確切 commit 建立 tag。

## 對外介紹短文

> DWG → SKP White Model 將建築 DWG／DXF 整理成可核定、保留原 CAD 圖層的 SketchUp 白模。流程保存構件來源與判讀依據，寫出後重新讀入原生 SKP 驗證；既有來源白模也可依 CAD 選取做局部修正，或在空選取時比對整模並更新差異。倉庫附有安裝說明、合成平面範例與開發文件。

分享時附上實際倉庫網址與已公開的 Release 連結。

## 編排依據

[awesome-readme](https://github.com/matiassingers/awesome-readme) 是優良 README 的範例集。本次採用其中反覆出現的清楚簡介、視覺說明、安裝與使用範例、架構及貢獻資訊，依這個專案的原生環境與核定流程調整；沒有引用不存在的 CI、下載數或正式發布紀錄。
