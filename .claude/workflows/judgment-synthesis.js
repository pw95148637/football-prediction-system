// 研判 (the judgment layer) — synthesis for every fixture in a matchweek,
// then adversarial critique whose depth follows a program rule.
//
// REPLACES `team-forensics-judgment.js` (2026-09-03). That script sent two
// agents to research both teams FROM ZERO and then critiqued them. That
// shape predates the evidence layer: exploration is now the SCOUTS' job,
// and by the time this workflow runs the bundle has already been validated
// and checked for coverage across all ten dimensions of
// `evidence.schema.DIMENSIONS`. Two more agents re-researching the same
// ground is paying twice for one piece of reasoning. Worse, the old script
// never required either agent to use anything J structurally cannot do, so
// nothing in it enforced DECISION_PROTOCOL section 2's acceptance test — a
// judgment that uses no such capability is "a J with a looser standard",
// and a looser standard is a recipe for manufacturing edge. Its xrefs also
// pointed into `history/falsification-2026/` (verdicts_main.md,
// pilot_verdicts.md, results.json), which holds international fixtures only
// and nothing about any English club.
//
// What this produces is a PROPOSAL, never a written record: the store is
// append-only and subagents must not write to it (AGENT_DISPATCH section 1).
// The calling session adjudicates and writes the row itself with
// `decision/judgment.py`'s `record()`, which validates the schema.
//
// From-zero forensics is CONDITIONAL (`live_divergence`): it runs where no
// estimate independent of M exists -- J missing, or locked with zero factors
// (J = M, all ten GW06 fixtures) -- since otherwise every number in the
// fixture is M plus edits and the judgment cannot land anywhere but on M or
// on the market. It builds each side's strength WITHOUT reading M or any
// price, runs alongside the synthesis, and the revision must reconcile it.
//
// Critique depth is decided by `fullReviewReason`, from numbers alone. A
// fixture gets all three lenses when its lean differs from the market or from
// the locked J by `depth.escalate_pp` or more on any outcome, names a
// mechanism (`verified`, `graded`), has no locked J or no market reading to
// compare against, or carries a live disagreement. Every other fixture gets
// the known-bias/abstention-burden lens alone: the commitment to a lean is
// the same for all, and that lens is the one that guards against drift into
// abstention (DECISION_PROTOCOL section 4). Arithmetic consistency of the
// row is enforced by `judgment.validate_record` at write time either way.
//
// Invoke:
// Workflow({ name: 'judgment-synthesis', args: {
//   repo_root: '<absolute path of this checkout>',   // all of `args` is generated: `ops judgment-brief --gameweek N`
//   today: '<ISO date>',
//   era: 'v1-epl-2026-27',
//   known_biases: ['<this window\'s own measured instrument biases>'],
//   depth: { escalate_pp },          // gap (pp) at which a fixture gets the full critique
//   matches: [{
//     mid, home, away, kickoff,
//     bundle_file,                  // absolute path, already validate_bundle'd
//     coverage,                     // {covered:{dim:n}, silent:[]} — silent MUST be empty
//     m_probs, j_probs,             // {home,draw,away}; j_probs null when foreclosed
//     j_status,                     // 'locked' | 'foreclosed' | 'absent'
//     j_factors, j_basis,           // what J actually said, for the divergence note
//     market,                       // {devigged:{...}, best_prices:[{selection,book,decimal_odds}], gap_pp}
//     live_divergence,              // bool — adds the from-zero forensics stage
//     divergence_reason,            // string — why, shown to the agents
//   }],
// } })

export const meta = {
  name: 'judgment-synthesis',
  description: '研判: per-fixture committed lean (direction/probability/confidence/divergence from J) using capabilities J structurally lacks, then adversarial critics (three lenses where the numbers call for it, one otherwise), then a revision that must answer them',
  phases: [
    { title: 'Synthesize', detail: 'one lean per fixture, computed with project code, not by feel' },
    { title: 'Forensics', detail: 'from-zero re-derivation — ONLY for fixtures with a live disagreement to adjudicate' },
    { title: 'Critique', detail: 'full: arithmetic, evidence provenance, known-bias/abstention burden; otherwise the last lens alone' },
    { title: 'Revise', detail: 'answer every critic on the record; a lean that survives unchanged must say why' },
  ],
}

const A = (typeof args === 'object' && args) ? args : {}
if (!A.repo_root || !Array.isArray(A.matches) || !A.matches.length) {
  throw new Error(
    `judgment-synthesis requires args: { repo_root, era, known_biases?, depth: { escalate_pp }, matches: [{mid, home, away, kickoff, bundle_file, coverage, m_probs, j_probs, j_status, j_factors?, j_basis?, market?, live_divergence?, divergence_reason?}] }. ` +
    `Received: ${typeof args === 'string' ? 'a string (not an object)' : JSON.stringify(args).slice(0, 400)}.`
  )
}

const REPO = A.repo_root
const ERA = A.era || 'v1-epl-2026-27'
const ESCALATE_PP = A.depth && A.depth.escalate_pp
const OUTCOMES = ['home', 'draw', 'away']

// Largest per-outcome difference between two 1X2 vectors, in percentage points.
const maxGapPp = (p, q) => Math.max(...OUTCOMES.map((k) => Math.abs(p[k] - q[k]))) * 100

// Why a fixture needs the full critique, or null when one lens is enough.
// Anything that cannot be compared escalates: no threshold, no locked J, no market.
const fullReviewReason = (lean, m) => {
  if (!(ESCALATE_PP > 0)) return 'no escalation threshold supplied'
  if (m.live_divergence) return m.divergence_reason || 'live disagreement to adjudicate'
  if (m.j_status !== 'locked' || !m.j_probs) return 'no locked J to compare against'
  if (!m.market) return 'no market reading to compare against'
  if (lean.mechanism_grade === 'verified' || lean.mechanism_grade === 'graded') {
    return `mechanism named (${lean.mechanism_grade})`
  }
  const vsMarket = maxGapPp(lean.judgment_probs_90, m.market.devigged)
  if (vsMarket >= ESCALATE_PP) return `lean differs from market by ${vsMarket.toFixed(1)}pp`
  const vsJ = maxGapPp(lean.judgment_probs_90, m.j_probs)
  if (vsJ >= ESCALATE_PP) return `lean differs from J by ${vsJ.toFixed(1)}pp`
  return null
}

const LEAN_SCHEMA = {
  type: 'object',
  properties: {
    match_id: { type: 'string' },
    judgment_probs_90: {
      type: 'object',
      properties: { home: { type: 'number' }, draw: { type: 'number' }, away: { type: 'number' } },
      required: ['home', 'draw', 'away'], additionalProperties: false,
    },
    direction: { type: 'string', enum: ['home', 'draw', 'away'] },
    prob: { type: 'number' },
    confidence: { type: 'string', enum: ['low', 'medium', 'high'] },
    basis: { type: 'string' },
    diverges_from_j: { type: 'boolean' },
    divergence_note: { type: 'string' },
    capabilities_used: {
      type: 'array', minItems: 1,
      items: {
        type: 'object',
        properties: {
          capability: {
            type: 'string',
            enum: ['project_code_arithmetic', 'historical_result_lookup',
              'scout_number_verification', 'player_or_phase_decomposition',
              'mechanism_ledger_hit_rate', 'adversarial_review'],
          },
          what: { type: 'string' },
        },
        required: ['capability', 'what'], additionalProperties: false,
      },
    },
    mechanism_grade: {
      type: 'string',
      enum: ['verified', 'graded', 'searched_not_found', 'actively_excluded'],
    },
    mechanism_score: { type: 'number' },
    mechanism_type: { type: 'string' },
    market_error_mechanism: { type: 'string' },
    abstention: {
      type: 'object',
      properties: {
        missing: { type: 'string' }, flips_if: { type: 'string' }, known_by: { type: 'string' },
      },
      required: ['missing', 'flips_if', 'known_by'], additionalProperties: false,
    },
    commands_run: { type: 'array', items: { type: 'string' } },
    honest_gaps: { type: 'string' },
    post_price_changes: { type: 'string' },
  },
  required: ['match_id', 'judgment_probs_90', 'direction', 'prob', 'confidence',
    'basis', 'diverges_from_j', 'divergence_note', 'capabilities_used',
    'mechanism_grade', 'market_error_mechanism', 'abstention', 'commands_run',
    'honest_gaps'],
  additionalProperties: false,
}

const CRITIQUE_SCHEMA = {
  type: 'object',
  properties: {
    lens: { type: 'string' },
    verdict: { type: 'string', enum: ['stands', 'needs_revision', 'refuted'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim: { type: 'string' },
          why_wrong: { type: 'string' },
          evidence: { type: 'string' },
          severity: { type: 'string', enum: ['fatal', 'material', 'minor'] },
        },
        required: ['claim', 'why_wrong', 'evidence', 'severity'],
        additionalProperties: false,
      },
    },
    checks_run: { type: 'array', items: { type: 'string' } },
  },
  required: ['lens', 'verdict', 'findings', 'checks_run'],
  additionalProperties: false,
}

const biasBlock = (A.known_biases && A.known_biases.length)
  ? `## M 的實測偏誤(以下只量過 M;J 未對收盤價計分;研判另由 decision.track_record 以自己的紀錄計分,未生效前不作為研判優劣的依據)\n\n${A.known_biases.map((b) => `- ${b}`).join('\n')}\n\n這些是 M 的警訊:研判若只是沿用 M 的數字,而 M 正落在這個區間,就要交出自己的依據。研判以自己的證據偏離 M 或市場時,不得僅憑這幾條推定研判錯,也不得只引用它們作為 PASS 或向市場收斂的理由。`
  : ''

// Stage 1 (synthesis, critique, revision) never sees a price: the
// probability is formed blind, and only stage 2 (pricing) adds the market.
// A judgment that saw the price first cannot be scored as independent of it
// (GW06 ast_bre: "the market price was in the brief from the start ...
// anchoring cannot be excluded"), and the track record would then measure a
// lean already pulled toward the close.
const blindBlock = `## 市場讀數:本段刻意不給

這一段**價格盲**:先只憑球隊事實、證據與專案程式定出機率。看不到價格不是缺陷,是這一段的條件——你的機率之後會原封不動地帶進第二段,第二段才看價,只用來算 f\\*、判斷市場可能錯在哪、寫 abstention。

本段的 \`mechanism_grade\`、\`market_error_mechanism\`、\`abstention\` 先填暫定值(例如 \`searched_not_found\`、「待第二段看價後定案」),\`abstention\` 寫你目前已知的缺口與翻轉條件即可。不要用網路搜尋任何賠率、交易所或預測網站;誤見就在 \`honest_gaps\` 申報。`

const marketBlock = (m) => m.market
  ? `## 市場讀數(J 已鎖定,這一層可以看)

- 去水後市場機率:${JSON.stringify(m.market.devigged)}
- 真實可下注報價:${JSON.stringify(m.market.best_prices)}
- M 與市場的落差:${m.market.gap_pp} pp

**市場價可以用來算 f\\*,不可以用來反推機率。** 「市場這樣定所以大概是對的」不是理由;「市場定在這裡,而我算出來的 f\\* 是負的,所以沒有部位」才是。`
  : `## 市場讀數

**這一場還沒有價格。** f\\* 無法計算,\`market_error_mechanism\` 只能就機制本身論述。傾向判斷照樣要交(章程 §4)。`

const jBlock = (m) => {
  if (m.j_status === 'locked') {
    return `## 已鎖定的 J(隔離儀器,輸入只有 M + 證據包 + 凍結 spec)

- J 機率:${JSON.stringify(m.j_probs)}
- J 的 factors:${JSON.stringify(m.j_factors || [])}
- J 的 basis:${JSON.stringify(m.j_basis || [])}

**J 回傳空 factors 不代表這一層無事可做——那正是最需要研判的時候。** J 的輸入被刻意限死(無工具、無專案程式、無歷史資料庫、無法回頭查證蒐證者的數字),它拒絕偏離常常是因為它拿不到判斷所需的東西,不是因為那些東西不存在。

與 J 的任何差異都必須寫進 \`divergence_note\`,**不得默默覆蓋**(章程 §2)。**同意 J 也要寫**:說明為什麼在你動用了 J 沒有的能力之後仍然同意——那是一個發現,不是預設值。`
  }
  if (m.j_status === 'foreclosed') {
    return `## J:永久不可得(foreclosed)

這一場在 J 鎖定前就落了價,\`amend_lock\` 會拒絕任何 J。**這不是隨機缺失**——舊排程系統性地讓某些時段的場次受害。

所以這一層是本場唯一的非機械讀數。\`diverges_from_j\` 必須是 \`false\`,\`divergence_note\` 寫明「J 已 foreclosed,無可比對」與這對本場結論的影響。`
  }
  return `## J:本場沒有 J\n\n\`diverges_from_j\` 必須是 \`false\`,\`divergence_note\` 說明原因。`
}

const synthBrief = (m) => `# 研判:${m.home} v ${m.away}(${m.mid},開球 ${m.kickoff})

今天是 ${A.today || '(args.today 未提供)'}。你是**研判層**。你的工作不是重做一次 J,是交出一個 J **在結構上做不到**的判斷。

## 你跟 J 的差別(章程 §2,這是驗收判準)

J 的輸入嚴格只有三樣(M、已驗證證據包、凍結 spec),跑在無工具的隔離 subprocess 裡。你不受這個限制,你可以:

1. **\`project_code_arithmetic\`** — 用專案自己的程式算,不心算。
2. **\`historical_result_lookup\`** — 查 2015-16 起約 4190 場英超真實賽果。
3. **\`scout_number_verification\`** — 回到原始來源,查核蒐證者給的數字。
4. **\`player_or_phase_decomposition\`** — 球員層、分階段(開放/定位球/反擊/點球)拆解。
5. **\`mechanism_ledger_hit_rate\`** — 引用本專案自己的機制分類帳命中率。
6. **\`adversarial_review\`** — 接受對抗查核(下一階段會有三個查核員來拆你)。

**至少要真的動用一項,並在 \`capabilities_used\` 裡寫清楚做了什麼。** 只列能力名稱、沒有實際內容,等同沒做。一份沒動用任何一項的研判,是「標準較寬鬆的 J」,退回重做。

## 輸入

- 證據包(已通過 \`validate_bundle\` 機械驗證,十維度覆蓋完整):\`${m.bundle_file}\`
- 覆蓋狀況:${JSON.stringify(m.coverage)}
- M(純戰績 Elo + Dixon-Coles,**對陣容組成結構性失明**):${JSON.stringify(m.m_probs)}

${jBlock(m)}

${blindBlock}

${biasBlock}

## 怎麼算(不要憑感覺寫機率)

在 \`${REPO}\` 下用專案的程式。可用的入口:

\`\`\`bash
PYTHONIOENCODING=utf-8 python -c "
from football_ai.competitions.epl_2026_27 import CONFIG, ERA, _fit_params, current_ratings
from football_ai.decision.report import lambdas_at
from football_ai.models.m_dc import score_matrix, outcome_probs
from football_ai.decision import sizing
from football_ai.store import io
# 例:把攻守乘數套上 M 的 lambda,重算 1X2
g_a, g_b, rho = _fit_params()
# lambdas_at 一定要帶 era=ERA;不帶時讀到的是世界盃的進球曲線,不是英超的
# lam_h, lam_a = lambdas_at(d_eff, era=ERA)
# lam_h, lam_a = ...(從鎖定檔的 lambdas 出發,乘上你的乘數)
# print(outcome_probs(lam_h, lam_a, rho))
"
\`\`\`

歷史賽果對照:
\`\`\`bash
PYTHONIOENCODING=utf-8 python -c "
from football_ai.data.football_data_co_uk import load_seasons
rows = load_seasons('E0', range(2015, 2027), missing_ok=True)
# 篩你要的對照組,自己算,不要憑印象
"
\`\`\`

機制分類帳(結算後由 \`ops materialise\` 依鎖定時的檢核寫入;查到幾筆就用幾筆,0 筆也是誠實的輸入,**不要拿世界盃的命中率頂替**。\`lambda_precedent\` 只在找到機制(\`verified\`、\`graded\`)時生效,n=0 給 0.5;沒找到機制時是 1.0,折扣已含在 \`lambda_mech\`,不要再扣一次,STAKING §2.3):
\`\`\`bash
PYTHONIOENCODING=utf-8 python -c "
from football_ai.decision.sizing import precedent_stats
print(precedent_stats('<your mechanism_type>', era='${ERA}'))
"
\`\`\`

球員缺陣而查不到他個人的產出時,用 \`football_ai.data.absence_baserate.multipliers([...位置組...], 該隊基準 xG-for, 基準 xG-against)\` 的同位置基準率,不要歸零。

## 交付

1. **\`judgment_probs_90\`**:三個機率,加總為 1(誤差 1e-6 內)。從進球模型算出來,不要手寫湊數。
2. **\`direction\` / \`prob\` / \`confidence\`**:承諾的傾向。\`prob\` 必須**等於** \`judgment_probs_90[direction]\`。
   - **不准為了安全把量級壓在 J 的取樣離散度以內**——那是刻意設計成不可證偽的輸出,章程 §4 明文禁止。
   - 同意市場也是一個承諾:給機率、可被 CLV 檢驗、要入帳。「沒找到」不是結論。
3. **\`diverges_from_j\` / \`divergence_note\`**:見上。同意也要寫。
4. **\`mechanism_grade\`**(給 λ 用,見 \`STAKING.md\` §2.1):
   - \`verified\` = 具體、可證偽的市場錯誤機制,九項防錯全過
   - \`graded\` = 有機制但信心不滿,附 \`mechanism_score\`(0-1)。**分數只能沿用 edge-hunt 工具的輸出,不自訂主觀分數**(STAKING §2.1);沒有工具分數就不是 \`graded\`,改用下面兩級之一
   - \`searched_not_found\` = 真的查過,市場錯在哪答不出來
   - \`actively_excluded\` = 曾有候選機制,已被明確排除
   同時給 \`mechanism_type\`(對齊 J 的分類:\`squad_composition_change\` / \`injury_replacement\` / \`rotation_discount_recovery\` / \`rest_travel\` / \`tactical_stylistic_matchup\` / \`incentive\` / \`other\`)。
5. **\`market_error_mechanism\`**:一句話講市場**具體**錯在哪;答不出來就誠實寫「答不出來」(gate 會據此判 PASS,那是合法結論)。
6. **\`abstention\`**(章程 §4,三欄缺一報告會印 UNFINISHED):
   - \`missing\`:缺的是**哪一類**資訊
   - \`flips_if\`:什麼**具體可觀測事件**會翻轉這個判斷
   - \`known_by\`:那件事最晚**何時**可知(給日期時間)
7. **\`commands_run\`**:你實際跑過的指令,逐條。這是 \`capabilities_used\` 的證據,不是裝飾。
8. **\`honest_gaps\`**:你查不到、只能留白的東西。

## 護欄

- **絕對不要寫入** \`${REPO}/football_ai/store/data/\`(append-only 稽核軌跡)或 \`${REPO}/history/\`(唯讀封存)。你交提案,不寫記錄。
- 不要修改任何鎖定檔。
- \`history/falsification-2026/\` 裡的 \`results.json\`/\`locks/\` **只有國際賽,沒有任何英超俱樂部資料**,不要拿來當本場的對照。
- 不要為了跟 J 不一樣而不一樣;也不要為了安全而跟 M 一樣。兩者都是把輸出設計成不可檢驗。`

const forensicsBrief = (m, side) => `# 從零鑑識:${side === 'home' ? m.home : m.away}(${m.mid},對手 ${side === 'home' ? m.away : m.home})

這一場需要一份**不依附 M 的估計**:${m.divergence_reason || '(未說明)'}。本場其餘每個數字都是「M 加修正」,所以研判只能落在 M 或市場上;你交的是第三個、獨立建立的讀數。

## 不可讀的東西(獨立性的前提)

- **不讀 M**:不要跑 \`current_ratings\`、\`lambdas_at\`、\`_fit_params\`、\`report.build\`,不要打開鎖定檔或 \`store/data/\`。
- **不讀任何價格或預測機率**:不查博彩、交易所、預測網站、「勝率模型」。證據包已經過防火牆;外部搜尋時避開這類頁面,誤見就在產出中申報。

## 你的任務

只針對 **${side === 'home' ? m.home : m.away}**,從事實建立這一隊的**進攻指數**與**防守指數**(相對聯賽平均,1.00 = 平均;防守指數 = 失球端,越大越差),主客場分開:

1. **產出基準**:本季與上季的 xG-for / xG-against 每 90 分鐘(Understat、FotMob 等,證據包或原始來源),依樣本量加權;本季場數少就明說,並說明加權依據。
2. **陣容變動**:夏窗進出與本場傷停,點名球員,附其出場分鐘與產出占比(xG、xA,能取得的防守數據),以及接替者的水準。
3. **查不到的數據不歸零**:某球員缺數據時,用同位置的基準率估計其影響,並加寬不確定性;「沒有數據」不等於「影響為零」。基準率表已建好:\`from football_ai.data.absence_baserate import multipliers, effect\`(英超 12 季、4,560 場量得;進攻端已依樣本外校準打折,防守端用 2022-25 的值),用法與可信度見該模組說明。
4. **回歸的幅度**:開季數場的表現要回歸多少,用 \`football_ai.data.football_data_co_uk.load_seasons('E0', range(2015, 2027), missing_ok=True)\` 的真實賽果自己算(例如開季 N 場的進失球對後續的預測力),不要憑印象。

證據包(已驗證,價格盲):\`${m.bundle_file}\`

## 產出

- 進攻指數、防守指數(本場的主或客場情境),各附 1 個標準差的不確定性;
- 每一項調整的來源與量級,標明機械計算、歷史實證、分析判斷;
- 聯賽平均進球(主、客)用你算出的數字並寫明來源,讓下游能組合:λ_主 = 聯賽主場平均 × 主隊進攻指數 × 客隊防守指數,λ_客 同理;
- 誠實列出查不到的項目。

## 護欄

- 不要寫入 \`${REPO}\` 內任何檔案(含 \`football_ai/store/data/\`、\`history/\`)。
- \`history/falsification-2026/\` 只有國際賽資料,對英超俱樂部不適用。`

const pricingBrief = (m, lean) => `# 第二段(看價):${m.home} v ${m.away}(${m.mid})

第一段已在**價格盲**的條件下,經綜合、對抗查核與修訂定出下面的研判。現在才給你市場價。

## 第一段的研判(機率已定案)

\`\`\`json
${JSON.stringify(lean, null, 2)}
\`\`\`

${marketBlock(m)}

${biasBlock}

## 你的任務

1. **\`judgment_probs_90\` 原樣保留。** 價格不是改機率的理由。唯一例外:你在這一段查到第一段沒有的**非價格**新資訊(例如已公布的先發名單、賽前新傷停)。這時才能改,並在 \`post_price_changes\` 逐項寫明資訊、來源、時間與量級,用專案程式重算。沒有新資訊時,\`post_price_changes\` 寫「無」。程式會核對:機率變了而本欄寫「無」,一律退回第一段的數字。
2. **f\\***:對每個結果,以 \`football_ai.decision.sizing.kelly_fraction(p, 真實賠率)\` 計算(\`p\` 用研判機率,賠率用上面具名書口的真實報價,不用去水價),列入 \`commands_run\`。
3. **市場可能錯在哪(章程 §5)**:定 \`market_error_mechanism\`、\`mechanism_grade\`、\`mechanism_type\`。具體機制須交代三件事:市場尚未反映什麼、為什麼尚未反映、之後可觀測到什麼事件來檢驗。說不出就照實寫「答不出」,這是合法結論。
4. **\`abstention\`(章程 §4)**:三欄都要具體:缺哪一類資訊、什麼可觀測事件會翻轉判斷(用程式算出門檻)、最晚何時可知(日期時間)。同意市場也是承諾,要量化。
5. 其餘欄位(\`basis\`、\`capabilities_used\`、\`honest_gaps\` 等)沿用第一段,補上這一段做了什麼。

不要寫入 \`${REPO}\` 內任何檔案。交出同一個 schema 的完整版本。`

const LENSES = [
  {
    key: 'arithmetic',
    title: '算術與程式查核',
    brief: `你的透鏡是**算術**。不要評論敘事好不好,只查數字對不對。

1. **重跑它的計算**。它在 \`commands_run\` 裡宣稱跑過的每一條,你自己跑一次(\`${REPO}\` 下)。跑出來不一樣就是 finding。宣稱跑過但根本跑不起來的指令,是 fatal。
2. **機率自洽**:\`judgment_probs_90\` 加總是不是 1?\`prob\` 是不是等於 \`judgment_probs_90[direction]\`?機率是從進球模型算出來的,還是手寫的?用 \`football_ai.models.m_dc.outcome_probs\` 從它宣稱的 lambda 重算,比對。
3. **λ 的算術**:如果它給了 mechanism_grade,用 \`football_ai.decision.sizing.confidence_lambda\` 自己算一次 λ,看它的分級對不對(特別是:**沒找到機制時 \`lambda_precedent\` 必須是 1.0,不是 0.5**——否則同一個「沒有」被扣了兩次)。
4. **f\\*** :用 \`sizing.kelly_fraction(p, 真實賠率)\` 算,\`p\` 必須是**研判自己的機率**、賠率必須是**真實可成交的價**(具名書口 + 時間戳),不是去水後的公平價。用去水價算 f\\* 會系統性高估。
5. **量具先於數字**(AGENT_DISPATCH §3):先確認它用的函式是不是這個 era 的擬合參數,不是別的窗口的。2026-08-28 的先例:一份鑑識報告的回歸腳本簽名錯了,自己印出的數字跟自己的算術差了一百多個 Elo,方向整個反了——那是重跑量具才發現的,不是讀出來的。`,
  },
  {
    key: 'evidence',
    title: '證據溯源查核',
    brief: `你的透鏡是**證據的真實性與相關性**。

1. **回到來源**。挑三筆對這個結論最關鍵的 claim(優先挑它拿來當機制依據的、以及標 \`likely_unpriced\` 的),打開原始來源核對。數字對不上、日期過期、來源等級被高估,都是 finding。
2. **claim 撐得住這個機制嗎?** 常見的失敗是:證據說 A,結論用的是 A 的一個更強版本。逐條檢查「這筆證據實際說了什麼」與「它被拿來支持什麼」之間的距離。
3. **機制轉移**:如果理由來自「上一輪對某個對手發生的事」,有沒有**這場對手自己的數據**證明同一個弱點會轉移過來?只有「應該也適用」的推論而無本場對手實證,標記為證據不足。
4. **重複計算與換名重入**:兩隊各自的機制是不是同一個現象被算了兩次(例如 A 隊的弱點 X 與 B 隊的優勢 Y 是同一件事,不可兩邊都加乘)?J 已在 \`j_basis\` 裡拒絕採用的機制,有沒有換個名稱重新帶進來?
5. **absorption 判斷**:標成 \`likely_unpriced\` 的東西,真的沒被市場消化嗎?英超是每週交易、球隊新聞完全公開的市場,真正未定價的資訊比盃賽對決稀少得多。一則兩天前就上了 BBC 的傷停,不會是 unpriced。
6. **樣本量**:任何趨勢說法(「本季 PPDA 下降」「定位球變強」)都要看它依據幾場。場數少時,查它有沒有補上賽季基準,還是拿少數幾場當趨勢。`,
  },
  {
    key: 'bias',
    title: '已知偏誤與棄權舉證',
    brief: `你的透鏡是**這個系統自己的失敗形狀**,以及棄權的舉證責任。

1. **M 的實測偏誤**(只量過 M,列在上方):M 的落差越大,越可能是 M 錯。檢查這份研判是不是只是沿用 M 的數字、把 M 的大落差當成機會;是的話,它必須交出具體機制,不能只說「模型這樣算」。研判若有自己的獨立依據,M 的偏誤不構成推翻它的理由:要拆它,就拆它的依據。
2. **但反過來也是錯的**(章程 §4):不能因為「市場通常是對的」就把每一場都收斂回市場。棄權與行動負**同等**舉證責任。(本段價格盲,\`abstention\` 與機制在第二段定案;這裡檢查的是它目前已寫下的缺口與翻轉條件是否具體。)檢查:
   - \`abstention\` 三欄是不是**具體**的?「需要更多資訊」不是 \`missing\`;「情況改變」不是 \`flips_if\`;沒有日期不是 \`known_by\`。
   - 如果結論是同意市場,它有沒有以**承諾的形式**交出(機率、可 CLV 檢驗、入帳),還是只是空泛地推託?
   - 把「PASS / 無注 / 未解 / 證據不足」這幾個詞從它的產出裡刪掉,還剩下可被證偽的內容嗎?剩不下就是不合格。
3. **量級是不是被刻意壓小求安全?** 把偏離刻意壓在 J 的取樣離散度以內,是設計成不可證偽的輸出,章程明文禁止。
4. **陣容陳舊的修正有沒有做到位?** M 對陣容組成結構性失明。如果這隊夏窗有實質人員變動而研判完全沒動,那是漏了一個 M 修不了、而這一層修得動的東西。反過來,如果它動了,有沒有點名球員、附實際產出(分鐘數/參與度/防守量體)、處理替補水準差異?某一樣查不到時,要求它改用 \`football_ai.data.absence_baserate\` 的同位置基準率並加寬不確定性;**不要要求歸零**——「查不到所以影響為零」與沒有依據的量級同樣不合格(章程 §4)。
5. **禁區**:任何「市場對熱門過度自信」「這個數字看起來太極端」形式的推理,是**市場模擬**,不是球隊事實。分辨測試只有一句:理由引用的是**球隊的事實**,還是**市場的存在**?`,
  },
]

const critiqueBrief = (m, lens, lean, forensics) => `# 對抗查核(透鏡:${lens.title}) — ${m.home} v ${m.away}(${m.mid})

你是**對抗式查核員**。你的工作是**主動去找能推翻這份研判的證據**,不是重讀一遍看看順不順。找不到問題要明說找不到,但預設立場是懷疑。

**不確定時,判 \`needs_revision\`,不要放行。**

${lens.brief}

---

## 待查的研判

\`\`\`json
${JSON.stringify(lean, null, 2)}
\`\`\`

${biasBlock}

${forensics ? `## 同場的從零鑑識報告(供你交叉比對)\n\n${forensics.filter(Boolean).join('\n\n---\n\n')}\n` : ''}

## 本場的其他輸入

- M:${JSON.stringify(m.m_probs)}
- J(${m.j_status}):${JSON.stringify(m.j_probs)} / factors: ${JSON.stringify(m.j_factors || [])}
- 證據包:\`${m.bundle_file}\`
- 市場:本段刻意不給(價格盲;第二段才看價)。不要搜尋任何賠率或預測網站。

## 產出

每個 finding 要有:被質疑的**具體主張**、**為什麼錯**、**你的證據**(跑過的指令與其輸出、你查到的來源)、**嚴重度**。
\`checks_run\` 要逐條列出你實際執行的查核動作——沒跑過的不要寫。

**不要**寫入 \`${REPO}/football_ai/store/data/\` 或 \`${REPO}/history/\`。`

const reviseBrief = (m, lean, critiques, forensics) => `# 修訂:${m.home} v ${m.away}(${m.mid})

對抗查核員從不同透鏡拆過你的研判。你要**逐條回應**,然後交出最終版本。

## 你原本的研判

\`\`\`json
${JSON.stringify(lean, null, 2)}
\`\`\`

## 查核意見

${critiques.filter(Boolean).map((c) => `### 透鏡:${c.lens} — 判定 ${c.verdict}
執行過的查核:${(c.checks_run || []).join(' / ')}
${(c.findings || []).map((f) => `- **[${f.severity}]** ${f.claim}\n  - 為什麼錯:${f.why_wrong}\n  - 證據:${f.evidence}`).join('\n') || '- (無 finding)'}`).join('\n\n')}

${forensics && forensics.filter(Boolean).length ? `## 同場的從零鑑識(不依附 M、不看價格的獨立估計)

${forensics.filter(Boolean).join('\n\n---\n\n')}

**這是第二個估計,不是判決。** 用專案程式把兩份鑑識組合成本場的 λ(λ_主 = 聯賽主場平均 × 主隊進攻指數 × 客隊防守指數,λ_客 同理),再以 \`football_ai.models.m_dc.outcome_probs\` 算出 1X2,列入 \`commands_run\`。把它和你的研判並列:差多少、為什麼;最終機率採用哪一份、或怎麼加權,都要有依據並寫進 \`basis\`。兩份都不得因為比較接近 M 或比較接近市場而被選。

` : ''}## 規則

1. **每一條 \`fatal\` 與 \`material\` 都要處理**:改掉,或說明為什麼查核員錯了——說明必須帶你自己的證據(跑過的指令、查到的來源),不能只是重申原本的立場。
2. **查核員的意見也是待驗證的主張**,不是判決。它說你算錯了,你自己重算一次再決定。它錯了就明講它錯在哪。
3. **不要因為被質疑就把量級縮到零**。收斂回 M 或收斂回市場,如果是被證據逼出來的,那是正確結論;如果只是為了讓查核員閉嘴,那是把輸出設計成不可證偽,章程 §4 明文禁止。
4. \`capabilities_used\` 要加上 \`adversarial_review\`,\`what\` 寫清楚查核改變了什麼(或為什麼沒改變)。
5. \`basis\` 要把修訂的理由寫進去,\`honest_gaps\` 要包含查核員指出、而你確實補不了的缺口。

交出跟原本同一個 schema 的最終版本。`

// ---------------------------------------------------------------------------

log(`研判: ${A.matches.length} fixtures. Per fixture: 1 synthesis, then 3 critique lenses where fullReviewReason applies and 1 otherwise, plus a revision when a critic blocks or forensics must be reconciled. From-zero forensics (2 agents, alongside the synthesis) runs where no estimate independent of M exists — ${A.matches.filter((m) => m.live_divergence).length} of ${A.matches.length} qualify.`)

const results = await pipeline(
  A.matches,

  // 1. the lean itself, computed with the project's own code, and -- where
  //    no estimate independent of M exists -- from-zero forensics alongside
  //    it (they never see the lean, the lean never sees them)
  async (m) => {
    const runs = [() => agent(synthBrief(m), { label: `synth:${m.mid}`, phase: 'Synthesize', schema: LEAN_SCHEMA })]
    if (m.live_divergence) {
      for (const side of ['home', 'away']) {
        runs.push(() => agent(forensicsBrief(m, side), { label: `forensics:${m.mid}:${side}`, phase: 'Forensics' }))
      }
    }
    const [lean, ...forensics] = await parallel(runs)
    return { lean: lean || null, forensics: m.live_divergence ? forensics : null }
  },

  // 3. adversarial lenses, concurrently: all three where the numbers call
  //    for it, the abstention-burden lens alone otherwise
  async (prev, m) => {
    if (!prev.lean) return { ...prev, critiques: [] }
    const reason = fullReviewReason(prev.lean, m)
    const lenses = reason ? LENSES : LENSES.filter((l) => l.key === 'bias')
    const critiques = await parallel(lenses.map((lens) => () =>
      agent(critiqueBrief(m, lens, prev.lean, prev.forensics),
        { label: `critique:${m.mid}:${lens.key}`, phase: 'Critique', schema: CRITIQUE_SCHEMA })))
    return { ...prev, depth: reason ? 'full' : 'light', escalation_reason: reason, critiques: critiques.filter(Boolean) }
  },

  // 4. revision that has to answer them on the record
  async (prev, m) => {
    if (!prev.lean) return null
    const blocking = prev.critiques.filter((c) =>
      c.verdict !== 'stands' || (c.findings || []).some((f) => f.severity !== 'minor'))
    // an independent estimate must be reconciled on the record even when
    // no critic blocks
    const mustReconcile = Boolean(prev.forensics && prev.forensics.some(Boolean))
    const final = (blocking.length || mustReconcile)
      ? await agent(reviseBrief(m, prev.lean, prev.critiques, prev.forensics),
        { label: `revise:${m.mid}`, phase: 'Revise', schema: LEAN_SCHEMA })
      : prev.lean
    const blind = final || prev.lean
    // 5. stage 2: only now is the price shown; probabilities may change only
    //    for non-price information, written in post_price_changes
    let priced = null
    let pricing_note = null
    if (m.market && blind) {
      priced = await agent(pricingBrief(m, blind), { label: `price:${m.mid}`, phase: 'Price', schema: LEAN_SCHEMA })
      if (priced) {
        const moved = OUTCOMES.some((k) => Math.abs(priced.judgment_probs_90[k] - blind.judgment_probs_90[k]) > 1e-6)
        const why = String(priced.post_price_changes || '').trim()
        if (moved && (!why || why === '無')) {
          pricing_note = 'probabilities changed after the price with no non-price reason; reverted to the blind ones'
          const d = priced.direction in blind.judgment_probs_90 ? priced.direction : blind.direction
          priced = { ...priced, judgment_probs_90: blind.judgment_probs_90, direction: d, prob: blind.judgment_probs_90[d] }
        }
      }
    }
    return {
      mid: m.mid,
      proposed: prev.lean,
      blind,
      priced,
      pricing_note,
      final: priced || blind,
      revised: Boolean((blocking.length || mustReconcile) && final),
      blind_probs_90: blind ? blind.judgment_probs_90 : null,
      depth: prev.depth,
      escalation_reason: prev.escalation_reason,
      forensics: prev.forensics,
      critiques: prev.critiques,
      unresolved: prev.critiques.flatMap((c) => (c.findings || [])
        .filter((f) => f.severity === 'fatal')
        .map((f) => ({ lens: c.lens, claim: f.claim, why_wrong: f.why_wrong }))),
    }
  },
)

const ok = results.filter(Boolean)
log(`研判 proposals ready: ${ok.length}/${A.matches.length}. Full critique: ${ok.filter((r) => r.depth === 'full').length}, one lens: ${ok.filter((r) => r.depth === 'light').length}. Revised after critique: ${ok.filter((r) => r.revised).length}. Fixtures still carrying a FATAL finding for the session to adjudicate: ${ok.filter((r) => r.unresolved.length).map((r) => r.mid).join(', ') || 'none'}`)

return {
  proposals: ok,
  failed: A.matches.filter((m) => !ok.some((r) => r.mid === m.mid)).map((m) => m.mid),
  note: 'PROPOSALS ONLY. The calling session adjudicates and writes the row with decision/judgment.py record(); subagents must never write to the append-only store.',
}
