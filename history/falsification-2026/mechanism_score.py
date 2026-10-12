"""Mechanism-type scoreboard: does a CATEGORY of J deviation actually help?

Why this exists: scoring only the aggregate (score.py's B1) hides that every
match so far has tested a DIFFERENT, bespoke, one-off J mechanism (public-money
premium, rotation-discount recovery, rest/travel...) -- each n=1, never
revisited. That is not "16 tests of one system," it is 16 different untested
hypotheses. This groups J's deviations from M by a FIXED taxonomy so the same
mechanism TYPE can accumulate its own track record across matches, gated by
a MIN_N threshold so a type is never declared "works" from a handful of
matches (below MIN_N everything prints as informational only).

Taxonomy (append new types here as they appear; do not invent a new type per
match -- if nothing fits, use "other" and reconsider the taxonomy, don't just
tag-and-forget):
  no_deviation                       - J=M, deviation considered and declined
  rotation_discount_recovery         - Elo distorted by a prior deliberately-weakened lineup
  roster_era_distortion              - rating lags real current squad strength (aging core, transition)
  rest_travel                        - rest-day / travel-distance / venue-familiarity asymmetry
  injury_replacement                 - a specific starter is out, understudy quality assessed
  illness_camp                       - camp illness/injury doubt on a player expected to start
  tactical_stylistic_matchup         - playing-style clash (press vs press-resistance, low-block, etc.)
  cross_confederation_elo_uncertainty - rating-scale comparability across confederations
  incentive_rotation                 - asymmetric squad-rotation/motivation signals (dead-rubber, farewell match,
                                        individual-award incentive) not yet absorbed by a lineup-confirmed price;
                                        added 2026-07-17 for m103_fra_eng, first use (was being tagged in prose
                                        without a ledger entry -- see PROTOCOL.md CHANGELOG 2026-07-17)
  other                              - does not fit above; flag for taxonomy review, don't silently bucket

A match can carry more than one type when J bundled several concrete facts
into one un-decomposed adjustment (see m96_sui_col) -- it counts toward every
type's tally, since the ledger cannot honestly attribute the bundled magnitude
to one fact alone.
"""
import json
from pathlib import Path

BASE = Path(__file__).parent
LEDGER = BASE / "mechanism_ledger.jsonl"
REPORT = BASE / "report.json"

MIN_N = 5  # below this, no "this mechanism works/fails" claim (informational only)


def load_ledger():
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_b1():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    return {row["match_id"]: row for row in report.get("b1", report.get("B1", []))}


def main():
    ledger = load_ledger()
    b1 = load_b1()

    by_type = {}
    for entry in ledger:
        row = b1.get(entry["match_id"])
        if row is None or row.get("status") != "scored":
            status = "unscored" if row is None else row.get("status")
            print(f"  {entry['match_id']:<16} {','.join(entry['mechanism_types']):<45} "
                  f"-- {status}, not counted yet")
            continue
        if "j_rps" not in row:
            print(f"  {entry['match_id']:<16} {','.join(entry['mechanism_types']):<45} "
                  f"-- scored but no j_rps (J=M with no separate field, or M-only lock)")
            continue
        model_rps, j_rps = row["model_rps"], row["j_rps"]
        hit = j_rps < model_rps          # did the deviation from M help, on this match?
        delta = model_rps - j_rps        # positive = J beat M
        for t in entry["mechanism_types"]:
            by_type.setdefault(t, []).append({
                "match_id": entry["match_id"], "hit": hit, "delta": delta,
                "j_rps": j_rps, "model_rps": model_rps,
            })

    print("\n=== per-match ===")
    print("(see above for unscored/no-j-field rows)")

    print("\n=== per mechanism type (J vs M, RPS lower=better) ===")
    for t, rows in sorted(by_type.items()):
        n = len(rows)
        hits = sum(r["hit"] for r in rows)
        mean_delta = sum(r["delta"] for r in rows) / n
        matches = ", ".join(r["match_id"] for r in rows)
        gate = "" if n >= MIN_N else f"  ** n<{MIN_N}: NO PATTERN CLAIM, informational only **"
        print(f"  {t:<38} n={n}  J-better={hits}/{n}  mean RPS delta={mean_delta:+.4f}"
              f"  [{matches}]{gate}")

    scored_types = sum(1 for rows in by_type.values() if len(rows) >= MIN_N)
    if scored_types == 0:
        print(f"\nNo mechanism type has reached MIN_N={MIN_N} yet -- every row above is "
              f"informational only. This is expected mid-tournament; do not read a "
              f"per-type hit/miss as validation or refutation until n>={MIN_N}.")


if __name__ == "__main__":
    main()
