# 即時同步已移至獨立技能

目前開啟且來源綁定的 SketchUp 白模局部更新，使用 `$dwg-skp-sync`（同步更新）。只讀取 [同步更新入口](../../dwg-skp-sync/SKILL.md) 與其所需參考；不載入初次建模流程。

四種既有動作、live CAD 修訂、整組牆系及單次 Undo 的完整操作，統一維護於 [同步工作流程](../../dwg-skp-sync/references/live-repair.md)。舊 `dwg_to_skp.py live-*` 命令保留相容轉接，與新入口共用同一核心，並非另一套引擎。
