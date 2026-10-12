"""Trading-arm evaluation: paper-bet CLV + the shadow-bet derived metric.

Two ledgers, two questions (Architecture Review v3 §4.5):
- PAPER BETS (pre-registered B2 discipline, from the store): did the bets
  the SOP allowed through beat the close? The authoritative B2 for the live
  WC2026 sample stays in wc2026/score.py until cutover — this view must
  agree with it, it does not replace it.
- SHADOW BETS (mechanical rule, no discretion, EXPLORATORY — never B2):
  at the first post-J-lock non-closing snapshot, take J's largest-gap side.
  Comparing shadow CLV against paper CLV is what answers "does the
  two-questions gate filter out signal or noise?" — the B2-starvation fix
  (review §2.2) that needs no new pre-registration because it is derived,
  labelled, and kept off the registered ledgers.

research_eval equivalent: `python -m football_ai.research.scoring` (the
stratified B1 + recompute gate) — kept in research/ so the research arm
stays self-contained; this module is the trading side only.
"""
import json

from football_ai.market.clv import closing_snapshot, clv, shadow_bets
from football_ai.store import io as store_io


def _locks(era):
    groups = {}
    for rec in store_io.read("locks", era=era):
        groups.setdefault(rec["match_id"], []).append(rec["data"])
    for docs in groups.values():
        docs.sort(key=lambda d: d["version"])
    return groups


def _odds(era):
    out = {}
    for rec in store_io.read("odds", era=era):
        out.setdefault(rec["match_id"], []).append(rec["data"])
    return out


def _results(era):
    return {rec["data"]["match_id"]: rec["data"]
            for rec in store_io.read("results", era=era)}


def paper_bet_rows(era="v2-wc2026"):
    """CLV per recorded paper bet (1x2/qualify only need selection fair;
    ah/totals lines follow the snapshot conventions ported in market.clv)."""
    locks = _locks(era)
    odds = _odds(era)
    results = _results(era)
    rows = []
    for rec in store_io.read("bets", era=era):
        bet = rec["data"]
        mid = bet["match_id"]
        versions = locks.get(mid, [])
        snaps = odds.get(mid, [])
        first_fetch = min((s["fetched_at"] for s in snaps), default=None)
        contaminated = bool(versions) and first_fetch is not None and \
            versions[0]["locked_at"] > first_fetch
        kickoff = versions[0]["payload"]["kickoff_utc"] if versions else None
        row = {"bet": bet, "excluded": contaminated,
               "pilot": bool(versions and
                             versions[0]["payload"].get("pilot", False))}
        if mid not in results or "score90" not in results[mid]:
            row["clv"] = None
            row["note"] = "pending (match not played yet)"
            rows.append(row)
            continue
        line = bet.get("line")
        if bet["market"] == "ah" and bet["selection"] == "away" \
                and line is not None:
            line = -line
        close = closing_snapshot(snaps, bet["market"], line=line,
                                 kickoff_utc=kickoff)
        if close is not None and not close.get("closing"):
            close = None
        if close is None:
            row["clv"] = None
            row["note"] = "no flagged closing snapshot for this market/line"
        else:
            row["clv"] = clv(bet["odds_taken"], bet["selection"], close)
            row["beat_close"] = row["clv"] is not None and row["clv"] > 0
        rows.append(row)
    return rows


def shadow_rows(era="v2-wc2026"):
    locks = _locks(era)
    odds = _odds(era)
    kickoffs = {mid: docs[0]["payload"]["kickoff_utc"]
                for mid, docs in locks.items()}
    # pilots stay out of even the exploratory view's headline: the shadow
    # metric exists to be compared against B2, whose sample is main-only
    main_locks = {mid: docs for mid, docs in locks.items()
                  if not docs[0]["payload"].get("pilot", False)}
    return shadow_bets(main_locks, odds, kickoffs)


def summarize(era="v2-wc2026"):
    paper = [r for r in paper_bet_rows(era) if not r["pilot"]]
    paper_valid = [r for r in paper
                   if not r["excluded"] and r.get("clv") is not None]
    shadows = shadow_rows(era)
    placed = [s for s in shadows if not s.get("skipped")]
    with_clv = [s for s in placed if s.get("clv") is not None]
    return {
        "era": era,
        "paper": {
            "n_bets": len(paper),
            "n_clv_valid": len(paper_valid),
            "beat_close_n": sum(1 for r in paper_valid if r.get("beat_close")),
            "mean_clv": (sum(r["clv"] for r in paper_valid) / len(paper_valid)
                         if paper_valid else None),
        },
        "shadow": {
            "n_matches_eligible": len(shadows),
            "n_placed": len(placed),
            "n_clv_valid": len(with_clv),
            "beat_close_n": sum(1 for s in with_clv if s.get("beat_close")),
            "mean_clv": (sum(s["clv"] for s in with_clv) / len(with_clv)
                         if with_clv else None),
            "skip_reasons": {s["reason"]: sum(
                1 for x in shadows
                if x.get("skipped") and x["reason"] == s["reason"])
                for s in shadows if s.get("skipped")},
        },
        "label": ("shadow = mechanical derived metric, EXPLORATORY, never "
                  "B2; paper = SOP-gated pre-registered ledger. Their gap "
                  "is the value/noise content of the two-questions gate."),
    }


def _selfcheck():
    """Structural check on a synthetic era (heavy hand-computed cases live
    in market.clv._selfcheck; here we verify the store wiring + pilot
    exclusion)."""
    import tempfile
    from pathlib import Path
    fails = []
    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            lock = {"locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
                    "payload": {"match_id": "m_t", "pilot": False,
                                "kickoff_utc": "2026-01-03T00:00:00+00:00",
                                "probs_90": {"home": 0.5, "draw": 0.3,
                                             "away": 0.2},
                                "j": {"probs_90": {"home": 0.6, "draw": 0.25,
                                                   "away": 0.15}}}}
            pilot = {"locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
                     "payload": {"match_id": "m_p", "pilot": True,
                                 "kickoff_utc": "2026-01-03T00:00:00+00:00",
                                 "probs_90": {"home": 0.5, "draw": 0.3,
                                              "away": 0.2},
                                 "j": {"probs_90": {"home": 0.6,
                                                    "draw": 0.25,
                                                    "away": 0.15}}}}
            snaps = [
                {"match_id": "m_t", "book": "pinnacle", "market": "1x2",
                 "line": None, "outcomes": ["home", "draw", "away"],
                 "odds": [2.0, 3.0, 6.0], "closing": False,
                 "fetched_at": "2026-01-02T00:00:00+00:00"},
                {"match_id": "m_t", "book": "pinnacle", "market": "1x2",
                 "line": None, "outcomes": ["home", "draw", "away"],
                 "odds": [1.9, 3.2, 6.5], "closing": True,
                 "fetched_at": "2026-01-02T23:00:00+00:00"},
            ]
            store_io.rebuild_era("locks", "t", [
                {"data": lock, "match_id": "m_t", "version": 1},
                {"data": pilot, "match_id": "m_p", "version": 1}])
            store_io.rebuild_era("odds", "t", [
                {"data": s, "match_id": s["match_id"]} for s in snaps])
            store_io.rebuild_era("results", "t", [
                {"data": {"match_id": "m_t", "score90": [1, 0]}}])
            store_io.rebuild_era("bets", "t", [
                {"data": {"match_id": "m_t", "book": "pinnacle",
                          "market": "1x2", "line": None,
                          "selection": "home", "odds_taken": 2.0,
                          "stake_u": 1.0}}])
            s = summarize("t")
            if s["paper"]["n_clv_valid"] != 1:
                fails.append(f"paper CLV wiring: {s['paper']}")
            if s["shadow"]["n_placed"] != 1:
                fails.append(f"shadow wiring: {s['shadow']}")
            if s["shadow"]["n_matches_eligible"] != 1:
                fails.append("pilot lock must be excluded from shadow view")
        finally:
            store_io.STORE_DIR = old
    return fails


def main():
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
    print("\nper-shadow-bet detail:")
    for s in shadow_rows():
        print(f"  {json.dumps(s, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
