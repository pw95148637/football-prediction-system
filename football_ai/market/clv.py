"""Closing-line value, plus the MECHANICAL shadow-bet derived metric from
Architecture Review v3 section 4.5.

closing_snapshot is a byte-for-byte PORT of wc2026/score.py's function of
the same name: score_b2's CLV settlement and this module's clv() must
agree on what "the close" means (closing-flagged snapshot preferred, else
the last pre-kickoff fetch; book=None as an any-bookmaker fallback), so
the semantics are duplicated here rather than imported. They are not
imported because market/ must not depend on the v2 root scripts (see
football_ai/README.md's import-direction rules -- market may use
core/store plus this package's own frozen devig copy, nothing else); the
v2 pipeline in wc2026/ stays the sole live authority until cutover.
"""
from football_ai.market.devig import devig_shin


def _line_match(snap_line, line):
    """Same tolerance rule as score.py's closing_snapshot: both None
    matches (no-line market); exactly one None never matches; otherwise
    match within float tolerance."""
    if line is None and snap_line is None:
        return True
    if line is None or snap_line is None:
        return False
    return abs(snap_line - line) < 1e-9


def closing_snapshot(snaps, market="1x2", line=None, book="pinnacle",
                      kickoff_utc=None):
    """Prefer explicit closing snapshots; else the last fetch before
    kickoff. book=None accepts any bookmaker (used as a fallback when no
    Pinnacle close was captured -- the caller records which book was
    actually used). Exact port of wc2026/score.py's closing_snapshot.
    """
    def line_ok(s):
        return _line_match(s.get("line"), line)

    cands = [s for s in snaps
             if (book is None or s["book"] == book)
             and s["market"] == market and line_ok(s)]
    closers = [s for s in cands if s.get("closing")]
    if closers:
        return max(closers, key=lambda s: s["fetched_at"])
    if kickoff_utc:
        pre = [s for s in cands if s["fetched_at"] <= kickoff_utc]
        if pre:
            return max(pre, key=lambda s: s["fetched_at"])
    return None


def clv(odds_taken, selection, close_snap):
    """Closing-line value of a bet taken at odds_taken on `selection`,
    against the Shin-devigged fair price implied by close_snap.

    None if close_snap is None, or if `selection` is not one of the close
    snapshot's outcomes (e.g. a relabeled/void line) -- this function never
    silently substitutes a wrong number for a genuinely absent one.
    """
    if close_snap is None:
        return None
    p = devig_shin(close_snap["odds"])
    fair = dict(zip(close_snap["outcomes"], p)).get(selection)
    if fair is None:
        return None
    return odds_taken * fair - 1.0


def shadow_bets(lock_versions_by_match, odds_by_match, kickoff_by_match,
                 preferred_book="pinnacle"):
    """Architecture Review v3 section 4.5's MECHANICAL shadow-bet rule.

    MULTI-COMPETITION NOTE (2026-07-24): `preferred_book` was an internal
    "pinnacle" literal buried in this function's body (steps 3/5 below),
    not a parameter -- research/audit/final_report.md section 7.4 flagged
    it as the one hardcoded book default in this module with no override
    at all. Defaults to "pinnacle" so every existing caller is unaffected;
    pass `config.data_sources.odds_book_default` for another competition.

    This is a DERIVED, exploratory metric: how would a paper bet placed
    right after instrument J's first appearance in a match's lock history
    have fared against that match's eventual close? It is NOT the
    pre-registered B2, which only scores the bets.jsonl ledger of bets the
    analyst actually recorded taking (wc2026/score.py's score_b2). Every
    non-skipped row carries label="shadow-derived (exploratory, NOT B2)"
    for exactly that reason, so nothing downstream can fold it into B2's
    numbers by accident.

    Per match_id:
      1. Find the FIRST lock version (by "version") whose payload has a
         "j" key -> j_locked_at = that version's locked_at. No such
         version -> skip, reason "no J instrument".
      2. If any odds snapshot for the match was fetched before j_locked_at,
         J was not an independent pre-market read -> skip, reason
         "post-market j (contaminated_j)" (mirrors score_b1's
         contaminated_j audit, applied mechanically here too).
      3. entry snapshot = earliest 1x2 snapshot fetched after j_locked_at
         with closing=False, preferring book "pinnacle" (falling back to
         the earliest snapshot from any book if Pinnacle has none) -> no
         candidate -> skip, reason "no pre-close entry snapshot".
      4. fair_entry = Shin de-vig of the entry snapshot. j = the FINAL lock
         version's payload.j.probs_90 (a later amend may have refined J
         again after the version that first introduced it). gap_pp[o] =
         (j[o] - fair_entry[o]) * 100 for each outcome; side = argmax(gap).
         If gap[side] <= 0, there is no positive divergence to shadow-bet
         on -> skip, reason "no positive divergence" (defensive: with
         well-formed inputs both vectors sum to 1, so some outcome must
         have a positive gap unless entry and J are identical).
      5. close = closing_snapshot(..., book="pinnacle") but ONLY accepted
         if it is actually closing-flagged (like score_b2 -- a pre-kickoff
         fallback here could turn out to be the entry snapshot itself, or
         another pre-close snapshot, which would make "CLV" compare the
         entry price to a non-closing price). No accepted close -> clv is
         None with note "no flagged closing snapshot".
      6. clv_value = clv(entry_odds, side, close).

    Returns one row per match: either {"match_id", "skipped": True,
    "reason"} or the full shadow-bet row (see docstring keys below).
    """
    rows = []
    for match_id, versions in lock_versions_by_match.items():
        versions = sorted(versions, key=lambda v: v["version"])
        snaps = odds_by_match.get(match_id, [])
        kickoff = kickoff_by_match.get(match_id)

        j_doc = next((v for v in versions if v["payload"].get("j")), None)
        if j_doc is None:
            rows.append({"match_id": match_id, "skipped": True,
                         "reason": "no J instrument"})
            continue
        j_locked_at = j_doc["locked_at"]

        first_fetch = min((s["fetched_at"] for s in snaps), default=None)
        if first_fetch is not None and first_fetch < j_locked_at:
            rows.append({"match_id": match_id, "skipped": True,
                         "reason": "post-market j (contaminated_j)"})
            continue

        one_x_two = [s for s in snaps if s.get("market") == "1x2"]
        candidates = [s for s in one_x_two
                     if s["fetched_at"] > j_locked_at and not s.get("closing")]
        preferred_candidates = [s for s in candidates if s["book"] == preferred_book]
        if preferred_candidates:
            entry = min(preferred_candidates, key=lambda s: s["fetched_at"])
        elif candidates:
            entry = min(candidates, key=lambda s: s["fetched_at"])
        else:
            entry = None
        if entry is None:
            rows.append({"match_id": match_id, "skipped": True,
                         "reason": "no pre-close entry snapshot"})
            continue
        entry_book = entry["book"]

        fair_entry = dict(zip(entry["outcomes"], devig_shin(entry["odds"])))
        final_doc = versions[-1]
        j_final = final_doc["payload"].get("j", {}).get("probs_90", {})
        gap = {o: (j_final.get(o, 0.0) - fair_entry.get(o, 0.0)) * 100.0
               for o in fair_entry}
        side = max(gap, key=lambda o: gap[o])
        if gap[side] <= 0.0:
            rows.append({"match_id": match_id, "skipped": True,
                         "reason": "no positive divergence"})
            continue

        entry_odds = dict(zip(entry["outcomes"], entry["odds"]))[side]

        close = closing_snapshot(snaps, "1x2", book=preferred_book, kickoff_utc=kickoff)
        if close is not None and not close.get("closing"):
            # A pre-kickoff fallback here could be the entry snapshot
            # itself (or another non-closing snapshot) -- CLV must be
            # measured strictly against a flagged close, never a stand-in.
            close = None
        if close is None:
            clv_value = None
            note = "no flagged closing snapshot"
        else:
            clv_value = clv(entry_odds, side, close)
            note = None if clv_value is not None else \
                "selection not in closing snapshot outcomes"

        rows.append({
            "match_id": match_id,
            "side": side,
            "gap_pp": gap[side],
            "entry_odds": entry_odds,
            "entry_book": entry_book,
            "entry_at": entry["fetched_at"],
            "clv": clv_value,
            "beat_close": (clv_value > 0) if clv_value is not None else None,
            "note": note,
            "skipped": False,
            "label": "shadow-derived (exploratory, NOT B2)",
        })
    return rows


def _selfcheck():
    """Failure-string list (empty = pass). Covers:
    (1) closing_snapshot port fidelity, replicating the exact synthetic
        fixture and assertions from wc2026/selftest.py's "score plumbing"
        section, so this port can never silently diverge from score.py;
    (2) clv() basic sanity (absent selection, None close);
    (3) a three-match shadow_bets scenario -- one clean bet with
        hand-computed side/gap/clv, one contaminated-j skip, one
        no-flagged-close case -- covering every branch of the mechanical
        shadow-bet rule.
    """
    fails = []

    def approx(a, b, tol=1e-9):
        return abs(a - b) <= tol

    # --- (1) closing_snapshot port fidelity ---
    snaps = [
        {"book": "polymarket", "market": "1x2", "line": None,
         "odds": [2.0, 3.0, 4.0], "outcomes": ["home", "draw", "away"],
         "fetched_at": "2026-01-01T00:00:00+00:00", "closing": False},
        {"book": "pinnacle", "market": "1x2", "line": None,
         "odds": [2.1, 3.1, 4.1], "outcomes": ["home", "draw", "away"],
         "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
        {"book": "pinnacle", "market": "1x2", "line": None,
         "odds": [2.2, 3.2, 4.2], "outcomes": ["home", "draw", "away"],
         "fetched_at": "2026-01-01T02:00:00+00:00", "closing": True},
    ]
    ko = "2026-01-01T03:00:00+00:00"
    if closing_snapshot(snaps, "1x2")["odds"][0] != 2.2:
        fails.append("closing_snapshot: closing flag not preferred over later fetch")
    if closing_snapshot(snaps[:2], "1x2", kickoff_utc=ko)["odds"][0] != 2.1:
        fails.append("closing_snapshot: pre-kickoff fallback did not pick last pinnacle fetch")
    if closing_snapshot(snaps[:1], "1x2", book=None, kickoff_utc=ko)["odds"][0] != 2.0:
        fails.append("closing_snapshot: book=None did not fall back to any bookmaker")
    if closing_snapshot(snaps[:1], "1x2", kickoff_utc=ko) is not None:
        fails.append("closing_snapshot: pinnacle-only should return None when absent")

    # --- (2) clv() basic sanity ---
    close_flagged = snaps[2]
    fair_away = devig_shin(close_flagged["odds"])[2]  # outcomes[2] == "away"
    expected_clv = 3.0 * fair_away - 1.0
    got_clv = clv(3.0, "away", close_flagged)
    if got_clv is None or not approx(got_clv, expected_clv):
        fails.append(f"clv() basic mismatch: {got_clv} vs {expected_clv}")
    if clv(3.0, "push", close_flagged) is not None:
        fails.append("clv() must return None for a selection absent from the close")
    if clv(3.0, "away", None) is not None:
        fails.append("clv() must return None when close_snap is None")

    # --- (3) shadow_bets: three-match synthetic scenario ---
    lock_versions = {
        "mA_clean": [{
            "locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
            "payload": {"match_id": "mA_clean",
                        "j": {"probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2}}},
        }],
        "mB_contam": [
            {"locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
             "payload": {"match_id": "mB_contam"}},
            {"locked_at": "2026-01-01T05:00:00+00:00", "version": 2,
             "payload": {"match_id": "mB_contam",
                         "j": {"probs_90": {"home": 0.4, "draw": 0.3, "away": 0.3}}}},
        ],
        "mC_noclose": [{
            "locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
            "payload": {"match_id": "mC_noclose",
                        "j": {"probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2}}},
        }],
    }
    odds_by_match = {
        "mA_clean": [
            {"book": "pinnacle", "market": "1x2", "line": None,
             "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.0, 6.0],
             "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
            {"book": "pinnacle", "market": "1x2", "line": None,
             "outcomes": ["home", "draw", "away"], "odds": [2.5, 2.5, 5.0],
             "fetched_at": "2026-01-01T02:00:00+00:00", "closing": True},
        ],
        "mB_contam": [
            {"book": "pinnacle", "market": "1x2", "line": None,
             "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.0, 4.0],
             "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
        ],
        "mC_noclose": [
            {"book": "pinnacle", "market": "1x2", "line": None,
             "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.0, 6.0],
             "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
            {"book": "pinnacle", "market": "1x2", "line": None,
             "outcomes": ["home", "draw", "away"], "odds": [2.1, 3.1, 4.1],
             "fetched_at": "2026-01-01T02:00:00+00:00", "closing": False},
        ],
    }
    kickoff_by_match = {"mA_clean": None, "mB_contam": None,
                        "mC_noclose": "2026-01-01T03:00:00+00:00"}

    rows = shadow_bets(lock_versions, odds_by_match, kickoff_by_match)
    by_id = {r["match_id"]: r for r in rows}

    a = by_id.get("mA_clean")
    if a is None or a.get("skipped"):
        fails.append(f"mA_clean unexpectedly skipped/missing: {a}")
    else:
        if a["side"] != "away":
            fails.append(f"mA_clean side mismatch: {a['side']}")
        if not approx(a["gap_pp"], (0.2 - 1 / 6) * 100.0):
            fails.append(f"mA_clean gap_pp mismatch: {a['gap_pp']}")
        if a["entry_odds"] != 6.0 or a["entry_book"] != "pinnacle":
            fails.append(f"mA_clean entry mismatch: {a}")
        if a["clv"] is None or not approx(a["clv"], 0.2):
            fails.append(f"mA_clean clv mismatch: {a['clv']}")
        if a["beat_close"] is not True:
            fails.append(f"mA_clean beat_close should be True: {a}")
        if a["label"] != "shadow-derived (exploratory, NOT B2)":
            fails.append("mA_clean missing/incorrect exploratory label")

    b = by_id.get("mB_contam")
    if b is None or not b.get("skipped") or \
            b.get("reason") != "post-market j (contaminated_j)":
        fails.append(f"mB_contam should skip as contaminated_j: {b}")

    c = by_id.get("mC_noclose")
    if c is None or c.get("skipped"):
        fails.append(f"mC_noclose unexpectedly skipped/missing: {c}")
    elif c["clv"] is not None or c.get("note") != "no flagged closing snapshot":
        fails.append(f"mC_noclose should have clv=None: {c}")

    # --- (4) preferred_book override: both a pinnacle and a betfair
    # candidate exist post-j-lock; the DEFAULT call must still prefer
    # pinnacle (byte-identical to pre-2026-07-24 behavior), but passing
    # preferred_book="betfair" must switch the entry (and the closing
    # lookup) to betfair instead -- proving this is a real parameter, not
    # dead code shadowing the hardcoded literal it replaced.
    lock_versions_d = {"mD_altbook": [{
        "locked_at": "2026-01-01T00:00:00+00:00", "version": 1,
        "payload": {"match_id": "mD_altbook",
                    "j": {"probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2}}},
    }]}
    odds_d = {"mD_altbook": [
        {"book": "betfair", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [2.05, 3.05, 6.05],
         "fetched_at": "2026-01-01T01:00:00+00:00", "closing": False},
        {"book": "pinnacle", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.0, 6.0],
         "fetched_at": "2026-01-01T01:30:00+00:00", "closing": False},
        {"book": "betfair", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [2.4, 2.6, 5.1],
         "fetched_at": "2026-01-01T02:00:00+00:00", "closing": True},
    ]}
    kickoff_d = {"mD_altbook": None}

    default_row = shadow_bets(lock_versions_d, odds_d, kickoff_d)[0]
    if default_row.get("entry_book") != "pinnacle":
        fails.append(f"preferred_book default should still be pinnacle: {default_row}")

    betfair_row = shadow_bets(lock_versions_d, odds_d, kickoff_d,
                              preferred_book="betfair")[0]
    if betfair_row.get("entry_book") != "betfair":
        fails.append(f"preferred_book='betfair' override was not honored "
                     f"for entry selection: {betfair_row}")
    if betfair_row.get("clv") is None:
        fails.append(f"preferred_book='betfair' should also steer the "
                     f"closing-snapshot lookup to the flagged betfair close: {betfair_row}")

    return fails


if __name__ == "__main__":
    import sys
    problems = _selfcheck()
    print(problems if problems else "OK")
    sys.exit(1 if problems else 0)
