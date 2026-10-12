"""Pre-registered J+研判 combination rule scoreboard (STACKING_PREREGISTRATION.md).

combined_probs_90 = normalize(0.5 * J_probs_90 + 0.5 * research_probs_90)

Derives everything mechanically from existing files -- no manual re-entry:
  - locks/<id>.v2.json (or highest version)  -> J
  - judgment_ledger.jsonl (latest non-resolution entry per match_id) -> research
  - results.json -> actual outcome
  - odds/<id>.jsonl (closing snapshot) -> market fair (Shin de-vig)

Decision-support only: never feeds B1/B2, never rewrites locks. See
STACKING_PREREGISTRATION.md for why the rule is a plain 50/50 average and
why the sample size this tournament (<=4) cannot support a verdict either way.
"""
import json
from pathlib import Path

from devig import devig_shin
from model import rps

BASE = Path(__file__).parent
LOCKS_DIR = BASE / "locks"
ODDS_DIR = BASE / "odds"
RESULTS = BASE / "results.json"
LEDGER = BASE / "judgment_ledger.jsonl"

MIN_N_FOR_ANY_CLAIM = 5  # same bar as mechanism_score.py; this tournament tops out below it


def load_ledger():
    rows = []
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def latest_research(match_id, ledger):
    candidates = [r for r in ledger
                  if r.get("match_id") == match_id and r.get("kind") != "resolution"]
    return candidates[-1] if candidates else None


def load_lock_j(match_id):
    base = LOCKS_DIR / f"{match_id}.json"
    if not base.exists():
        return None
    versions = [json.loads(base.read_text(encoding="utf-8"))]
    for vf in sorted(LOCKS_DIR.glob(f"{match_id}.v*.json")):
        versions.append(json.loads(vf.read_text(encoding="utf-8")))
    versions.sort(key=lambda d: d["version"])
    final = versions[-1]["payload"]
    j = final.get("j")
    m = final["probs_90"]
    return {
        "m_probs": [m["home"], m["draw"], m["away"]],
        "j_probs": ([j["probs_90"]["home"], j["probs_90"]["draw"], j["probs_90"]["away"]]
                    if j else None),
    }


def closing_market(match_id):
    p = ODDS_DIR / f"{match_id}.jsonl"
    if not p.exists():
        return None
    snaps = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    cands = [s for s in snaps if s["market"] == "1x2" and s["book"] == "pinnacle"]
    closers = [s for s in cands if s.get("closing")]
    use = closers if closers else cands
    if not use:
        return None
    snap = max(use, key=lambda s: s["fetched_at"])
    fair = dict(zip(snap["outcomes"], devig_shin(snap["odds"])))
    return [fair["home"], fair["draw"], fair["away"]]


def outcome_index(score90):
    h, a = score90
    return 0 if h > a else (2 if h < a else 1)


def combine(j_probs, research_probs):
    avg = [(j_probs[i] + research_probs[i]) / 2.0 for i in range(3)]
    s = sum(avg)
    return [x / s for x in avg]


def main():
    results = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    ledger = load_ledger()

    match_ids = sorted({e["match_id"] for e in ledger if e.get("kind") != "resolution"})

    print("=== combined (0.5*J + 0.5*研判) vs J alone, 研判 alone, market ===")
    print("    (decision-support only; never touches B1/B2 or locks)\n")

    rows = []
    for mid in match_ids:
        research_entry = latest_research(mid, ledger)
        lock = load_lock_j(mid)
        res = results.get(mid)
        market = closing_market(mid)

        if research_entry is None or lock is None or lock["j_probs"] is None:
            print(f"  {mid}: skipped (missing research entry or J)")
            continue
        if res is None or "score90" not in res:
            print(f"  {mid}: pending (not yet played/settled)")
            continue

        j_probs = lock["j_probs"]
        research_probs = [research_entry["judgment_probs_90"]["home"],
                           research_entry["judgment_probs_90"]["draw"],
                           research_entry["judgment_probs_90"]["away"]]
        combined_probs = combine(j_probs, research_probs)
        idx = outcome_index(res["score90"])

        row = {
            "match_id": mid,
            "j_rps": rps(j_probs, idx),
            "research_rps": rps(research_probs, idx),
            "combined_rps": rps(combined_probs, idx),
        }
        if market is not None:
            row["market_rps"] = rps(market, idx)
        rows.append(row)

        mtxt = f"  market={row['market_rps']:.4f}" if "market_rps" in row else ""
        print(f"  {mid}: J={row['j_rps']:.4f}  研判={row['research_rps']:.4f}  "
              f"combined={row['combined_rps']:.4f}{mtxt}")

    print(f"\nn={len(rows)} scored")
    if rows:
        j_mean = sum(r["j_rps"] for r in rows) / len(rows)
        r_mean = sum(r["research_rps"] for r in rows) / len(rows)
        c_mean = sum(r["combined_rps"] for r in rows) / len(rows)
        c_beats_j = sum(1 for r in rows if r["combined_rps"] < r["j_rps"])
        c_beats_r = sum(1 for r in rows if r["combined_rps"] < r["research_rps"])
        print(f"  mean RPS: J={j_mean:.4f}  研判={r_mean:.4f}  combined={c_mean:.4f}")
        print(f"  combined beat J in {c_beats_j}/{len(rows)}, "
              f"beat 研判 in {c_beats_r}/{len(rows)}")

    if len(rows) < MIN_N_FOR_ANY_CLAIM:
        print(f"\n  ** n={len(rows)} < MIN_N={MIN_N_FOR_ANY_CLAIM}: NO CLAIM that stacking "
              f"helps or hurts, by design (STACKING_PREREGISTRATION.md). This tournament "
              f"has at most 4 matches with both J and 研判 -- structurally cannot reach "
              f"MIN_N. Directional numbers above are informational only. **")


if __name__ == "__main__":
    main()
