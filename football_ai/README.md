# football_ai — 決策支援平台

系統的唯一主體。目的是有紀律的下注決策支援;證偽實驗是它的防自欺護欄與歷史,不是目的(章程 `../DECISION_PROTOCOL.md` §1)。

**資料的權威來源。** 本套件是主要來源。`../history/falsification-2026/` 是唯讀封存,為世界盃證偽實驗的完整鏡像,只供重現與查證。帳本(store)中 `era="v2-wc2026"` 的記錄是封存檔案的衍生視圖,可由 `data/import_v2.py` 重建,`audit/selftest.py` 設有逐位元重算的檢核;`era="v1-epl-2026-27"` 起的每個 era 都是原生主資料,不是任何來源的衍生視圖。

## 分層與依賴方向

```
store/        單一寫入路徑,僅追加的 JSONL(store/data/*.jsonl);sync:多機寫入的守衛與推送
core/         registry(id 對帳)、states(污染狀態機)、agenda(到期節點)、
              competition(多聯賽設定檔的抽象)、paths
data/         football_data_co_uk(賽果)、the_odds_api(賠率)、import_v2(封存匯入)
models/       m_dc(Elo 重播與 Dixon-Coles,含樣本外檢核)、division_elo(有升降班的
              單一級別 Elo 重播:新進球隊的進場分規則)
evidence/     firewall(市場洩漏驗證器)、schema(結構化 claim 與驗證)
market/       devig(Shin 法)、consensus(跨書口共識與走勢)、clv
research/     metrics、scoring、j_runner(J 的隔離多樣本執行)、j_spec/
decision/     sop(兩問判定)、sizing(相關下注合併、¼ Kelly 與信心係數 λ)、
              judgment(研判記錄)、report(決策報告)
competitions/ 每個競賽窗口一個設定檔;ops(快照、結算、復盤、跑 J,`ops matchday` 是排程任務);
              ops_tools(`ops` 的操作員指令:lock、evidence、evidence-brief、judgment-brief、done、preflight);
              matchflow(每場所處階段與下一個合法動作,由 store 推導);
              schedule(排程的死線算術,selftest 對照 workflow 檔的 cron)
evaluation/   trading_eval
audit/        experiments(實驗登記)、selftest
```

依賴方向:`store ← core ← {data, models, evidence} ← research`。已知例外:`data/backfill_phase2.py` 引入 `research.scoring`(回填封存資料的記分,僅匯入器使用)。`market` 可用 `core` 與 `store`。`decision` 與 `competitions` 可用全部。`evaluation` 讀 `store` 與 `market`,不碰 `research` 與 `decision`。`audit` 只被 `models/m_dc.py` 的 `main()` 以可選方式引入(登記實驗),其餘不引入。

`audit/selftest.py` 機械檢查 `research`、`market`、`evidence`、`models`、`core`、`evaluation`、`store` 的禁止引入;`data`、`audit`、`competitions`、`decision` 沒有此類規則。

## 價格盲

J 的輸入須價格盲:`j_runner.assemble_prompt` 只組合 M、證據包與凍結的規格三項。

`research/` 整體並非價格盲(`scoring.py` 為記分而讀取賠率,並使用 `market.devig`),但不得引入 `market.consensus` 或 `decision.*`;selftest 以 `ast` 解析檢查,涵蓋 `from football_ai.market import consensus` 這類寫法。

僅檢查引入並不足夠,資料本身也可能夾帶市場數字:`football_data_co_uk` 的記錄附有 Pinnacle 收盤價,因此收盤價為 opt-in(`with_closing_odds=True`)。新增資料來源時,價格盲的檢查須同時涵蓋引入方向與資料內容。

## 慣例

- 一律從 repo 根目錄執行,例如 `python -m football_ai.audit.selftest`。相依套件見 `../requirements.txt`。
- 帳本記錄為每行一個 JSON:`{"era", "kind", "ts", ...額外欄位, "data": {...}}`;額外欄位依 kind 而異,常見的有頂層的 `match_id` 與 `version`(鎖定的修訂序號)。歷史位元組永不修改。
- 每個模組自帶 `_selfcheck()`,採植入錯誤(planted-fault)的測試取向:驗證工程誠實(該攔的有攔、不變式成立),不指向預測績效。新模組須加入 `audit/selftest.py` 的清單。
- 改動任何程式後,兩套 selftest 都須通過:`python -m football_ai.audit.selftest` 與 `python history/falsification-2026/selftest.py`。封存的那套須繼續獨立通過。

## 新增競賽窗口

1. `competitions/<name>.py`:一個 `CompetitionConfig` 與操作函式。
2. 賽程檔。
3. 登記進 `competitions/__init__.py` 的 `ACTIVE`。條目須帶可呼叫的 `amend_lock`、`goal_curve`、`lock_match` 與 `current_ratings`(selftest 強制)及 `upcoming_fixtures`;可選 `paused`(暫停標記)。登記後,session 開場的到期節點會自動掃到它。
4. `.claude/skills/<name>/` 操作手冊。

`ops` 的預設參數(`--competition epl-2026-27`、`--sport-key soccer_epl`)與 `settle` 只認 `football_data_co_uk:` 來源,新增聯賽時一併處理。`decision.report` 的命令列只適用封存的世界盃。

## 容易誤解之處

- `evidence/firewall.py` 是現役程式碼,不是封存品。封存目錄中有同名檔案,兩者由 `DIVERGENCES` 等價閘綁住:任何行為差異須明文登記,否則 selftest 失敗。
- `CompetitionConfig.known_biases` 由每個窗口自行量測,預設為空。世界盃的偏誤只在其 16 場淘汰賽樣本上量得,不得套用至其他聯賽。
- 一次 `/odds` 呼叫回傳整輪價格,`the_odds_api.fetch_odds` 因此只落地原始回應並回傳物件,永不列印;由呼叫端決定顯示哪一場。
- `ops.snapshot()` 拒絕為「已鎖定 M 但尚無可報告離散度的 J」(`k_valid` 低於 2 視同沒有)的場次提早寫入市場價,因為寫入後該場的 J 即永久不可得(`J_FORECLOSED`)。收盤窗口是刻意的例外:CLV 需要收盤價,而屆時 J 已無意義。
