// Runs .claude/workflows/judgment-synthesis.js with agent() replaced by a
// recorder, so the prompts and the depth decision come from the workflow's
// own code (the Workflow tool needs an explicit owner request; the skill says
// to follow the same architecture with the Agent tool).
//
// usage: node ops/workflow_harness.mjs .claude/workflows/judgment-synthesis.js <brief.json> <responses.json|-> <outDir> [mid,mid,...]
// Each run writes the prompts still owed (labels with ':' -> '__') and
// prints the pending labels; dispatch those with the Agent tool, add each
// structured result to responses.json under its label, and run again.
// responses.json maps agent labels (synth:<mid>, critique:<mid>:<lens>,
// revise:<mid>) to the structured objects those agents returned.
import fs from 'fs'

const [, , wfPath, briefPath, respPath, outDir, only] = process.argv
let src = fs.readFileSync(wfPath, 'utf8').replace('export const meta', 'const meta')
src = src.replace('const biasBlock', '__out.schemas = { LEAN_SCHEMA, CRITIQUE_SCHEMA }\nconst biasBlock')
const brief = JSON.parse(fs.readFileSync(briefPath, 'utf8'))
let A = brief.args
if (only) {
  const keep = only.split(',')
  A = { ...A, matches: keep.map((mid) => A.matches.find((m) => m.mid === mid)).filter(Boolean) }
}
const responses = respPath !== '-' && fs.existsSync(respPath)
  ? JSON.parse(fs.readFileSync(respPath, 'utf8')) : {}
const recorded = []
const __out = {}
const g = {
  args: A,
  __out,
  log: (s) => console.error('[workflow log]', s),
  parallel: async (fns) => Promise.all(fns.map((f) => f())),
  pipeline: async (items, ...stages) => Promise.all(items.map(async (m) => {
    let v = await stages[0](m)
    for (const s of stages.slice(1)) v = await s(v, m)
    return v
  })),
  agent: async (prompt, opts) => {
    recorded.push({ label: opts.label, phase: opts.phase, prompt })
    return Object.prototype.hasOwnProperty.call(responses, opts.label) ? responses[opts.label] : null
  },
}
const fn = new Function(...Object.keys(g), `return (async () => {\n${src}\n})()`)
const result = await fn(...Object.values(g))
fs.mkdirSync(outDir, { recursive: true })
fs.writeFileSync(`${outDir}/schemas.json`, JSON.stringify(__out.schemas, null, 1))
for (const r of recorded) {
  if (Object.prototype.hasOwnProperty.call(responses, r.label)) continue // already answered
  fs.writeFileSync(`${outDir}/${r.label.replace(/:/g, '__')}.txt`, r.prompt)
}
console.log(JSON.stringify({
  pending: recorded.filter((r) => !Object.prototype.hasOwnProperty.call(responses, r.label)).map((r) => r.label),
  proposals: (result.proposals || []).map((p) => ({ mid: p.mid, depth: p.depth, escalation_reason: p.escalation_reason, revised: p.revised, unresolved: p.unresolved, pricing_note: p.pricing_note, final_probs: p.final && p.final.judgment_probs_90, blind_probs: p.blind_probs_90 })),
}, null, 1))
