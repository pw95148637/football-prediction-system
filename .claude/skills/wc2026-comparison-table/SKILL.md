---
name: wc2026-comparison-table
description: 世界盃封存窗口的完整比較表(已封存):把 M/J/研判/市場整合成一份給使用者看的 HTML,僅適用世界盃 2026 淘汰賽窗口,英超不得沿用(須先把 Step 1/3 改寫為 store 讀取)。使用者要世界盃封存場次的「完整比較表」「比較表」「comparison table」時使用。CSS/排版範本抄 `reports/m104_final_comparison.html` 或 `reports/m103_final_comparison.html`(兩份都含 2026-07-17 新增的「建議下注」區塊,見 Step 2b);較早的 m101/m102 版本(`reports/final_four_comparison.html`)只當方法論的歷史參考,版面已過時不要照抄。
---

# WC2026 完整比較表生成

**已封存(2026-10-06):僅適用世界盃 2026 淘汰賽窗口(依賴已凍結的 judgment_ledger 與封存目錄);英超不得沿用。** 若要英超版,須改寫 Step 1/3 為 store 讀取,並以 `reports/epl2627_gw02_tot_new_comparison.html` 為版型起點。

**定位**:研判跑完之後的**選用**最後一步,預設問使用者要不要做(預設答案是「要」)。純決策支援輸出,不回寫 M/J、不入 B1/B2。輸入是研判已算出的結果,這裡不重新做鑑識判斷,只做「整合 + 算全部市場 + 事實查核 + 排版」。

## 前置條件

研判必須先完成:`history/falsification-2026/judgment_ledger.jsonl` 有該場一筆記錄(`kind` 不是 `resolution`),且知道研判用的攻/守乘數(通常在 `reports/<match_id>_final_judgment.md` 或研判當時的對抗查核 agent 輸出裡)。沒有研判就沒有比較表的生成依據——這一步是把研判結果做成看板,不是重跑研判。

## Step 1 — 用真實函式算六市場(親自做,不假手 LLM 心算)

跟研判本身同一個紀律:機率不能靠模型心算,要真的呼叫專案自己的函式。

```python
import sys; sys.path.insert(0,'history/falsification-2026')
from model import predict_lambdas, qualify_prob
from football_ai.models.m_dc import score_matrix
from football_ai.decision.report import _grid_1x2, _grid_totals, _grid_ah_home, _top_scores, dc_rho
from football_ai.decision.sizing import ev as ev_of
from football_ai.market.devig import devig_shin, devig_both

base_h, base_a = predict_lambdas(<elo差>)          # 純戰績評分基準 λ,跟鎖定的 M 同一組數字
lam_h = base_h * <研判home攻擊乘數> * <研判away防守乘數>
lam_a = base_a * <研判away攻擊乘數> * <研判home防守乘數>
rho = dc_rho()
grid = score_matrix(lam_h, lam_a, rho)

home, draw, away = _grid_1x2(grid)                  # 勝平負
over, under = _grid_totals(grid, <line>)            # 大小球(quarter line 自動拆兩半平均)
win, lose = _grid_ah_home(grid, <line>)             # 讓分
top6 = _top_scores(grid, 6)                         # 正確比分前6
qual = qualify_prob(lam_h, lam_a, pk_h=<沿用J的pk_h,除非研判有自己查證的理由改>)
```

**λ 的乘法規則**(m104 驗證過,不要重新發明):`λ_該隊 = 純戰績評分基準λ × 該隊自己的攻擊乘數 × 對手的防守乘數`。防守乘數 >1 = 該隊防守比中性差(對手進球期望被推高),<1 = 防守比中性好(壓低對手)。

**真實賠率一律讀原始檔,不得手動輸入**:
```bash
cat history/falsification-2026/odds/<match_id>.jsonl          # Odds API 已抓的快照(1x2/totals/ah)
```
qualify(晉級/奪冠)、BTTS、隊伍分盤、大小球其他線位如果 Odds API 沒有,用使用者截圖(見下方 Step 4);不要用網路搜尋彙整的查價當正式 EV 輸入——這類數字精度不足以算 EV,只能標「非機械抓取」放旁邊當參考。

## Step 2 — 排版與文字規範(強制,不是品味建議)

以下為**硬性規則**,完成後逐項對照檢查,不合格視為未完工。

### 2a. 視覺系統(照抄,不要重新設計)

直接複製 `reports/m104_final_comparison.html` 的整個 `<style>` 區塊(不是 `final_four_comparison.html`——那份是 m101/m102 時期的舊版面,只當方法論參考,CSS 不要抄;`m103_final_comparison.html` 也是同一版 CSS,兩份互為對照)。這份 CSS 的分類色/狀態色(`--info`/`--accent`/`--good`/`--risk`,淺色與深色模式各一組)已經用外部 dataviz skill(不在本 repo)的調色盤驗證器驗證過**兩種模式都過關**(亮度帶、彩度下限、色盲可辨識度、對比度全通過)——**不要臨時改色號**;一定要改時,須以同類驗證器對兩種模式重新驗證,本 repo 沒有該驗證器。
（深色模式現行值:`--info:#5a9ad4 --accent:#b8792a --good:#35a878 --risk:#c85f47`,深色表面 `#14171c`,已通過該驗證。**驗證器只測「文字色 vs 背景」這種用法,不測「配白字的底色」用法**——`.seg.home`/`.seg.draw`/`.seg.away`(機率長條裡的白字色塊)若直接拿 `--info`/`--accent`/`--ink-400` 當底色配粗體白字,對比度會低於 WCAG AA 的 4.5:1 門檻:這三個變數是為「文字」調的,不是為「配白字的底色」調的,兩種用途混用會過驗證但實際看不清楚。修法:另定義 `--info-fill`/`--accent-fill`/`--ink-fill` 三個「配白字底色」專用變數,兩套主題各自加深到 ≥4.5:1(淺色:`--info-fill:#2e74b0`、`--accent-fill:#8a5e1f`、`--ink-fill:#5f6674`;深色:`--info-fill:#2f6aa3`、`--accent-fill:#8f5c1f`、`--ink-fill:#5c636e`)。`.seg.*` 的 `background` 一律用 `-fill` 版本,原本的 `--info`/`--accent`/`--ink-400` 只用於純文字。）

**顏色語意(不要挪作他用)**:
| 變數 | 語意 | 用在哪 |
|---|---|---|
| `--info` / `--info-fill` | 主隊/西班牙式的隊伍識別色(前者純文字用,後者配白字的底色專用,見上方對比度說明) | `--info`:大數字;`--info-fill`:`.seg.home` 機率條底色 |
| `--accent` / `--accent-fill` | 客隊識別色 / 研判專屬強調色 / 全站主題色,是刻意的固定 home/away 色槽慣例、不是誤用(前者純文字,後者配白字底色) | `--accent`:`.hero` 頂邊、`section-head` 編號框;`--accent-fill`:`.seg.away` |
| `--ink-400` / `--ink-fill` | 和局/中性/次要文字(前者純文字,後者配白字底色) | `--ink-400`:次要說明文字;`--ink-fill`:`.seg.draw` |
| `--good`(`-ink`) | 正向/划算/高信心 | `.ev-pos`、`.conf b.hi`、`.pill.go` |
| `--risk`(`-ink`) | 負向/不划算/低信心/警告 | `.ev-neg`、`.conf b.lo`、`.note.warn` |

**元件庫(照這個對照表選,不要發明新樣式)**:
| 元件 | 用途 |
|---|---|
| `.masthead` | 頁首,標題+右對齊 meta 資訊 |
| `.hero`(`.hero-top`/`.hero-nums`/`.hero-num`/`.hero-line`/`.conf-row`/`.conf`) | 結論區塊,永遠放第一個 section |
| `.action`(`.action-list`/`.action-row`/`.action-row-head`/`.action-row-name`/`.action-row-amt`/`.action-row-reason`/`.pill.go`/`.pill.wait`/`.pill.watch`/`.pill.no`/`.action-total`) | **建議下注區塊,永遠放第二個 section(緊接在 hero 之後)**,見下方 2b 第 1 點。採 flex 卡片式(`.action-row`),不用 `<table>`——四欄表格在 380px 手機寬度會擠爆理由欄,卡片式在任何寬度都不會擠 |
| `.stack-wrap`/`.stack-row`/`.stack`(`.seg`) | M/J/研判/市場的機率長條比較,`.seg.*` 底色記得用 `-fill` 版本(見上方顏色語意) |
| `.table-wrap` + `table.profiles` | 球隊逐場數據表。**不得加 `min-width`**——`min-width:560px` 會使表格在 380px 手機寬度擠出 `.table-wrap` 外變成側滑,且無提示,讀者無法察覺資料被裁切。窄寬度改用 media query 縮 padding/字級(參考 `table.markets` 完全不設 `min-width` 的寫法) |
| `.forensic-grid` + `.forensic-card`(兩層 div,見下方陷阱) | 兩隊並排卡片,760px 以下自動變單欄 |
| `.mult-table` | 乘數對照表,`.strike` 標被推翻的舊值、`.final` 標最終值 |
| `table.markets`(`.grp` 分組列、`.ev-pos`/`.ev-neg`) | 市場划算度總表 |
| `.note` / `.note.warn` | 說明框(一般用 accent 左框,警告/修正用 risk 左框) |
| `footer` | 方法論/來源,小字灰階 |

**已知 CSS 陷阱**:
- `.card` 與需要 flex 排版的內容類(如 `.forensic-card`)不可疊用同一個 class 在同一個 div 上——部分渲染引擎下背景框不會隨 flex 內容撐開,並排卡片其中一張的內容會溢出卡片外。正確寫法為兩層:`<div class="card"><div class="forensic-card">...</div></div>`。
- **窄螢幕(≤400px)是硬指標,不是可選檢查**:任何表格要嘛在 ≤400px 完全可讀(靠拿掉 `min-width`、縮 padding/字級、或改卡片式),要嘛給明顯的側滑提示——絕不能讓資料在讀者不知情的狀況下被裁掉。做完排版一律拿 380px 寬度檢查一次(截圖或瀏覽器裝置模擬都可以)。
- **`.pill` 目前是 4 種固定狀態,不要臨時發明第 5 種文字塞進 `.pill.no`**:`go`(下注)/`wait`(保留待觸發)/`no`(不下)/`watch`(觀察,虛線外框、無底色,用於「有紀錄但未達下注門檻」的候選,例如 edge hunt 的 MARGINAL 倖存者)。標籤固定用 2-5 個字的短詞,不要寫成一句話塞進 pill 裡——那樣會失去「掃色塊不用讀字」的作用,理由留給 `.action-row-reason` 講。

### 2b. 文字規範(通篇適用,不是改一兩句)

1. **結論放最前面,大字**:`.hero` 區塊放最上方,大字顯示單一最佳點估計(3個百分比或核心數字)+ 一句話理由 + 高信心/低信心兩個標籤(聚合層級判斷給高信心、綁定具體機制的判斷給低信心)。不要讓讀者從表格自己拼結論。
1b. **「建議下注」區塊是強制的,不是選配**——划算度數字再多,若沒有明確結論收尾,仍視為沒做完。緊接在 hero 之後放第二個 section,`.card.action`(CSS 見上方元件庫的 `.action` 列;卡片式 markup,不是 `<table>`),用假設本金 1000 的一組卡片列出**每個有討論價值的部位**:
   - 每個 `.action-row`:部位名(`.action-row-name`,可帶 `.badge` 附註賠率/來源)+ 建議 pill(`.pill.go`「下注」/`.pill.wait`「保留待觸發」/`.pill.watch`「觀察」/`.pill.no`「不下」)+ 金額(`.action-row-amt`,右對齊大字)+ 理由(`.action-row-reason`,1-2 句,講清楚為什麼,不是重複划算度數字)。
   - 這是**判定的直接翻譯**,不是另一層新分析——`Pass,無 paper 注` 就整組卡片全部 `.pill.no`(m104 範本:仍要把 edge hunt 的近門檻候選各自列一張卡講「為什麼沒過」,近門檻但未達標的用 `.pill.watch`,不能只放一句「無」);有 Bet 候選或條件單(T-75 名單分支之類)就對應 `.pill.go`/`.pill.wait`。
   - 結尾一定要有 `.action-total`:總曝險 X/1000,一句話收尾。
   - 這組卡片跟第 3 節「市場划算度」表不是重複——划算度表是給每個數字的公允價對比,建議下注是「看完全部數字後,我真正會怎麼做」,兩者常常不一致(划算度最高的格子經常是被判盲區汙染、不建議下的,範本見 m103 第 3 節的大小球/和局)。
2. **表格優先於段落**:能用一行表格列出的數字,不要展開成敘述句。
3. **每句話要有實質內容,不是重複表格或湊字數**:`.note` 說明框每個只講一件表格沒講的事——一個新發現、一個該打折的理由、一個具體的下一步,不是把上面表格的數字再唸一遍。禁止「值得注意」「可能」「或許」這類懸空修飾語後面沒接具體結論——要嘛講清楚意味著什麼,要嘛刪掉。
4. **少黑話,通篇檢查不是只改一處,連緊湊標籤都要換**:xG→「應進球」,λ→「預期進球數」,EV→「划算度」,Elo→「戰績評分」(包含 badge 這種緊湊標籤,例:`戰績評分 2232`,不是 `Elo 2232`),1x2→「勝平負」,BTTS→「雙方進球」,M/J→第一次出現時用「純戰績評分算出來的/賽前已封存的正式判斷」帶一次,edge/favorite/workflow 這類英文詞換中文說法。R32/QF/SF 這類賽事輪次縮寫換成「32強/8強/4強」。改完用 Grep 對 `xG|λ|EV|BTTS|1x2|edge|workflow` 掃一遍全檔,不要漏改。
5. **每個 section 要有觀點,不是資料傾印**:光列數字不算做完——每張表旁邊要講「這代表什麼」,尤其是分歧、反常、或跟市場對不上的地方,要給具體判讀,不是留給讀者自己猜。

## Step 3 — 事實查核(強制,這是這個 skill 存在的理由)

**不查核就不算完成**,目的是確保數據真實而非模型幻覺——生成完的 HTML 送進:
```
Workflow({ name: 'comparison-table-factcheck', args: {
  html_path: '<絕對路徑>\\reports\\<match_id>_final_comparison.html',
  match_id: '<id>', home: '<主隊>', away: '<客隊>',
  source_files: [
    { label: '研判提案', path: '<judgment-synthesis 產出的 final lean 路徑>' },
    { label: '對抗查核', path: '<...三個透鏡的 critique 路徑>' },
    { label: 'home/away 鑑識', path: '<僅在條件式從零鑑識實際執行時才有;未執行則整列省略>' },
  ],
  repo_root: 'C:\\path\\to\\wc2026',
} })
```
（`judgment-synthesis` 的 agent 輸出不會自動存成檔案,先用 Write 存下來才有路徑可以傳給這裡的 `source_files`。其從零鑑識為**條件式**:僅 `live_divergence` 為真的場次才有該兩份報告,未執行時不得將空路徑放入 `source_files`。）三個獨立透鏡平行跑:
1. **球隊數據溯源**:表裡每個數字回頭對照鑑識報告原始檔跟 `results.json`,標 CONFIRMED / DISCREPANCY / UNSOURCED。
2. **市場數學獨立重算**:不相信 HTML 的算術,自己重新呼叫 Step 1 那組函式重算一次,逐格比對。
3. **敘事/引言查核**:HTML 裡任何看起來像引用鎖定檔或其他文件原文的句子,回頭核對是不是真的逐字或至少內容準確。

查核完會有一份修正清單,須逐項修正,不得只作為參考意見保留。

## Step 4 — 使用者即時盤口(選用)

可向使用者詢問是否有截圖覆蓋掉初版用的 Odds API 快照或網路查價,尤其 BTTS、晉級/奪冠、隊伍分盤這幾個 Odds API 抓不到的市場。收到截圖:
1. `Read` 直接讀圖(不要用 OCR 或轉述,截圖本身就是證據)。
2. 用 `devig.devig_both` 去水算市場真實機率,`sizing.ev` 對 Step 1 算出的研判機率算划算度,**不得手動估算**。
3. 標明每個市場數字的真正來源(Odds API 快照的時間戳 vs 使用者截圖),不要混在一起讓讀者以為精度一樣。

## 產出

`reports/<match_id>_final_comparison.html`(檔名的 `match_id` 慣例上可簡寫成 `mNNN`,如 `m104_final_comparison.html`,不強制用鎖定檔的完整 id)——單檔、自包含(內嵌 CSS,無外部依賴),深色/淺色主題都要能看。同時以口頭向使用者說明查核結果(是否有幻覺、修正了哪些數字),不能只提供連結。

## 已知限制,誠實列出

- 事實查核 workflow 目前只查「數字有沒有出處、算得對不對」,不查「乘數本身的判斷合不合理」——那是研判階段對抗查核 agent 的工作,這個 skill 不重做。
- 沒有 Odds API 快照又沒有使用者截圖的市場(通常是 BTTS/晉級/正確比分),只能標「非機械抓取,無法計算划算度」,不要為了填滿表格硬湊一個數字。
