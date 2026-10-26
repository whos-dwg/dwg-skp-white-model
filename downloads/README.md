# 成對技能下載

- [下載 dwg-skp-skills-v0.2.1.zip](https://github.com/whos-dwg/dwg-skp-white-model/raw/refs/tags/v0.2.1/downloads/dwg-skp-skills-v0.2.1.zip)
- [SHA256SUMS.txt](SHA256SUMS.txt)

v0.2.1 修正 GitHub 下載後，同步技能因核心清單雜湊不同而無法安裝／啟動的問題。請使用同版本的兩個技能，解壓後讀取 INSTALL.md，先安裝 dwg-layered-skp，再安裝 dwg-skp-sync。

包內含兩個技能、共用 runtime、Ruby 橋接器、安裝工具及 MIT 授權。Python、Shapely 與原生依賴須依[README](../README.md#requirements)另行安裝；Release 提供本目錄下載連結。

```powershell
Get-FileHash ./dwg-skp-skills-v0.2.1.zip -Algorithm SHA256
```

歷史 v0.2.0 的封存與標籤保留；該版有已知的清單換行失配問題，請使用 v0.2.1 成對更新。
