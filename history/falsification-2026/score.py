"""B1/B2 scoring report + anchoring audit (PROTOCOL.md section 4).

B1: per-match RPS/Brier of the locked model 1X2 probabilities vs the
    Pinnacle closing de-vigged (Shin) probabilities. All sample matches count.
B2: paper-bet CLV — beat-close rate vs Pinnacle closing fair price.
    P&L is settled and shown but is informational only (not a criterion).

Audit: a match whose v1 lock is later than its first odds fetch is
CONTAMINATED — still scored for B1, excluded from B2. Amended locks are
scored alongside v1, with an anchoring-drift check (did the amendment move
toward the market?).

Bet ledger (bets.jsonl), one JSON object per line:
  {"match_id", "book", "market", "line", "selection", "odds_taken",
   "taken_at", "stake_u", "note"}
For "ah" bets, "line" is the line of the SELECTION's side (an away bet at
+0.75 stores +0.75; the matching home-line snapshot is -0.75).
"""
import json
from pathlib import Path

from devig import devig_shin
from model import rps, brier

BASE = Path(__file__).parent
LOCKS_DIR = BASE / "locks"
ODDS_DIR = BASE / "odds"
RESULTS = BASE / "results.json"
BETS = BASE / "bets.jsonl"
REPORT = BASE / "report.json"

SAMPLE_SIZE = 16  # pre-registered: matches 89-104
# Registered-MISSING matches (PROTOCOL CHANGELOG 2026-07-06): can never be
# locked, so the main sample tops out at SAMPLE_SIZE - len(REGISTERED_MISSING)
# scored matches. The section-4 thresholds are defined on 16; when every
# LOCKABLE main match is settled the report prints the section-4 statistics
# and direction but defers the formal verdict to the final report, which
# must list the missing match (Architecture Review v3, Migration Phase 0(a)).
REGISTERED_MISSING = ["m92_mex_eng"]


def load_locks(locks_dir=LOCKS_DIR):
    """{match_id: {"v1": doc, "final": doc, "versions": [docs], "n_versions": int}}"""
    out = {}
    if not locks_dir.exists():
        return out
    for f in sorted(locks_dir.glob("*.json")):
        if ".v" in f.name:
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        mid = doc["payload"]["match_id"]
        versions = [doc]
        for vf in sorted(locks_dir.glob(f"{mid}.v*.json")):
            versions.append(json.loads(vf.read_text(encoding="utf-8")))
        versions.sort(key=lambda d: d["version"])
        out[mid] = {"v1": versions[0], "final": versions[-1],
                    "versions": versions, "n_versions": len(versions)}
    return out


def load_odds(match_id, odds_dir=ODDS_DIR):
    path = odds_dir / f"{match_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def closing_snapshot(snaps, market="1x2", line=None, book="pinnacle",
                     kickoff_utc=None):
    """Prefer explicit closing snapshots; else the last fetch before kickoff.
    book=None accepts any bookmaker (used as fallback when no Pinnacle close
    was captured — the scored row records which book was actually used)."""
    def line_match(s):
        if line is None and s.get("line") is None:
            return True
        if line is None or s.get("line") is None:
            return False
        return abs(s["line"] - line) < 1e-9

    cands = [s for s in snaps
             if (book is None or s["book"] == book)
             and s["market"] == market and line_match(s)]
    closers = [s for s in cands if s.get("closing")]
    if closers:
        return max(closers, key=lambda s: s["fetched_at"])
    if kickoff_utc:
        pre = [s for s in cands if s["fetched_at"] <= kickoff_utc]
        if pre:
            return max(pre, key=lambda s: s["fetched_at"])
    return None


def probs_vector(snap, order):
    """De-vig (Shin) and reorder to `order` (e.g. ["home","draw","away"])."""
    p = devig_shin(snap["odds"])
    by_label = dict(zip(snap["outcomes"], p))
    return [by_label[o] for o in order]


def model_vector(lock_doc):
    p = lock_doc["payload"]["probs_90"]
    return [p["home"], p["draw"], p["away"]]


def outcome_index(score90):
    h, a = score90
    return 0 if h > a else (2 if h < a else 1)


def _settle_result(adj):
    if adj > 1e-9:
        return 1.0
    if adj < -1e-9:
        return -1.0
    return 0.0


def settle_bet(bet, res):
    """Paper P&L in units. Quarter lines split into two half-stakes."""
    h, a = res["score90"]
    market, sel = bet["market"], bet["selection"]
    odds, stake = bet["odds_taken"], bet.get("stake_u", 1.0)
    if market == "1x2":
        outcome = "home" if h > a else ("away" if h < a else "draw")
        return (odds - 1.0) * stake if sel == outcome else -stake
    if market == "qualify":
        advanced = res.get("advanced")
        if advanced is None:
            return None  # qualify needs ET/pens resolution beyond score90
        return (odds - 1.0) * stake if sel == advanced else -stake
    if market not in ("ah", "totals"):
        return None
    line = bet["line"]
    q = round(line * 4)
    lines = [line] if q % 2 == 0 else [(q - 1) / 4.0, (q + 1) / 4.0]
    pnl = 0.0
    for ln in lines:
        part = stake / len(lines)
        if market == "ah":
            adj = (h + ln - a) if sel == "home" else (a + ln - h)
        else:
            t = h + a
            adj = (t - ln) if sel == "over" else (ln - t)
        r = _settle_result(adj)
        pnl += (odds - 1.0) * part if r > 0 else (-part if r < 0 else 0.0)
    return pnl


def score_b1(locks, results):
    rows = []
    for mid, entry in sorted(locks.items()):
        kickoff = entry["v1"]["payload"]["kickoff_utc"]
        snaps = load_odds(mid)
        close = closing_snapshot(snaps, "1x2", kickoff_utc=kickoff)
        if close is None:  # no Pinnacle close captured — any-book fallback
            close = closing_snapshot(snaps, "1x2", book=None,
                                     kickoff_utc=kickoff)
        res = results.get(mid)
        row = {"match_id": mid, "n_versions": entry["n_versions"],
               "pilot": bool(entry["v1"]["payload"].get("pilot", False))}
        # anchoring audit: v1 lock must precede the first odds contact
        first_fetch = min((s["fetched_at"] for s in snaps), default=None)
        row["contaminated"] = first_fetch is not None and \
            entry["v1"]["locked_at"] > first_fetch
        # J-independence audit: with the auto-M-lock process (CHANGELOG
        # 2026-07-08) J may arrive via a later amend. The FIRST version
        # carrying "j" must still precede first odds contact — a post-market
        # j is not an independent pre-market estimate and is dropped from
        # J scoring (por_esp / m97 precedent, applied mechanically).
        j_doc = next((v for v in entry["versions"]
                      if v["payload"].get("j")), None)
        row["contaminated_j"] = j_doc is not None and \
            first_fetch is not None and j_doc["locked_at"] > first_fetch
        if close is None or res is None or "score90" not in (res or {}):
            row["status"] = "unscored (missing closing odds or result)"
            rows.append(row)
            continue
        idx = outcome_index(res["score90"])
        market_p = probs_vector(close, ["home", "draw", "away"])
        mv_final = model_vector(entry["final"])
        mv_v1 = model_vector(entry["v1"])
        row.update({
            "status": "scored",
            "close_book": close["book"],
            "outcome": ["home", "draw", "away"][idx],
            "model_rps": rps(mv_final, idx),
            "model_rps_v1": rps(mv_v1, idx),
            "market_rps": rps(market_p, idx),
            "model_brier": brier(mv_final, idx),
            "market_brier": brier(market_p, idx),
            "model_better": rps(mv_final, idx) < rps(market_p, idx),
        })
        j = entry["final"]["payload"].get("j")
        if j and row["contaminated_j"]:
            j = None  # j landed after odds contact: not independent, dropped
        if j:  # instrument J (LLM synthesis) — B1 primary for the main sample
            jv = [j["probs_90"]["home"], j["probs_90"]["draw"],
                  j["probs_90"]["away"]]
            row["j_rps"] = rps(jv, idx)
            row["j_brier"] = brier(jv, idx)
            row["j_better"] = rps(jv, idx) < rps(market_p, idx)
        if entry["n_versions"] > 1 and \
                any(abs(x - y) > 1e-12 for x, y in zip(mv_v1, mv_final)):
            # drift check only when the amendment actually moved M — a
            # J-addition amend (auto-M-lock flow) that leaves M untouched
            # is not anchoring drift and must not print as "amend-away"
            d1 = sum(abs(x - y) for x, y in zip(mv_v1, market_p))
            d2 = sum(abs(x - y) for x, y in zip(mv_final, market_p))
            row["amend_moved_toward_market"] = d2 < d1
        rows.append(row)
    return rows


def score_b2(locks, results):
    if not BETS.exists():
        return []
    contaminated = set()
    for mid, entry in locks.items():
        snaps = load_odds(mid)
        if snaps and entry["v1"]["locked_at"] > min(s["fetched_at"] for s in snaps):
            contaminated.add(mid)
    rows = []
    for line_txt in BETS.read_text(encoding="utf-8").splitlines():
        if not line_txt.strip():
            continue
        bet = json.loads(line_txt)
        mid = bet["match_id"]
        pilot = bool(locks.get(mid, {}).get("v1", {})
                     .get("payload", {}).get("pilot", False))
        row = {"bet": bet, "excluded": mid in contaminated, "pilot": pilot}
        res = results.get(mid)
        if not res or "score90" not in res:
            # CLV only settles once the match is played — otherwise the
            # pre-kickoff fallback would compare the taken price to itself
            row["clv"] = None
            row["note"] = "pending (match not played yet)"
            rows.append(row)
            continue
        snaps = load_odds(mid)
        kickoff = (locks.get(mid, {}).get("v1", {})
                   .get("payload", {}).get("kickoff_utc"))
        snap_line = bet.get("line")
        if bet["market"] == "ah" and bet["selection"] == "away" \
                and snap_line is not None:
            snap_line = -snap_line  # snapshots store the home line
        close = closing_snapshot(snaps, bet["market"], line=snap_line,
                                 kickoff_utc=kickoff)
        if close is not None and not close.get("closing"):
            close = None  # CLV is strictly vs flagged closes — a pre-kickoff
            # fallback can be the very snapshot the bet was taken at
        if close is None:
            row["clv"] = None
            row["note"] = "no closing snapshot for this market/line"
        else:
            p = devig_shin(close["odds"])
            fair = dict(zip(close["outcomes"], p)).get(bet["selection"])
            if fair is None:
                row["clv"] = None
                row["note"] = "selection not in closing snapshot outcomes"
            else:
                row["clv"] = bet["odds_taken"] * fair - 1.0
                row["beat_close"] = row["clv"] > 0
        row["pnl_u"] = settle_bet(bet, res)
        rows.append(row)
    return rows


def section4_verdict(scored, j_scored, sample_size=SAMPLE_SIZE,
                     missing=None):
    """The pre-registered section-4 verdict state, as printable lines.

    `scored`/`j_scored` must already be MAIN-SAMPLE-ONLY rows (pilots
    excluded upstream). Three states:
    - n >= 16: the registered thresholds apply mechanically.
    - n >= lockable (all non-MISSING matches settled): print the section-4
      statistics and direction; the formal call is deferred to the final
      report because the thresholds are defined on 16. This state cannot be
      gamed — MISSING matches are impossible to lock retroactively.
    - otherwise: partial sample, no verdict.
    """
    missing = REGISTERED_MISSING if missing is None else missing
    lockable = sample_size - len(missing)

    def stats(use, key_rps, key_bet):
        m_mean = sum(r[key_rps] for r in use) / len(use)
        k_mean = sum(r["market_rps"] for r in use) / len(use)
        losses = sum(1 for r in use if not r[key_bet])
        return m_mean, k_mean, losses

    if len(scored) >= sample_size:
        use = j_scored if len(j_scored) >= sample_size else scored
        label = "J" if use is j_scored else "M"
        key_rps = "j_rps" if use is j_scored else "model_rps"
        key_bet = "j_better" if use is j_scored else "model_better"
        m_mean, k_mean, losses = stats(use, key_rps, key_bet)
        if m_mean > k_mean and losses >= 12:
            return [f"VERDICT (pre-registered, instrument {label}): "
                    f"B1 falsified in-domain"]
        if m_mean < k_mean:
            return [f"VERDICT (pre-registered, instrument {label}): "
                    f"not falsified — NOT proof (N=16 power limit)"]
        return [f"VERDICT (pre-registered, instrument {label}): "
                f"inconclusive"]
    if scored and len(scored) >= lockable:
        use = j_scored if len(j_scored) >= len(scored) else scored
        label = "J" if use is j_scored else "M"
        key_rps = "j_rps" if use is j_scored else "model_rps"
        key_bet = "j_better" if use is j_scored else "model_better"
        m_mean, k_mean, losses = stats(use, key_rps, key_bet)
        direction = ("market ahead" if m_mean > k_mean else
                     "instrument ahead" if m_mean < k_mean else "tied")
        return [
            f"MAIN SAMPLE COMPLETE at n={len(scored)} (<{sample_size}: "
            f"registered MISSING {missing}) — instrument {label}: mean RPS "
            f"{m_mean:.4f} vs market {k_mean:.4f} ({direction}), "
            f"instrument worse in {losses}/{len(use)}.",
            f"Pre-registered section-4 thresholds are defined on "
            f"{sample_size} matches; the formal verdict is adjudicated in "
            f"the final report, which must list the missing match(es).",
        ]
    return [f"partial sample ({len(scored)}/{lockable} lockable main "
            f"matches scored; registered MISSING: {missing}) -- no verdict"]


def main():
    locks = load_locks()
    results = json.loads(RESULTS.read_text(encoding="utf-8")) \
        if RESULTS.exists() else {}
    b1 = score_b1(locks, results)
    b2 = score_b2(locks, results)

    # P0 composition fix (Architecture Review v3 section 2.1 / Migration Plan
    # Phase 0(a)): pilot matches (payload.pilot == true) are exploratory per
    # PROTOCOL CHANGELOG 2026-07-02 and must never be counted into the
    # pre-registered 16-match sample or trip the section-4 verdict trigger.
    # This is a pure reporting/aggregation fix — no historical row's RPS,
    # CONTAMINATED flag, or lock content changes.
    scored_all = [r for r in b1 if r.get("status") == "scored"]
    scored = [r for r in scored_all if not r.get("pilot")]
    pilot_scored = [r for r in scored_all if r.get("pilot")]
    print("=== B1: model vs Pinnacle close (RPS, lower is better) "
          "— pre-registered main sample (Match 89-104) ===")
    for r in b1:
        if r.get("pilot"):
            continue
        if r.get("status") != "scored":
            flag = " CONTAMINATED" if r.get("contaminated") else ""
            print(f"  {r['match_id']}: {r.get('status', '?')}{flag}")
            continue
        flag = " CONTAMINATED" if r["contaminated"] else ""
        if r.get("contaminated_j"):
            flag += " J-DROPPED(post-market j)"
        drift = ""
        if "amend_moved_toward_market" in r:
            drift = ("  amend->market" if r["amend_moved_toward_market"]
                     else "  amend-away")
        jtxt = f"  J={r['j_rps']:.4f}" if "j_rps" in r else ""
        book = ("" if r.get("close_book") == "pinnacle"
                else f"  [close={r.get('close_book')}]")
        print(f"  {r['match_id']}: outcome={r['outcome']}  "
              f"M={r['model_rps']:.4f}{jtxt}  market={r['market_rps']:.4f}  "
              f"{'MODEL' if r['model_better'] else 'market'} better"
              f"{flag}{drift}{book}")
    if scored:
        mm = sum(r["model_rps"] for r in scored) / len(scored)
        mk = sum(r["market_rps"] for r in scored) / len(scored)
        wins = sum(1 for r in scored if r["model_better"])
        print(f"\n  scored {len(scored)}/{SAMPLE_SIZE}  "
              f"mean RPS: M={mm:.4f} market={mk:.4f}  "
              f"M better in {wins}/{len(scored)}")
        j_scored = [r for r in scored if "j_rps" in r]
        if j_scored:
            jm = sum(r["j_rps"] for r in j_scored) / len(j_scored)
            jw = sum(1 for r in j_scored if r["j_better"])
            print(f"  instrument J: n={len(j_scored)}  mean RPS: J={jm:.4f}  "
                  f"J better in {jw}/{len(j_scored)}")
        # pre-registered verdict applies to the hypothesis instrument:
        # J when present on the full MAIN sample, else M (pilots never count
        # toward this — they have no bearing on the section-4 trigger).
        for line in section4_verdict(scored, j_scored):
            print(f"  {line}")
    else:
        print("  no main-sample matches scored yet")

    if pilot_scored:
        print("\n=== B1 (pilot, EXPLORATORY — not the pre-registered sample; "
              "informational only, no verdict) ===")
        for r in pilot_scored:
            jtxt = f"  J={r['j_rps']:.4f}" if "j_rps" in r else ""
            print(f"  {r['match_id']}: outcome={r['outcome']}  "
                  f"M={r['model_rps']:.4f}{jtxt}  market={r['market_rps']:.4f}")
        pm = sum(r["model_rps"] for r in pilot_scored) / len(pilot_scored)
        pk = sum(r["market_rps"] for r in pilot_scored) / len(pilot_scored)
        print(f"  n={len(pilot_scored)}  mean RPS: M={pm:.4f} market={pk:.4f}"
              f"  (pilot — excluded from section-4 verdict by design)")

    print("\n=== B2: paper-bet CLV (vs Pinnacle close) "
          "— pre-registered main sample ===")
    b2_main = [r for r in b2 if not r.get("pilot")]
    b2_pilot = [r for r in b2 if r.get("pilot")]
    valid = [r for r in b2_main if not r["excluded"] and r.get("clv") is not None]
    for r in b2_main:
        b = r["bet"]
        tag = "EXCLUDED" if r["excluded"] else (
            r.get("note") or
            (f"CLV={r['clv']:+.3%} {'beat' if r['beat_close'] else 'lost'}-close"))
        pnl = f"  pnl={r['pnl_u']:+.2f}u" if r.get("pnl_u") is not None else ""
        print(f"  {b['match_id']} {b['market']} {b['selection']} "
              f"@{b['odds_taken']}: {tag}{pnl}")
    if valid:
        beat = sum(1 for r in valid if r["beat_close"])
        print(f"\n  beat-close rate: {beat}/{len(valid)}"
              f"{'' if len(valid) >= 5 else '  (<5 bets: B2 no-data)'}")
    else:
        print("  no valid bets recorded")

    if b2_pilot:
        print("\n=== B2 (pilot, EXPLORATORY — informational only) ===")
        valid_p = [r for r in b2_pilot if not r["excluded"]
                   and r.get("clv") is not None]
        for r in b2_pilot:
            b = r["bet"]
            tag = "EXCLUDED" if r["excluded"] else (
                r.get("note") or
                (f"CLV={r['clv']:+.3%} {'beat' if r['beat_close'] else 'lost'}-close"))
            pnl = f"  pnl={r['pnl_u']:+.2f}u" if r.get("pnl_u") is not None else ""
            print(f"  {b['match_id']} {b['market']} {b['selection']} "
                  f"@{b['odds_taken']}: {tag}{pnl}")
        if valid_p:
            beat_p = sum(1 for r in valid_p if r["beat_close"])
            print(f"\n  pilot beat-close rate: {beat_p}/{len(valid_p)} "
                  f"(exploratory, never counts toward B2)")

    REPORT.write_text(json.dumps({"b1": b1, "b2": b2}, ensure_ascii=False,
                                 indent=2, default=str), encoding="utf-8")
    print(f"\nreport written to {REPORT}")


if __name__ == "__main__":
    main()
