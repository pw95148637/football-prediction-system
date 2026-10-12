# ops — 無人值守作業與雲端運作

系統不依賴任何人的電腦:排程任務跑在 GitHub Actions,互動分析跑在 Claude Code 雲端 session,兩者靠 git 上的 `main` 交換帳本。本檔是這套運作的唯一說明。

## 1. 排程任務

`python -m football_ai.competitions.ops matchday` 依序執行 `snapshot --when-due --only-within-hours 100` 與 `settle`。旗標與理由定義在 `competitions/ops.py`,有測試;任何 runner 都只呼叫這一條指令。

**呼叫 API 的閘門:到期才叫(owner 2026-10-10 核准)。** `snapshot --when-due` 先以純函式 `ops.fetch_due` 判斷,只在下列任一條件成立時呼叫 odds API;都不成立時不呼叫、不花額度、不需要金鑰。

- **收盤到期**:有已鎖定 M 的場次在 [現在+10, 現在+45] 分鐘內開球,且尚無 `fetched_at` 不早於開球前 60 分鐘的 closing 1x2 快照。
- **入場到期**:有 M 與 J 皆已鎖定(J 的有效樣本數至少 2)的場次在 100 小時內開球,且尚無任何 odds 快照。J 是否可用,閘門與 `snapshot` 的延後守衛共用同一個判定(`ops._j_ready`);兩者若不一致,閘門會為 `snapshot` 不肯定價的場次每輪呼叫。
- **入場不重試**:J 推上 `main` 之後(以 J 的鎖定時間加 60 分鐘計)若已有一次呼叫而仍未寫入該場,表示 API 回應裡沒有它(隊名對應失敗、延賽、尚未開盤),不再每輪重叫,否則直到開球前每 20 分鐘都會花一次額度;該場的收盤呼叫照常。

門檻常數在 `competitions/schedule.py`(`CLOSING_DUE_MIN`、`CLOSING_FRESH_MIN`、`ENTRY_HORIZON_HOURS`、`ENTRY_PUSH_SLACK_MIN`)。

呼叫之後的寫價規則不變:

- 該次為開球前 100 小時內、M 與 J 皆已鎖定的**所有**場次寫入市場價,不只是到期的那一場;收盤窗口(開球前 150 分鐘)內則連沒有 J 的場次也寫,並標為 closing。
- `--only-within-hours` 與 `snapshot()` 自身的守衛(沒有可報告離散度的 J 的場次,收盤窗口以外一律延後;未鎖定的場次拒絕寫價),使任務不會為數日後的場次寫入市場價,而讓其 J 永久不可得。
- 價格何時落地,即各場階段一(價格盲)何時結束。死線的算術在 `competitions/schedule.py`,每場的死線由 `matchflow` 給出,取收盤窗口內的第一個排程時點。這是保守值:那一刻是否真的呼叫,取決於有沒有任何場次到期,但只要呼叫,就會為窗口內所有已鎖定場次定價。
- 到期的場次在呼叫後仍未取得應有的價(收盤到期者沒有 closing 1x2,入場到期者沒有任何一筆),`snapshot` 寫完其餘的價後以 `due but not priced` 失敗,GitHub 寄信給 owner。

其餘:

- 任務從不列印價格,只印 match id 與計數,log 可以安全閱讀。
- `settle` 每次都執行(不花額度),只從競賽宣告的賽果來源讀分數。排程最後一次在 19:47 UTC,晚場的賽果於隔日 10:07 起結算(結算沒有期限)。來源回 404,該場留在 pending;來源回 5xx 或逾時,改用快取或 git 內不含賠率的快照,並在 stderr 警告,不使整輪失敗。
- 一步失敗不阻止另一步,但整體以非零結束碼結束,排程器因此通報;已寫入的帳本仍會推送。
- 競賽的暫停標記(`competitions/__init__.py` 的 `paused`)只影響提醒,不停止此任務:沒有價到期時空轉,有已鎖定場次時仍會寫入收盤價並結算。
- 收盤價的時點:每個標準開球時段的收盤到期窗口(開球前 45 至 10 分鐘)內有兩個排程時點,收盤價因此落在開球前 10 至 45 分鐘;60 分鐘的新鮮度門檻使第二個時點不重抓。GitHub 的 cron 是盡力而為:2026-10-06 至 10 的每小時排程實際一天只跑約 3 次,最晚延遲近 3 小時。兩個時點都被延後或丟棄時,該場沒有收盤價,CLV 就缺,事後無法補回;窗口內可手動觸發補抓(見 §2,閘門照樣判斷,沒有到期就不呼叫)。每週應查看 Actions 的執行紀錄有無失敗。

## 2. 現行 runner:GitHub Actions `epl-matchday`

定義於 `.github/workflows/epl-matchday.yml`。cron 為 `7,27,47 10-19 * * *`:UTC 10:00–19:59 每小時的 :07、:27、:47,一天 30 次,避開 GitHub 負載最重的整點。英超開球在 11:30–20:00 UTC(英國當地 12:30–20:00,跨夏令與冬令時間);每個標準開球時段在收盤到期窗口內有兩個時點,一次延遲或丟棄不致漏掉收盤價。開球晚於 20:32 UTC 的場次不會有收盤到期的時點。`schedule._selfcheck` 逐一檢查標準時段與已登錄賽程的每個開球時間,並要求 workflow 的 cron 與 `schedule.TICK_MINUTES`、`TICK_HOURS_UTC` 一致。

**Actions 分鐘數。** repo 為 private,每個 job 以整分鐘計費。一次執行約 20–35 秒(2026-10-06 至 10 實測 20–64 秒),計為 1 分鐘。排程上限為 31 天 × 30 次 = 930 次,約 930 分鐘;另留 50 分鐘給手動觸發與偶爾超過 1 分鐘的執行,合計在 owner 定的每月 1000 分鐘以內(`schedule.ACTIONS_MINUTES_CAP`,selfcheck 檢查,調高頻率會失敗)。GitHub 實際執行的次數通常少於排程。

也可手動觸發(閘門照樣判斷,排程漏跑時用來補抓收盤或入場價):

```bash
gh workflow run epl-matchday.yml
gh run list --workflow=epl-matchday.yml --limit 5
```

手動觸發時可勾選 `verify_push`,以 `git push --dry-run` 確認 workflow 的 token 能推 `main`,不寫入任何東西。

- **金鑰。** `ODDS_API_KEY` 存放於 repo 的 Actions secret,只有收盤價或入場價到期、真的呼叫 API 時才會被讀取。須由 owner 自行設定,金鑰不經過任何 agent。已有 `history/falsification-2026/config.json` 時,從 repo 根目錄執行(PowerShell 與 bash 皆適用):

  ```bash
  python -c "import json,subprocess; k=json.load(open('history/falsification-2026/config.json'))['odds_api_key']; subprocess.run(['gh','secret','set','ODDS_API_KEY','--repo','<owner>/<repo>'],input=k.strip().encode(),check=True)"
  gh secret list --repo <owner>/<repo>
  ```

  Python 直接把金鑰交給 `gh`,不經 shell 管線,金鑰不會出現在畫面或指令歷史;第二行只列出 secret 的名稱,用來確認已設定。沒有該檔時,改用 `gh secret set ODDS_API_KEY --repo <owner>/<repo>` 互動輸入,或於 repo 的 Settings → Secrets and variables → Actions 新增。

  沒有這個 secret 時,任務空轉仍成功;一旦有價到期,就會因缺金鑰而失敗並通報。恢復跑比賽週之前須先設好。
- **步驟與失敗行為。** 依序為:`ops matchday`(寫入市場價、結算)、更新賽果快照、推送帳本(`sync publish`,`if: always()`,前面失敗也會推送已寫入的部分)、保留原始回應、最後在 `matchday` 失敗時使整個 run 失敗,GitHub 寄信給 owner。更新賽果快照的步驟為 `continue-on-error`,失敗不使 run 失敗、也不寄信,只在 log 留下痕跡,雲端 session 計算 M 時便沿用較舊的快照;因此每週查看 Actions 紀錄時,要看這一步,不只看 run 的整體狀態。改動 workflow 屬須先徵詢 owner 的動作。
- **原始賠率回應。** 因過大而被 gitignore,每次 run 以 artifact 保留 90 天;帳本記錄的 `raw=` 所指檔案超過 90 天即不可取得,這是已知的保證弱化。
- **不含賠率的賽果快照。** 每次 run 順便更新 `football_ai/data/results_snapshot/`(只含日期、隊名、比分,僅已賽場次),使雲端 session 不連 football-data 也能計算 M。

**排程寫入者唯一。** 本機 Windows 排程 `wc2026-epl-matchday` 已停用;`ops/epl_matchday.cmd` 與 `ops/run_hidden.vbs` 留作 Actions 故障時的備援。兩者不得同時啟用,否則兩台機器會對同一份帳本追加價格。換手時先停舊的、再啟新的;改動排程須先徵詢 owner(`AGENT_DISPATCH.md` §7)。

## 3. 多處寫入同一份帳本

`store/io.py` 的「單一寫入者」指單一程式路徑,不是單一機器。帳本在 git 裡,各檔的寫入者如下:

| 寫入者 | 檔案 |
|---|---|
| 排程任務 | `odds`、`results`、`results_snapshot/` |
| 互動 session | `locks`、`judgments`、`evidence`、`mechanisms`、`bets`、`real_bets`、`experiments`、`calibrations`(證據包由 `ops evidence --store` 與 `ops.run_j` 寫入) |
| 僅匯入器 | `fixtures`、`verdict_backfill`(回填封存的世界盃資料,現行窗口不寫) |

不同檔案在 rebase 時不會衝突。程式強制的部分(`store/sync.py`):

- `lock_match`、`amend_lock`、`ops.run_j` 執行前會 `git fetch`,確認這份 checkout 沒有落後於 origin 上的帳本變更;落後或連不上 origin 一律拒絕。原因:污染守衛讀的是本機的 odds,任務在遠端寫入了市場價而本機沒有 pull,守衛看不到價,J 就會被寫入並永久污染。
- 寫完帳本後以 `python -m football_ai.store.sync publish -m "<說明>"` 推送:commit、rebase、push 到 `main`;在 detached HEAD 上也能使用。HEAD 夾帶帳本以外的未推送變更時,它拒絕並點名檔案。同一個檔案被兩邊追加而衝突時,它中止 rebase、保留本機提交並停手;不得手動合併 JSONL,須先查明是哪個檔案、誰寫入的。未推送的記錄在雲端容器結束時就沒了。

## 4. 雲端互動 session(Claude Code on the web)

環境為 Linux 沙箱,repo 位於 `/home/user/wc2026`,以 `CLAUDE_CODE_REMOTE=true` 標示。各項能力與限制:

| 項目 | 說明 |
|---|---|
| 推送 `main` | 可以直接推。刪除遠端分支會被 git 代理以 403 拒絕。checkout 可能在 `main` 或 detached HEAD,`sync publish` 兩者皆支援 |
| 相依套件 | `.claude/settings.json` 的 SessionStart hook 呼叫 `ops/ensure_deps.py`,為該 session 實際使用的 Python 安裝 `requirements.txt`,新 session 一開始就能引入 |
| 賽果與 M | 預設網路白名單封鎖 `football-data.co.uk`,M 改用 git 內不含賠率的快照計算,結果與本機逐位相同 |
| 同步守衛 | `sync check` 能 fetch origin,回 `{'checked': True}` |
| 就緒檢查 | `ops preflight` 以 `gh` 讀 Actions 的執行紀錄(可用),並探測隔離的 `claude -p`(`--no-probe` 略過),以及蒐證者能否開啟網頁(`evidence sources`,探測 fbref、understat、premierleague、bbc、skysports;被網路政策擋下即 `[WARN]`,不阻止鎖定但須申報)。Actions 的 secrets 名單被代理封鎖,`ODDS_API_KEY` 是否已設無法由此確認,須 owner 確認 |
| J(`claude -p`) | 可用。沙箱設有 `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`,會把專案的 CLAUDE.md 注入任何 `claude` 子行程;直接在 repo 內執行,模型會引用專案脈絡。經 `cli_llm` 執行時,`j_runner.isolated_env()` 剔除該變數並以暫存目錄為 cwd,因此 J 不受影響 |
| 其他被封鎖的網域 | 預設封鎖 `api.the-odds-api.com`、`www.premierleague.com`、`www.espn.com`,2026-10-07 實測亦封鎖 fbref、understat、bbc、skysports、transfermarkt、wikipedia 等幾乎所有網頁擷取(WebFetch 與 curl 皆然),蒐證者只剩搜尋摘要,GW06 的 J 因此十場皆停在 M。蒐證所需網域須由 owner 在 claude.ai 的環境設定(Network access)加入,`ops preflight` 的 `evidence sources` 會檢查。賠率由 Actions 抓取(其網路完整)。只有想在互動 session 內手動執行 `ops snapshot` 時,才需在 claude.ai 的環境設定加入 `api.the-odds-api.com` 並設定環境變數 `ODDS_API_KEY` |

新的雲端 session 的第一個動作,見 `CLAUDE.md`。

## 5. 備援與歷史

- 世界盃的排程記錄於 `history/falsification-2026/SCHEDULED_TASKS_ARCHIVE.md`(31 個任務,已全數註銷)。
- 搬遷前的執行器:`history/falsification-2026/ops/hidden_run.vbs`、`run_grab.cmd`(唯讀);現役備援為本目錄的 `run_hidden.vbs`。
- `ops/*.log` 被 gitignore;Actions 的 log 在 GitHub 上。
