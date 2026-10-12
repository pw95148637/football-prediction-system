// Reusable DECISION-SUPPORT edge hunt for a live football match (POST-LOCK ONLY).
//
// NOT part of the pre-registered B1/B2 experiment — it reads the whole market
// and hunts for bettable value, which is market-derived, not the independent
// prediction command B tests. Guardrails (see
// history/falsification-2026/PROTOCOL.md CHANGELOG 2026-07-06):
//   * run ONLY after lock.py create (reading odds pre-lock CONTAMINATES the match)
//   * output is decision support (bet/pass narrative), it does NOT feed B1/B2
//     and any bet it motivates still goes through the pre-registered SOP, not this
//   * expensive (~20+ agents / ~1.5M tokens) — run on demand, not every match
//
// Invoke: Workflow({ name: 'match-edge-hunt', args: {
//   home, away, kickoff_utc, venue, neutral,           // fixture (neutral: true adds "(neutral venue)")
//   competition_label,                                 // e.g. 'EPL 2026-27 matchweek 5'; default 'a live football match'
//   known_prices: '<verbatim Pinnacle/other prices you already have, or "">',
//   model_context: '<the locked M/J probs + any known instrument bias, e.g.
//                    "naive Poisson over-confident on the favourite (Elo tail)">',
//   date: '<today YYYY-MM-DD>',
//   reference_class: '<optional: the base-rate lens reference class; default = same competition and format>',
//   repo_root: '<optional: project root, so the base-rate lens computes rates from the results database>',
//   division: '<optional: football-data division code for that database, e.g. E0, E1>' } })
// Everything in args is optional; missing pieces are gathered by the agents.

export const meta = {
  name: 'match-edge-hunt',
  description: 'POST-LOCK decision-support edge hunt for a football match: 6 lenses genuinely search for exploitable value in either direction (cross-book, matchup, models, base-rates, niche, contrarian), two-sided adversarial evaluation of each candidate, then a completeness/bias critic. Finds real edges if they exist without confirmation bias; grounded pass otherwise. NOT part of B1/B2.',
  phases: [
    { title: 'Dimensions', detail: '6 parallel lenses hunting for real edges' },
    { title: 'Evaluate', detail: 'two-sided adversarial evaluation per edge' },
    { title: 'Critique', detail: 'completeness + bias critic' },
  ],
}

const A = (typeof args === 'object' && args) ? args : {}
// home/away MUST come from real args, not a fallback placeholder: a silent
// 'HOME team'/'AWAY team' default has previously caused every lens to guess
// (and disagree on) which real fixture was meant, corrupting the whole run.
if (!A.home || !A.away) {
  throw new Error(
    `match-edge-hunt requires args as a structured object: { home, away, kickoff_utc, venue, neutral, competition_label, known_prices, model_context, date }. ` +
    `Received: ${typeof args === 'string' ? 'a string (not an object)' : JSON.stringify(args)}. ` +
    `Pass home/away as real team names — do not describe the fixture in prose inside a single string.`
  )
}
const home = A.home
const away = A.away
const fixture = `${home} vs ${away}` + (A.kickoff_utc ? `, kickoff ${A.kickoff_utc}` : '') +
  (A.venue ? `, ${A.venue}` : '') + (A.neutral === true ? ' (neutral venue)' : '')

const CTX = `LIVE ${A.competition_label || 'a live football match'}: ${fixture}. Today is ${A.date || '(date not supplied — check the current date yourself)'}. Real current-events research — use WebSearch/WebFetch; do not rely on training knowledge for time-sensitive facts.

THIS EXACT FIXTURE ONLY (${home} vs ${away}) — do not research, analyze, or report on any other match, even if a local project directory or your own tools surface other fixtures (other locked matches, other results). If you are ever unsure which match this task concerns, it is ${home} vs ${away} and no other.

KNOWN PRICES (build on these, cross-check, do not re-derive needlessly):
${A.known_prices || '(none supplied — pull current prices across books yourself)'}

INSTRUMENT/MODEL CONTEXT (respect this; do NOT anchor to it):
${A.model_context || '(none supplied — treat any single model, including a naive Elo-Poisson, as fallible; the sharpest book is usually the better estimate on core markets)'}

THE ROLE OF MARKET PRICE — this whole exercise exists to test whether a reasoned estimate built WITHOUT reference to market price can beat the market, so price has exactly two legitimate uses and no others: (1) telling you what you'd actually get paid, so you can compute EV; (2) a movement/consistency check (has the line moved, do books disagree with EACH OTHER, flagging one as a genuinely soft outlier). Price is NEVER itself evidence of what is true. Critically: multiple books/exchanges/prediction-markets agreeing with each other (Pinnacle ≈ Kalshi ≈ FanDuel ≈ Betfair) is NOT multiple independent confirmations — it is ONE signal (the market), since these sources are correlated by design (shared models, copied lines, shared public information). Never build your own "independent" probability by averaging or triangulating bookmaker/exchange prices and then treat its agreement with the market as proof there is no edge — that is circular (you have just measured the market twice) and defeats the actual point of this project. Build independent_true_prob from mechanism, facts, and base rates alone, price-blind; only after that number exists should you compare it to what the market pays.

YOUR MANDATE: genuinely HUNT for an exploitable betting edge — in EITHER direction (back the favourite OR fade it / underdog side). Treat "there is a real edge here" as a live hypothesis to prove or disprove ON EVIDENCE. Do NOT default to "market is efficient, no edge" — that is a lazy non-answer. But do NOT invent an edge to fit a story: every candidate needs a concrete mechanism + a price + YOUR independent probability estimate. Truth over confirmation.`

phase('Dimensions')

const EDGE = {
  type: 'object', additionalProperties: false,
  properties: {
    market: { type: 'string' }, side: { type: 'string' },
    best_price_seen: { type: 'number' }, book: { type: 'string' },
    independent_true_prob: { type: 'number', description: 'YOUR probability estimate (0-1), reasoned from this lens, NOT read off the price' },
    est_ev_pct: { type: 'number' }, mechanism: { type: 'string' },
    confidence_0_1: { type: 'number' }, source: { type: 'string' },
  },
  required: ['market', 'side', 'mechanism', 'independent_true_prob', 'confidence_0_1'],
}
const DIM = {
  type: 'object', additionalProperties: false,
  properties: {
    dimension: { type: 'string' }, summary: { type: 'string' },
    findings: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
      claim: { type: 'string' }, evidence: { type: 'string' }, source: { type: 'string' }, as_of: { type: 'string' } }, required: ['claim', 'source', 'as_of'] } },
    candidate_edges: { type: 'array', items: EDGE, description: 'genuinely hunt; empty ONLY if truly nothing after real effort' },
    market_read: { type: 'string', description: 'where is the market right, where might it be soft' },
  },
  required: ['dimension', 'summary', 'findings', 'candidate_edges', 'market_read'],
}

const lenses = [
  { label: 'cross-book', prompt: `${CTX}\n\nLENS 1 — CROSS-BOOK PRICING & LINE MOVEMENT. Pull current prices across many books (Pinnacle, Bet365, DraftKings, FanDuel, William Hill, Betfair exchange, 1xBet, Kalshi/Polymarket) for 1x2, Asian handicap, totals, team totals, BTTS, DNB. (a) Is any soft book beatably off the sharp de-vigged fair? (b) Has the line moved since open — steam toward the favourite or drift to the dog? (c) Does the exchange/prediction-market differ? Report the BEST price per market and flag genuine value vs the sharpest fair. Quantify.` },
  { label: 'matchup', prompt: `${CTX}\n\nLENS 2 — MATCHUP / TACTICAL EDGE. Confirmed availability (injuries/suspensions, who actually starts, a depleted line). How each side sets up (low block → suppresses goals; press/chase → opens it). Does the favourite cruise/rotate once ahead (caps margin/team-overs) or bury teams? Which specific market does the matchup mis-pricing point to? Quantify an adjusted expectation.` },
  { label: 'models', prompt: `${CTX}\n\nLENS 3 — INDEPENDENT MODELS & EXPERT PROJECTIONS. Gather Opta supercomputer, Dimers, xG-based projections, reputable expert predicted scorelines. Where do independent models CLUSTER and where does that diverge from the sharp implied distribution? Flag models that admit market inputs (discount). A naive Poisson fit to a lopsided win-prob demands a high total/margin that the favourite's actual xG may contradict — check that tension. Quantify divergences.` },
  { label: 'base-rates', prompt: `${CTX}\n\nLENS 4 — EMPIRICAL BASE RATES for this class of mismatch. Historical scoreline/total/clean-sheet/margin distributions when a comparable favourite meets a comparable underdog ${A.reference_class || 'in the SAME competition and format as this fixture (a domestic league fixture: regular-season league matches of that league, not cup knockouts; a tournament knockout: knockouts)'}.${A.repo_root ? ` Compute the rates yourself from the project's results database rather than recalling them: in ${A.repo_root}, football_ai.data.football_data_co_uk.load_seasons('${A.division || 'E0'}', range(2015, 2027), missing_ok=True) (results only; do not use its odds columns to build the rate).` : ''} How often blow out (3-0+) vs grind (1-0/2-0)? Favourite clean-sheet rate? Average total? Does the empirical distribution say the market's total/handicap is too high or too low? USE THE RIGHT REFERENCE CLASS (match the favourite's strength; don't use all-underdogs for an extreme mismatch). Cite real examples.` },
  { label: 'niche', prompt: `${CTX}\n\nLENS 5 — NICHE / DERIVATIVE markets where soft pricing hides: corners, cards/bookings (referee tendency), BTTS, first-half markets, DNB, Asian alt-lines, half-time/full-time, player props. Which specific niche market has a defensible mis-pricing and why? Get real prices. IMPORTANT: before claiming a soft-book edge, sanity-check it against the SHARP line for the same market — a public heuristic ("dominant side → corner fest") is often already priced by the sharp book. Quantify.` },
  { label: 'contrarian', prompt: `${CTX}\n\nLENS 6 — CONTRARIAN / FADE-THE-FAVOURITE / UNDERDOG-SIDE VALUE. Public money piles onto glamour favourites. (a) Is the favourite shaded short by public money (public book vs sharp/global book; Kalshi-vs-Polymarket)? (b) Underdog value: +1.5/+2.5 handicap, DNB, to-score, double chance — is the dog's line soft, or is "favourite won't blow them out" already priced (making the dog handicap a TRAP not an edge)? (c) Is the dog genuinely live? Argue the strongest honest fade case with price + true-prob. Do not force it.` },
]

// Seen in practice: a lens agent occasionally fails schema validation once
// (e.g. omits a required field), then gives up and resubmits a literal
// placeholder ("test"/"test claim") instead of retrying with a real answer.
// That placeholder passes schema validation cleanly and would otherwise be
// silently accepted as "genuinely checked, no edge found" — indistinguishable
// from a real null result unless caught explicitly.
function isDegenerate(d) {
  if (!d) return true
  const text = `${d.summary || ''} ${d.market_read || ''}`.trim().toLowerCase()
  if (text === 'test' || text.length < 15) return true
  if ((d.findings || []).some((f) => (f.claim || '').toLowerCase().includes('test claim'))) return true
  return false
}

let dims = await parallel(lenses.map((l) => () =>
  agent(l.prompt, { label: `dim:${l.label}`, phase: 'Dimensions', schema: DIM })
))
const badIdx = dims.map((d, i) => (isDegenerate(d) ? i : -1)).filter((i) => i >= 0)
if (badIdx.length) {
  log(`${badIdx.length} lens(es) returned placeholder/degenerate output (${badIdx.map((i) => lenses[i].label).join(', ')}) — retrying once.`)
  const retries = await parallel(badIdx.map((i) => () =>
    agent(lenses[i].prompt, { label: `dim:${lenses[i].label}:retry`, phase: 'Dimensions', schema: DIM })
  ))
  badIdx.forEach((i, k) => { dims[i] = retries[k] })
}
const stillBad = dims.filter(isDegenerate).length
if (stillBad) log(`${stillBad} lens(es) still degenerate after retry — excluded from results, NOT counted as "checked, no edge".`)
dims = dims.filter((d) => d && !isDegenerate(d))

const allEdges = []
for (const d of dims) for (const e of (d.candidate_edges || [])) allEdges.push({ ...e, from: d.dimension })
log(`Dimensions done. ${allEdges.length} candidate edges surfaced across ${dims.length} lenses.`)

phase('Evaluate')

const EVAL = {
  type: 'object', additionalProperties: false,
  properties: {
    edge_label: { type: 'string' },
    case_for: { type: 'string' }, case_against: { type: 'string' },
    independent_prob_basis: { type: 'string', description: 'What independent_true_prob was built FROM — name the mechanism/facts/base-rates. If you cannot state this without mentioning bookmaker or exchange prices, you built it from the market, not independently — say so explicitly here, and know that this disqualifies "already priced" as a valid case_against and caps the verdict at INSUFFICIENT_EVIDENCE.' },
    independent_true_prob: { type: 'number' }, price_referenced: { type: 'number' },
    ev_estimate_pct: { type: 'number' },
    verdict: { type: 'string', enum: ['REAL_EDGE', 'MARGINAL', 'PRICED', 'INSUFFICIENT_EVIDENCE'] },
    confidence_0_1: { type: 'number' }, reasoning: { type: 'string' },
  },
  required: ['edge_label', 'case_for', 'case_against', 'independent_prob_basis', 'verdict', 'confidence_0_1', 'reasoning'],
}

const evals = allEdges.length ? (await parallel(allEdges.slice(0, 20).map((e) => () =>
  agent(`${CTX}\n\nTWO-SIDED ADVERSARIAL EVALUATION of ONE candidate betting edge. Be a fair judge — NOT a default-refuter and NOT a cheerleader. Build the STRONGEST honest case FOR it being real, and the STRONGEST case AGAINST. Verify prices and key facts with fresh web research. Watch for: a convenient price asserted exactly at the EV-positive threshold, motivated false facts, and a wrong reference class.\n\nON "ALREADY PRICED" — this is the most misused verdict, guard it carefully. A VALID already-priced argument names a specific reason the market would already reflect this exact fact: the information has been public long enough for a liquid sharp book to digest it, the mechanism is one professional traders are known to model closely (e.g. corner trading), or a cited price cannot be reproduced from a real source. An INVALID already-priced argument is: "I averaged/cross-checked several bookmakers or exchanges and they cluster together, therefore the market is right." Bookmakers and exchanges agreeing with each other is the market agreeing with itself — it is not a second, independent opinion, no matter how many books you check. If your reasoning for case_against amounts to that, you have not evaluated the candidate, you have re-measured the market; say so in independent_prob_basis and mark the verdict INSUFFICIENT_EVIDENCE rather than PRICED.\n\nGive YOUR independent true-probability, built price-blind from mechanism/facts/base-rates (see independent_prob_basis), and a calibrated verdict — REAL_EDGE only if, at a real obtainable price, a bettor has genuine positive expectation you'd stake on.\n\nCandidate (from lens "${e.from}"):\n${JSON.stringify(e, null, 2)}`,
    { label: `eval:${(e.market || 'edge').slice(0, 24)}`, phase: 'Evaluate', schema: EVAL })
))).filter(Boolean) : []

phase('Critique')

const critique = await agent(
  `Completeness & bias critic for a ${fixture} edge hunt. Dimensions and adversarial evaluations:\n\nDIMENSIONS:\n${JSON.stringify(dims.map((d) => ({ dimension: d.dimension, summary: d.summary, market_read: d.market_read, edges: d.candidate_edges })), null, 2)}\n\nEVALUATIONS:\n${JSON.stringify(evals, null, 2)}\n\nConcisely: (1) What decision-relevant dimension/market was NOT investigated (live/in-play? draw-in-90? player props?). (2) Any lens with confirmation bias (forcing a favourite edge) OR the lazy opposite (dismissing all as "efficient" without work)? Flag convenient-unverified-prices and false facts. (3) CIRCULARITY CHECK — for every PRICED verdict, read its independent_prob_basis: if the "independent" probability was actually built by averaging/triangulating bookmaker or exchange prices (i.e. it is the market measured a second time, not a real independent estimate), the PRICED verdict is invalid — flag it explicitly as a circularity failure, not a genuine rejection. (4) Which surviving REAL_EDGE/MARGINAL verdicts are best-supported vs shaky? Note if the survivors are the SAME correlated bet (size the cluster, not each). (5) Net: is there a genuinely bettable edge in either direction, and the single best-supported bet if any — and remind that ANY soft-book edge must be checked against the real sharp price before staking.`,
  { label: 'critique', phase: 'Critique' }
)

return { dimensions: dims, evaluations: evals, critique }
