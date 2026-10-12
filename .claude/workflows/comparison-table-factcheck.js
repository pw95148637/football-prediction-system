// WC2026 comparison-table fact-check (generalized from the m104 2026-07-16 run).
// WORLD CUP 2026 ONLY: every data path below points into the frozen archive
// `history/falsification-2026/` (results.json, locks/, odds/, verdicts_main.md,
// judgment_ledger.jsonl). It cannot verify an EPL or any other live-competition
// table, whose data lives in the store.
// Adversarially verifies every number in a generated comparison HTML against
// real source files and independent recomputation. Called from the
// wc2026-comparison-table skill's Step 3 — do not run standalone without
// having actually generated the HTML first.
//
// Invoke:
// Workflow({ name: 'comparison-table-factcheck', args: {
//   html_path: 'C:\\...\\reports\\m104_final_comparison.html',   // absolute path
//   match_id: 'm104_esp_arg',
//   home: 'Spain', away: 'Argentina',
//   source_files: [                                              // whatever the
//     { label: 'home forensics', path: 'C:\\...\\home_forensics.md' },   // team
//     { label: 'away forensics', path: 'C:\\...\\away_forensics.md' },   // forensics
//     { label: 'critique', path: 'C:\\...\\critique.md' },               // produced
//   ],                                                            // in this match's
//   repo_root: 'C:\\path\\to\\wc2026',
//   extra_note: '<anything project-specific the checker should know, optional>',
// } })

export const meta = {
  name: 'comparison-table-factcheck',
  description: 'Adversarially verify every number in a wc2026 match comparison HTML against real source files and independent recomputation',
  phases: [
    { title: 'Verify', detail: '3 parallel lenses: team-stats provenance, market-math recomputation, citation/narrative accuracy' },
    { title: 'Synthesize', detail: 'consolidate flags into a fix list' },
  ],
}

const A = (typeof args === 'object' && args) ? args : {}
if (!A.html_path || !A.match_id || !A.source_files || !A.repo_root) {
  throw new Error(
    `comparison-table-factcheck requires args: { html_path, match_id, home, away, source_files:[{label,path}], repo_root }. ` +
    `Received: ${typeof args === 'string' ? 'a string (not an object)' : JSON.stringify(args)}.`
  )
}

const fixture = `${A.home || '(home)'} vs ${A.away || '(away)'}`
const ARCHIVE = `${A.repo_root}\\history\\falsification-2026`
const sourceList = A.source_files.map((s) => `- ${s.label}: ${s.path}`).join('\n')

phase('Verify')

const lenses = [
  {
    key: 'team-stats',
    prompt: `You are a hostile fact-checker, not a copy-editor. Read the HTML file at ${A.html_path} — it is a data comparison table for a football decision-support report (${fixture}, wc2026 project). Your job: verify EVERY numeric claim in the "team forensics" section(s) — per-match xG/goal tables, player-level stat lines, multiplier tables — against real source files on disk. Do NOT trust that the HTML is correct — your default assumption is that any number could be a hallucination until you personally confirm it traces to a source.

Source files to check against (all real files produced for this match's judgment/研判 process):
${sourceList}
Also cross-check at least 3 of the per-match xG/goal numbers directly against ${ARCHIVE}\\results.json (the project's own match-by-match database) — do not rely solely on the forensics report files, they could themselves contain an error.
${A.extra_note ? `\nAdditional context: ${A.extra_note}` : ''}

For EACH numeric claim in the team-forensics tables and stat lines, report: (a) the exact number as it appears in the HTML, (b) whether you found it in a source file verbatim or derivable from one, (c) VERDICT: CONFIRMED / DISCREPANCY (number differs from source — state the source's actual number) / UNSOURCED (no basis found — treat as a likely hallucination until proven otherwise). Also do basic arithmetic sanity checks on any totals/sums shown (e.g. does a "total" row actually equal the sum of the rows above it) — a number can be verbatim-sourced AND still internally inconsistent; flag that too.

Report back a structured list of every DISCREPANCY, UNSOURCED, and internal-arithmetic finding (skip listing CONFIRMED ones individually, just give a count).`,
  },
  {
    key: 'market-math',
    prompt: `You are a hostile fact-checker verifying a computed probability table, not a copy-editor. Read the HTML file at ${A.html_path} — focus on the market/probability comparison table(s) and any instrument-comparison section (the locked M/J numbers vs the 研判/judgment numbers vs market fair). Your job: INDEPENDENTLY RECOMPUTE every probability and EV/划算度 number from scratch using the project's own real code in ${A.repo_root}, and flag any mismatch. Do not trust the HTML's arithmetic — verify it by actually running the code yourself (Bash tool, python, cd to the repo root first).

Steps:
1. Read the locked instrument file(s) under ${ARCHIVE}\\locks\\${A.match_id}*.json directly — confirm the HTML's stated M and J probabilities/qualify numbers match the lock file bit-for-bit (allowing display rounding).
2. Identify what attack/defense multipliers (or equivalent adjustment) the HTML claims were used for the 研判/judgment layer — trace these to the critique/forensics source file(s) listed here, don't take the HTML's own claim at face value:
${sourceList}
3. Recompute base lambdas via \`model.predict_lambdas(<elo diff>)\` (the archived model.py: \`sys.path.insert(0, 'history/falsification-2026')\` first), apply the multipliers, build the Dixon-Coles grid via \`football_ai.models.m_dc.score_matrix\`, and reproduce every market cell using \`football_ai.decision.report._grid_1x2\`, \`_grid_totals\`, \`_grid_ah_home\`, \`_top_scores\`, and \`dc_rho()\` for rho — call these functions directly, don't hand-derive.
4. Read the real odds/prices from ${ARCHIVE}\\odds\\${A.match_id}.jsonl directly and confirm the HTML's quoted prices match. For any market NOT in that file (the HTML should mark these as user-screenshot or non-mechanical sourced — check that it actually does, and does not silently present them with the same confidence as the mechanical snapshot).
5. Recompute EV/划算度 for every priced outcome using \`football_ai.decision.sizing.ev(prob, price)\` and \`football_ai.market.devig.devig_shin\`/\`devig_both\` for the market-fair de-vig, and compare every number to the HTML.

Report: for every number you recomputed, CONFIRMS or DISCREPANCY (your value vs the HTML's value, and the magnitude). Flag anything you could not reproduce as UNVERIFIABLE, and flag any case where the HTML presents a non-mechanically-sourced price (search-aggregated, no snapshot file) as if it had the same precision as a real snapshot — that is a presentation problem even if the underlying number turns out to be roughly right.`,
  },
  {
    key: 'narrative',
    prompt: `You are a hostile fact-checker verifying narrative/citation claims, not a copy-editor. Read the HTML file at ${A.html_path} — focus on any prose that presents itself as summarizing or quoting a source document (e.g. a box describing a methodological finding, a disclosed divergence, a quoted rationale from a lock file's basis).

Source files available for this match:
${sourceList}
Also check ${ARCHIVE}\\verdicts_main.md (search for this match's own section — do NOT load the whole file, it's large) and ${ARCHIVE}\\judgment_ledger.jsonl (search for this match_id) if the HTML references either.
${A.extra_note ? `\nAdditional context: ${A.extra_note}` : ''}

For each such claim: read the source directly and report CONFIRMED (quote what you actually found) or DISCREPANCY/UNSOURCED (state what you found instead). Specifically check whether anything presented in quote marks (「」 or "") is verbatim from the source or is a paraphrase dressed up as a direct quote — if the HTML drops words/clauses from a real quote without an ellipsis, flag that as a presentation issue even though the underlying content is accurate.`,
  },
]

const results = await parallel(lenses.map((l) => () =>
  agent(l.prompt, { label: `verify:${l.key}`, phase: 'Verify' })
))

phase('Synthesize')

const synth = await agent(
  `You are consolidating three independent fact-check reports on a wc2026 football-analytics HTML comparison table (${A.html_path}, match ${A.match_id}) into a single fix list for the report's author.

TEAM-STATS PROVENANCE CHECK:
${results[0] || '(failed)'}

---

MARKET-MATH RECOMPUTATION CHECK:
${results[1] || '(failed)'}

---

NARRATIVE/CITATION CHECK:
${results[2] || '(failed)'}

---

Produce: (1) a list of every CONFIRMED-DISCREPANCY or UNSOURCED/hallucination flag across all three reports, each with: what the HTML says, what the source/recomputation actually shows, and which HTML section/table cell to fix. (2) An overall verdict: is this report safe to hand to the user as "real data, not hallucination", or does it need fixes first? If there are zero discrepancies, say so plainly and don't manufacture false positives.`,
  { label: 'synthesize', phase: 'Synthesize' }
)

return { lensResults: { teamStats: results[0], marketMath: results[1], narrative: results[2] }, synthesis: synth }
