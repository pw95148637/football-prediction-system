---
name: wc2026
description: 世界盃 2026(已封存、唯讀)的重現與查證:重跑封存場次的決策報告(`python -m football_ai.decision.report <世界盃場次 id>`)、重算封存數字、查證偽實驗結論(B1 未被證偽、不構成證明,2026-07-20 結算)。只在使用者明確提到世界盃/WC2026 歷史場次或封存數字時使用;英超與任何現行競賽走各自的 skill(`epl-2026-27`),本 skill 不適用。
---

# WC2026 封存窗口:重現與查證

本檔只涵蓋已封存的世界盃 2026 窗口。現行競賽(英超 2026-27)的操作手冊是 `.claude/skills/epl-2026-27/SKILL.md`;系統章程是 `DECISION_PROTOCOL.md`(權限分工見其 §9)。封存目錄 `history/falsification-2026/` 唯讀:不編輯、不新增、不刪除,**不得在其下新增鎖定檔**。

## 結論(2026-07-20 結算,全文見 `history/falsification-2026/FINAL_REPORT.md`)

命題 B1(LLM 資訊合成 + 公開資料 + 可執行模型,機率品質不輸 Pinnacle 收盤)**未被證偽**:M 平均 RPS 0.1621 vs Pinnacle 收盤 0.1972,15 場中 11 場較佳。登記規則的讀法是「未證偽,但不構成證明」,N=16 的檢定力只能偵測大差距,不是「LLM 贏了市場」。J(n=13)平均 RPS 0.1742,優勢幾乎全來自 M。B2 無資料(paper bet 4 筆,低於登記門檻 5 筆)。

## 重跑封存場次的決策報告

```bash
python -m football_ai.decision.report <世界盃場次 id> [--mechanism "市場錯在哪"]   # 如 m104_esp_arg
```

此 CLI 的 `main()` 呼叫 `build()` 時不帶競賽設定,只讀 `history/falsification-2026/locks/` 與 `odds/`,因此**只能處理該目錄 `locks/` 已有鎖定檔的世界盃場次 id,不適用於英超或任何現行競賽的場次 id**(它們的鎖定在 store,走 `epl-2026-27` skill 的流程)。重跑是重現,不是重新下注。

## 重算封存數字

封存腳本以自身所在目錄為資料根,`score.py` 會把 `report.json` 寫回該目錄(`mechanism_score.py` 讀它,須在 `score.py` 之後執行),在原位執行即是改動唯讀的封存。因此除封存自測外,一律先複製到暫存目錄再執行:

```bash
python history/falsification-2026/selftest.py      # 封存護欄自測,唯讀,可原地執行;須獨立於平台持續通過
W=$(mktemp -d) && cp -r history/falsification-2026 "$W/archive"
python "$W/archive/score.py"                       # B1/B2、錨定稽核、CLV 重新記分
python "$W/archive/mechanism_score.py"
python "$W/archive/ledger_audit.py"                # 敘事與帳本一致性
```

這些指令只用於查證歷史數字,不產生新判定。改動平台程式後另須跑 `python -m football_ai.audit.selftest`。

## 其他文件

- 當年每場流程(SOP v4,Phase 1-4)與硬規則:`history/falsification-2026/PROTOCOL.md` §3。
- 逐場判定敘事:`history/falsification-2026/verdicts_main.md` 只讀該場小節(全檔極大)。
- 機制假說的歷史證據:`history/falsification-2026/pilot_verdicts.md`「J 機制清單與戒律」節。
- 世界盃排程任務(31 個,已全數註銷)的清單:`history/falsification-2026/SCHEDULED_TASKS_ARCHIVE.md`;現行排程見 `ops/README.md`。新增、修改或移除排程屬 `AGENT_DISPATCH.md` §7 須先徵詢 owner 的動作。
- 世界盃窗口的研判與完整比較表:研判流程已由 `.claude/workflows/judgment-synthesis.js` 取代;比較表見 `.claude/skills/wc2026-comparison-table/SKILL.md`(僅適用世界盃封存窗口)。
