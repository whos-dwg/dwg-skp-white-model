# 成對技能下載

- [下載 dwg-skp-skills-v0.2.0.zip](https://github.com/whos-dwg/dwg-skp-white-model/raw/refs/tags/v0.2.0/downloads/dwg-skp-skills-v0.2.0.zip)
- [SHA256SUMS.txt](SHA256SUMS.txt)

技能包包含 `dwg-layered-skp`、`dwg-skp-sync`、共用 runtime、Ruby 橋接器、安裝工具與 MIT 授權。解壓後先讀 `INSTALL.md`，保持兩個技能同層並先安裝核心，再安裝同步入口。

Python、Shapely、AutoCAD、.NET 與 SketchUp 原生依賴仍須另外安裝；完整需求見 [README](../README.md#requirements)。這是 `v0.2.0` 預覽版的倉庫下載檔，沒有透過 Release 附件提供。

PowerShell 可計算下載檔的 SHA-256，與校驗檔比對：

```powershell
Get-FileHash ./dwg-skp-skills-v0.2.0.zip -Algorithm SHA256
```
