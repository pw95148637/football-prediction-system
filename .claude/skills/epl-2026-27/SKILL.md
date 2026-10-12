---
name: epl-2026-27
description: 英超 2026-27 賽季決策支援操作手冊。循環賽,不延續世界盃的證偽實驗。當使用者說「英超」「鎖 <主隊> 對 <客隊>」「英超出報告」「英超這週有什麼要做」「settle <英超場次>」,或提到英超某場的分析、預測時使用。規則在 DECISION_PROTOCOL.md;設定檔與核心邏輯在 football_ai/competitions/epl_2026_27.py。
---

# 英超 2026-27 操作手冊

每場「現在該做什麼」由程式推導,不由本手冊或記憶決定:`python -m football_ai.competitions`(最急場次)、`python -m football_ai.competitions.matchflow`(每場的階段、死線、下一個合法動作)。本手冊說明各步為何如此、指令怎麼接,以及程式判斷不了的部分。現況與待辦見 `HANDOFF.md`;已封存的世界盃窗口見 `.claude/skills/wc2026/SKILL.md`,兩者的競賽設定、資料源與門檻各自獨立。

## 一週的兩個階段

價格由排程(GitHub Actions 的 `epl-matchday`)寫入,互動 session 不自行抓價。排程每 20 分鐘(UTC 10 至 19 時的 :07、:27、:47)檢查,只在價到期時呼叫 API:已鎖定場次的收盤價(開球前 45 至 10 分鐘),或 J 剛鎖定場次的入場價(100 小時內開球、尚無任何價)。一旦呼叫,該次為開球前 100 小時內、M 與 J 皆已鎖定的場次寫入價格,收盤窗口(開球前 150 分鐘)內則連沒有 J 的場次也寫。價格一寫入,該場的 J 永久不可得。細節見 `ops/README.md` §1。因此每場分兩段:

- **階段一(價格盲,死線前)**:鎖 M、蒐證、驗證、跑 J、推送。死線與「建議完成時刻」由 `matchflow` 給出,以建議時刻為準,留出排程晚啟動與重試的緩衝。M 與 J 須在同一個 session 完成:不鎖一個本 session 不打算補 J 的 M。
- **階段二(價格之後)**:報告、研判、寫入記錄、推送、`ops done`。看過價格後,對該場的 J 不再有效;這一段只做研判,不修訂 M 或 J。

## 階段一

動手前:`git pull --ff-only`(落後於 origin 時,鎖定、修訂與跑 J 都會被拒絕);每週首次另跑 `ops preflight`(`ops` 即 `python -m football_ai.competitions.ops`;`--no-probe` 略過探測 `claude -p` 的那一項)。`[FAIL]` 先處理。`[WARN] evidence sources` 表示網路政策擋下網頁擷取,蒐證者只能讀搜尋摘要,J 會停在 M(GW06 十場皆 0 因子):照常鎖 M、跑 J(J 薄弱仍有紀錄,沒有 J 則永久失去),但須在該輪產出中申報,並請 owner 在雲端環境的 Network access 加入列出的網域。`[??]` 是此環境無法確認的項目:`ODDS_API_KEY` secret 是否已設定須 owner 確認,使用者在本次提示中已說明者除外;未確認不鎖任何場次(沒有金鑰,收盤價抓不到,該場的 CLV 就缺;鎖定不可撤銷)。`matchflow` 若印出帳本「dirty / unpushed」的警示,先推送。

1. **鎖 M**:`ops lock --match-id <mid>`。隊名與開球時間取自賽程檔;沒有確認的 `kickoff_utc` 或沒有評分的隊名即拒絕(否則會以預設 Elo 1500 永久鎖定)。計算評分時會先嘗試連 football-data.co.uk,被網路白名單擋下時自動改用 git 內不含賠率的快照並印出警告,屬正常。越早越好:評分晚一輪的代價只是雜訊,晚鎖卻可能使場次落入抓價窗口而永久失去 J。已鎖定者不可重鎖;補 J 走 `amend_lock`,由 `ops run-j` 代為呼叫。
2. **蒐證(外派,價格盲)**:每場三支蒐證 agent:主隊、客隊、比賽層級(十個維度中 `rest_travel`、`referee`、`venue_conditions`、`incentive` 不屬於任一隊,只派兩支必使這四項沉默)。提示由程式產生:`ops evidence-brief --match-id <mid> --side home|away|match --scout-writes-to <repo 之外的絕對路徑>`:提示印到標準輸出,`--scout-writes-to` 是蒐證者被要求寫檔的位置(嵌入提示),不是提示的存放處。整份提示交給 agent,不手寫、不增刪。提示要求每次搜尋都封鎖博彩與預測站(`ops_tools.SCOUT_BLOCKED_DOMAINS`),並在證據檔旁另寫自報檔 `<檔名>.scout.json`(是否見過市場數字、能否開啟網頁、搜尋額度是否用盡)。agent 寫檔、只回傳路徑與結構化狀態。每批 8 至 12 支(`AGENT_DISPATCH.md` §2)。
3. **驗證並存證**:`ops evidence --match-id <mid> --claims <主隊檔> <客隊檔> <比賽檔> --store`。輸出 schema 錯誤、洩漏(只列第幾筆、哪個欄位、何種類型,不回顯原文)與十維度覆蓋。退出碼 0:可用;1(`REJECTED`):驗證未過,被標記的 claim 不得讀、不得進 J,退回蒐證者改寫後重驗,懷疑誤攔時依 `AGENT_DISPATCH.md` §1 要求最小重現;2(`INCOMPLETE`):驗證已過但有沉默維度,這個包仍可存證、仍可跑 J,不得為此扣住 J(死線過了就無法補),另派蒐證補缺口,補齊後用全部檔案重跑 `--store`;研判須等覆蓋齊全(或寫 `coverage_waiver`)。查無結果的維度須有附來源的負面發現。同一指令讀取各檔旁的 `.scout.json`,印出 `scout reports` 一行並隨證據存入 `scout_reports`:自報一律不擋 J(死線不可回復):缺檔列為 missing,欄位錯誤記為未知並列出,自由文字含任何數字即扣下不存,缺口照實入帳;`judgment-brief` 把它交給研判。證據包以內容雜湊去重存入 `evidence`;已先被定價而來不及跑 J 的場次,仍須存證,研判需要它。
4. **跑 J**:`ops run-j --match-id <mid> --claims <同上> [--k 3] [--j-model <模型>]`。依序檢查:已有價格即拒絕;已有有效 J 時,只有在新證據包對 J 當時所見為實質補強(tier-1a 至少加倍且加 3、總數不減)時可重跑一次,否則拒絕;證據驗證;補入 `goal_curve`;k 個樣本平行執行(單次逾時依提示長度推導,不寫死);算術閘必須執行;最後才 amend。J 的結果先落檔於系統暫存目錄,amend 被拒時不必重跑。此函式不是單一交易:證據在 J 之前已存入,後面的拒絕不會撤回它;輸出的 `evidence_stored: false` 是「相同內容已存過」(例如前一步已 `--store`),不是失敗。完成後看 `k_valid`:低於 2,排程視 J 為不存在,須在死線前重跑(舊版留在帳本,新版附加其後);k = 1 沒有離散度可言,而一旦定價就無法補強。蒐證在 J 之後大幅補強(例如網路恢復後重新蒐證)而尚未定價時,用補強後的全部檔案重跑一次 `run-j`。J 所用的模型不受限;實際作答的模型記入 `j.runner.models`;`ops models` 列出各層用過的模型與是否混用。
5. **推送**:`python -m football_ai.store.sync publish -m "<說明>"`。J 在推上 origin 之後才算數(排程在遠端讀 origin)。每完成一批就推,雲端容器結束時未推送的記錄會消失;衝突時它停手,不手動合併 JSONL。

## 階段二

`matchflow` 顯示該場為 `PRICED` 後:

1. **報告**:`report(mid, mechanism=..., abstention={"missing": ..., "flips_if": ..., "known_by": ...})`,取自 `football_ai.competitions.epl_2026_27`。沒填 `mechanism` 一律 PASS;PASS 或 WATCH 沒填 `abstention` 則印出 `UNFINISHED`。報告至多給 `WATCH`;判為 `BET_CANDIDATE` 須逐項核對九項檢核表後,呼叫 `epl_2026_27.evaluate_gate(gap_pp, mechanism, checklist={…九項…}, abstention=…)`,不用裸的 `sop.evaluate`(門檻是英超自己的)。僅 `BET_CANDIDATE` 時報告才印紙上單位(fixed 1.0u);實際注額以研判記錄為準。
2. **研判(每場都做)**:啟動條件只有一個,證據覆蓋無沉默維度(`judgment.record` 會檢查)。`ops judgment-brief --gameweek N` 組出 `judgment-synthesis` 的全部參數(證據落檔、M 與 J 的機率、市場讀數;尚不能判斷的場次與原因列在 `skipped`,不會默默省略)。呼叫 Workflow 工具須 owner 明示要求使用 workflow;沒有明示時,以 Agent 工具照同一架構做:`node ops/workflow_harness.mjs .claude/workflows/judgment-synthesis.js <brief> <responses.json> <outDir>` 由工作流程自身的程式產生每一步的提示,逐步派出 agent、把結果加進 responses.json 再跑一次,直到沒有待辦;入帳用 `python ops/assemble_judgment.py <brief> <最終研判> <meta> --blind <第一段研判> --write`(λ、f\*、EV、入場價都由專案程式算)。研判分兩段:第一段(綜合、查核、修訂)價格盲,第二段(`price:<mid>`)才看價,只補 f\*、機制與 abstention;機率只能因非價格的新資訊改動,寫在 `post_price_changes`,否則程式退回第一段的數字。查核深度由數字決定:研判與市場或已鎖定的 J 在任一結果上相差達 `depth.escalate_pp`(取 `CONFIG.gate.watch_pp`)、已命名機制(`verified`、`graded`)、無已鎖定的 J 或無市場讀數、或有 `live_divergence` 者,走三透鏡(算術重跑、證據溯源、已知偏誤與棄權舉證);其餘僅走「已知偏誤與棄權舉證」。每場的傾向判斷與入帳義務不因分級而減。`live_divergence` 由 `judgment-brief` 依帳本設定:沒有不依附 M 的估計(J 缺席,或已鎖定但 0 個因子、即 J = M)時為 true,加派兩支從零鑑識,與綜合者並行;鑑識不讀 M 與任何價格,從 xG 與陣容事實建立兩隊攻守指數,查不到的數據以同位置基準率估計、不歸零;修訂必須把它組合成第二個 1X2 估計並與研判並列裁決。研判與 J 另有待裁決的分歧時,也可由你改為 true 並填 `divergence_reason`。Workflow 的回傳是提案,不是記錄;subagent 不得寫入 store。
3. **寫入**:由主 session 經 `decision.judgment.record(rec, era=ERA)` 寫入,必填欄位與驗證見 `judgment.validate_record`(未通過即拒寫)。要點:`capabilities_used` 至少一項 J 結構上做不到的能力並載明實際作為;`divergence_note` 與 J 一致時也要填;`j_status` 為 `locked`、`foreclosed` 或 `absent`;`abstention` 不下注時三項俱全;`model` 與 `reviewer_models` 如實申報(未填存為 `undeclared` 並警告);`lambda`、`f_star`、`stake_frac` 有給即驗算乘法。λ 以 `decision.sizing.confidence_lambda` 計算(因子見 `STAKING.md` §2):本 era 機制分類帳為空時 `lambda_precedent` 取 0.5,是誠實的輸入,不得以世界盃命中率代入;進球曲線用英超自己的(`epl_2026_27.goal_curve()`),不是封存版。排序依 `judgment.rank`(λ·f\*),不依 EV 大小。證據覆蓋不全而來不及補時,以 `coverage_waiver=<理由>` 寫入且信心至多 `low`,缺口留在帳本。
4. **機制兌現判定(結算後)**:`ops materialise` 列出已結算、J 因子尚未判定的場次與每個因子在鎖定時寫下的檢核;逐項依檢核與賽後資料判定 `yes`/`no`/`opposite`/`undeterminable`(查不到資料就是 `undeterminable`,不得猜),寫成 `[{factor_index, verdict, basis}]`,再以 `ops materialise --match-id <mid> --verdicts <檔> --judged-by <判定者>` 寫入分類帳(主 session 寫;判定可交 agent,主 session 抽驗)。這是 λ_precedent 與 `mechanism_min_n` 唯一的資料來源。
5. **重跑報告**:研判給出的 `mechanism` 與 `abstention` 通常比第 1 步當下能填的更具體,取得後重跑一次 `report`,交付後者。
5. **推送並驗收**:`sync publish`;`ops done --gameweek N` 無 `FAIL` 且無 `WAIT`(`WAIT` 是尚在等價格的研判,代表這一輪還沒做完)。永久缺口顯示為 `LOSS`(從未鎖定、J 已被定價先落地、開球後才缺的證據或研判),不算未完成,但須在產出中講出來。交付 owner 的單頁摘要由 `ops summary --gameweek N` 產生(寫入 `reports/`,只讀帳本;結論以研判記錄為準,待處理不等於不下注)。

## 每輪的交付物

三類,不是一份下注清單;第二類每場都要有(章程 §4)。

1. **系統注**:通過兩問判定者,以 `decision/sizing.py` 定量(¼ Kelly、相關下注合併、單場至多 2 注),見 `STAKING.md`。可以是零筆;為零時列出最接近門檻的一筆、未達標的項目與達標所需的條件。
2. **傾向判斷**:方向、機率、信心,以及與已鎖定 J 的分歧說明,依 λ·f\* 排序。沒有這項,報告會退化成一串 PASS。
3. **市場是對的**:`|gap| ≥ 10pp`(取 `gap_J_pp`,沒有 J 用 `gap_M_pp`;門檻 `CONFIG.gate.bet_pp`)的場次逐一列出,附本 era 量得的先驗(見報告誠實區)。這是須給出機率、可用 CLV 檢驗、且入帳的承諾,不是空泛陳述。

深度由機械條件觸發:每場都跑 M、價格盲蒐證與 J(J 是子行程,不耗分析者的 token,沒有跳過任何一場的理由);每場都跑研判,條件只有證據覆蓋,其內的查核深度依階段二步驟 2 的規則分級。edge-hunt(`.claude/workflows/match-edge-hunt.js`,多個 agent)只在報告留下跨書口資料答不了的真實疑問、或 owner 指定時使用;基準率透鏡的參考類預設為同一聯賽的聯賽賽事,傳入 `repo_root` 與 `division: 'E0'` 時由專案的賽果資料庫實算。「我覺得這場值得深挖」不是觸發條件(自我把關的條件必然啟動不足);差距大小也不是:M 的差距越大越可能是 M 錯(實測只涵蓋 M,不涵蓋研判)。

## 限制

- `football-data.co.uk` 的 `fixtures.csv` 不是賽程表,是近一兩天的比賽日 feed,且每列帶賠率欄位;直接讀取會污染整輪的 J。賽程取自 `epl_2026_27_fixtures.py`。
- 兩問判定的差距門檻是回測得出的「值得查機制」門檻,不是「保證獲利」的門檻(記錄:`football_ai.audit.experiments.find("epl_gate_threshold_backtest_2026-07-24")`)。回答兩問是下注前的必要條件;差距超過門檻,不構成跳過的理由。
- 世界盃窗口的研判形狀(`judgment_qualify_home`、`stacking_score.py`、`judgment_ledger.jsonl`)與 `history/falsification-2026/` 的 `results.json`、`locks/`(只有國際賽)均不適用本競賽,不得沿用或作為對照。`judgment-synthesis.js` 的提示已載明封存資料只有國際賽,修改提示時不得移除。

## 結算與賽後

排程每次執行都從宣告的資料源結算(`ops settle` 同,不接受手打比分)。`settle_match(mid, h, a)` 是手打比分的後門,屬 `AGENT_DISPATCH.md` §7 不可逆的寫入,只在來源長期缺該場、且 owner 明確同意時使用。`ops review --match-id <mid>` 對 M、J、研判與市場逐一算 RPS 與 Brier:那是帳本條目,不是判決,單場 RPS 標準差約 0.1,偵測 0.01 的差異需約 400 場,三十場排不出名次,報告須明講。證據 claim 的 `absorption` 標記可驗證性最高,應優先處理(以 `market/consensus.movement()` 的開盤至收盤位移驗證)。

## 抓即時賠率

須先徵詢 owner。一次 `/odds` 回傳該聯賽所有近期場次的價格,原始回應若被印出,會當場污染那一輪每一場的 J,所以 `the_odds_api.fetch_odds()` 只落地、不列印。只走 `ops snapshot`(例:`ops snapshot --only-within-hours 6`,約 3 credits,只印 match id 與計數);自行組合 `fetch_odds`、`to_snapshots`、`store_io.append` 的路徑缺少「已鎖 M 而無 J 不得提早定價」這一道守衛。金鑰由 `ops.api_key()` 取得:本機讀 gitignored 的 `config.json`(不自行開啟),雲端讀環境變數 `ODDS_API_KEY`。查某場已寫入的價,從 store 讀那一場,不印整個 raw 檔;要場次 id 與隊名,用免費且不含賠率的 `api.fetch_events('soccer_epl', key)`。

## J 的規格

`research/j_runner.py` 不綁聯賽;spec 版本由 `CONFIG.j_spec_version` 決定(現為 `rr1`),不由呼叫端挑選。`j_spec/rr1.md` 自足,允許修正 M 的陣容陳舊(`mechanism_type: "squad_composition_change"`):判準只有一句,理由引用的是球隊的事實,還是市場的存在?前者允許(須點名球員、附實際產出、處理替補水準),後者絕對禁止。同目錄的 v1、v2、v2-rr 是封存的世界盃鏈,不是選項。

## 檔案地圖

- `football_ai/competitions/epl_2026_27.py`:`CONFIG`、`current_ratings`、`lock_match`、`amend_lock`、`report`、`evaluate_gate`、`settle_match`、`EVIDENCE_CATEGORIES`(俱樂部版);檔頭記錄每個數字的來源。
- `epl_2026_27_fixtures.py`:全季 380 場(`kickoff_utc` 只到 MW9)、`ROSTER`(資料源確切拼法的 20 隊)、`verify_team_names()`。更新方法見檔頭「HOW TO REFRESH」;隊名拼錯會被 `lock_match` 與 `ops lock` 拒絕;動賽程仍須跑 selfcheck。
- `competitions/`:`__init__.py`(`ACTIVE` 與 `paused`)、`matchflow.py`、`schedule.py`(死線算術)、`ops.py`(`snapshot`、`settle`、`review`、`run_j`、`models`)、`ops_tools.py`(`lock`、`evidence`、`evidence-brief`、`judgment-brief`、`done`、`preflight`、`summary`)、`summary.py`(單頁摘要的資料與版面)。
- `decision/`:`sop.py`、`report.py`、`judgment.py`、`sizing.py`。`evidence/firewall.py`:現役洩漏驗證器,新增誤攔類型加在此並於 `DIVERGENCES` 登記與封存版的差異,否則 selftest 失敗。

改動這些檔案後,兩套 selftest 都須通過:`python -m football_ai.audit.selftest` 與 `python history/falsification-2026/selftest.py`。
