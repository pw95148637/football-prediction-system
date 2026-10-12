"""Cross-book consensus fair prices, line movement, and steam detection.

Architecture Review v3 section 4.5 groundwork: before any shadow-bet or
decision-support logic can reason about "the market," it needs one honest
view of what "the market" currently thinks. This module builds that view
the same way score.py scores CLV -- Shin de-vig per book, per market/line
-- but aggregated across every book we have a snapshot for, instead of
anchored to a single reference book (Pinnacle).

Every function here is pure: it takes a list of odds-snapshot "data" dicts
(the inner `data` field of a store odds record -- NOT the store-wrapper
dict with era/kind/ts) and returns a plain value. Callers pull the raw
snapshot lists themselves (store.io.read(...)["data"] per record, or the
legacy odds/*.jsonl loader) and hand them in here.
"""
from statistics import median

from football_ai.market.devig import devig_shin


def _line_match(snap_line, line):
    """Same line-matching semantics as score.py's closing_snapshot: both
    None means a no-line market (1x2/qualify) and counts as a match;
    exactly one of the two being None never matches (a no-line query must
    not silently pick up a lined snapshot or vice versa); otherwise match
    within float tolerance (lines are stored as floats like -0.25/2.75)."""
    if line is None and snap_line is None:
        return True
    if line is None or snap_line is None:
        return False
    return abs(snap_line - line) < 1e-9


def snap_fair(snap):
    """De-vig (Shin) one snapshot into {outcome: probability}."""
    p = devig_shin(snap["odds"])
    return dict(zip(snap["outcomes"], p))


def latest_pre_kickoff(snaps, book, market="1x2", line=None, kickoff_utc=None):
    """The most recent snapshot for (book, market, line) at or before
    kickoff_utc. With kickoff_utc=None, simply the most recent snapshot we
    have (consensus_fair uses this for matches where no lock kickoff is on
    hand -- "the latest read of the market," not a formally pre-kickoff
    one). None if there is no candidate at all.
    """
    cands = [s for s in snaps
             if s.get("book") == book and s.get("market") == market
             and _line_match(s.get("line"), line)]
    if kickoff_utc is not None:
        cands = [s for s in cands if s["fetched_at"] <= kickoff_utc]
    if not cands:
        return None
    return max(cands, key=lambda s: s["fetched_at"])


def consensus_fair(snaps, market="1x2", line=None, kickoff_utc=None):
    """Per-book latest-pre-kickoff fair price, plus a cross-book consensus.

    The consensus is the per-outcome MEDIAN across books (robust to one
    stale or mispriced book), renormalized to sum to 1 -- the median of
    several vectors that each individually sum to 1 need not itself sum to
    1 once there are 3+ books pulling different outcomes' medians from
    different source vectors, so we renormalize rather than assume it.
    Returns {"books": {}, "consensus": None, "n_books": 0} if no book has a
    matching snapshot.
    """
    books = sorted({s["book"] for s in snaps if s.get("market") == market})
    fairs = {}
    for book in books:
        snap = latest_pre_kickoff(snaps, book, market=market, line=line,
                                   kickoff_utc=kickoff_utc)
        if snap is not None:
            fairs[book] = snap_fair(snap)
    if not fairs:
        return {"books": {}, "consensus": None, "n_books": 0}

    outcomes = sorted(next(iter(fairs.values())).keys())
    raw = {o: median(f[o] for f in fairs.values() if o in f) for o in outcomes}
    total = sum(raw.values())
    consensus = {o: v / total for o, v in raw.items()}
    return {"books": fairs, "consensus": consensus, "n_books": len(fairs)}


def movement(snaps, book, market="1x2", line=None):
    """Open (first fetch) vs latest fair price for one book/market/line, in
    percentage points per outcome. None if we have no snapshot at all for
    that (book, market, line) combination.
    """
    cands = [s for s in snaps
             if s.get("book") == book and s.get("market") == market
             and _line_match(s.get("line"), line)]
    if not cands:
        return None
    cands = sorted(cands, key=lambda s: s["fetched_at"])
    opening, latest = cands[0], cands[-1]
    open_fair, latest_fair = snap_fair(opening), snap_fair(latest)
    delta_pp = {o: (latest_fair[o] - open_fair[o]) * 100.0 for o in open_fair}
    return {"open": open_fair, "latest": latest_fair, "delta_pp": delta_pp,
            "open_at": opening["fetched_at"], "latest_at": latest["fetched_at"]}


def steam_flag(snaps, market="1x2", threshold_pp=2.0):
    """Steam = at least two books moving the SAME outcome the SAME
    direction by at least threshold_pp between that book's own open and
    latest snapshot. Deliberately line-agnostic (uses movement(..., line=
    None)): steam is a 1x2/qualify-style whole-market signal, not meant to
    track one specific spread/total line among many concurrent ones.
    """
    books = sorted({s["book"] for s in snaps if s.get("market") == market})
    moved = {}  # (outcome, sign) -> [book, ...]
    for book in books:
        mv = movement(snaps, book, market=market, line=None)
        if mv is None:
            continue
        for outcome, delta in mv["delta_pp"].items():
            if abs(delta) >= threshold_pp:
                sign = 1 if delta > 0 else -1
                moved.setdefault((outcome, sign), []).append(book)

    triggered = [(outcome, blist) for (outcome, sign), blist in moved.items()
                 if len(blist) >= 2]
    if not triggered:
        return {"steam": False, "direction": None, "books_moved": []}
    # Deterministic pick if more than one outcome qualifies at once: most
    # books agreeing wins; ties broken by outcome name for reproducibility.
    triggered.sort(key=lambda t: (-len(t[1]), t[0]))
    outcome, blist = triggered[0]
    return {"steam": True, "direction": outcome, "books_moved": sorted(blist)}


def _selfcheck():
    """Synthetic 2-books x 2-times-each fixture built from zero-margin
    (no-vig) odds, so every expected number below is hand-derivable from
    devig_shin's well-known no-margin behavior (proportional to implied
    probability -- see wc2026/selftest.py's "no-margin odds returned
    proportionally" check), not re-derived from this module's own output.

    Returns a list of failure-description strings (empty = all pass).
    """
    fails = []

    def approx(a, b, tol=1e-6):
        return abs(a - b) <= tol

    snaps = [
        # bookA, 1x2, no line. open -> fair (0.5, 1/3, 1/6);
        #                       latest -> fair (0.4, 0.4, 0.2)
        {"book": "bookA", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [2.0, 3.0, 6.0],
         "fetched_at": "2026-01-01T00:00:00+00:00", "closing": False},
        {"book": "bookA", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [2.5, 2.5, 5.0],
         "fetched_at": "2026-01-01T02:00:00+00:00", "closing": False},
        # bookB, 1x2, no line. open -> fair (0.25, 0.25, 0.5);
        #                       latest -> fair (1/3, 1/3, 1/3)
        {"book": "bookB", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [4.0, 4.0, 2.0],
         "fetched_at": "2026-01-01T00:30:00+00:00", "closing": False},
        {"book": "bookB", "market": "1x2", "line": None,
         "outcomes": ["home", "draw", "away"], "odds": [3.0, 3.0, 3.0],
         "fetched_at": "2026-01-01T02:30:00+00:00", "closing": False},
    ]

    # --- snap_fair ---
    fair0 = snap_fair(snaps[0])
    if not (approx(fair0["home"], 0.5) and approx(fair0["draw"], 1 / 3)
            and approx(fair0["away"], 1 / 6)):
        fails.append(f"snap_fair no-margin odds mismatch: {fair0}")

    # --- latest_pre_kickoff ---
    lp = latest_pre_kickoff(snaps, "bookA", kickoff_utc=None)
    if lp is None or lp["fetched_at"] != "2026-01-01T02:00:00+00:00":
        fails.append("latest_pre_kickoff(kickoff=None) did not pick the latest overall")

    lp2 = latest_pre_kickoff(snaps, "bookA",
                              kickoff_utc="2026-01-01T01:00:00+00:00")
    if lp2 is None or lp2["fetched_at"] != "2026-01-01T00:00:00+00:00":
        fails.append("latest_pre_kickoff did not exclude the post-kickoff fetch")

    if latest_pre_kickoff(snaps, "no_such_book") is not None:
        fails.append("latest_pre_kickoff must return None for an unknown book")

    # --- line-matching semantics ---
    ah_snaps = [
        {"book": "bookA", "market": "ah", "line": -0.25,
         "outcomes": ["home", "away"], "odds": [1.95, 1.95],
         "fetched_at": "2026-01-01T00:00:00+00:00", "closing": False},
        {"book": "bookA", "market": "ah", "line": None,
         "outcomes": ["home", "away"], "odds": [1.90, 1.90],
         "fetched_at": "2026-01-01T00:00:00+00:00", "closing": False},
    ]
    exact = latest_pre_kickoff(ah_snaps, "bookA", market="ah", line=-0.25)
    if exact is None or exact["odds"] != [1.95, 1.95]:
        fails.append("line-matching failed to find the exact-line snapshot")
    noisy = latest_pre_kickoff(ah_snaps, "bookA", market="ah",
                                line=-0.2500000000001)
    if noisy is None or noisy["odds"] != [1.95, 1.95]:
        fails.append("line-matching too strict on sub-1e-9 float noise")
    if latest_pre_kickoff(ah_snaps, "bookA", market="ah", line=0.0) is not None:
        fails.append("line-matching must not match a genuinely different line")
    none_q = latest_pre_kickoff(ah_snaps, "bookA", market="ah", line=None)
    if none_q is None or none_q["odds"] != [1.90, 1.90]:
        fails.append("line=None query must match only the no-line snapshot")

    # --- consensus_fair: median of (0.4/0.4/0.2) and (1/3/1/3/1/3) ---
    cf = consensus_fair(snaps)
    if cf["n_books"] != 2 or set(cf["books"]) != {"bookA", "bookB"}:
        fails.append(f"consensus_fair book set wrong: {cf}")
    raw_home = (0.4 + 1 / 3) / 2
    raw_draw = (0.4 + 1 / 3) / 2
    raw_away = (0.2 + 1 / 3) / 2
    total = raw_home + raw_draw + raw_away
    if cf["consensus"] is None or \
            not approx(cf["consensus"]["home"], raw_home / total) or \
            not approx(cf["consensus"]["draw"], raw_draw / total) or \
            not approx(cf["consensus"]["away"], raw_away / total):
        fails.append(f"consensus_fair median mismatch: {cf.get('consensus')}")

    if consensus_fair([])["consensus"] is not None:
        fails.append("consensus_fair on empty input must return consensus=None")

    # --- movement ---
    mv_a = movement(snaps, "bookA")
    if mv_a is None:
        fails.append("movement returned None for bookA")
    else:
        expected_delta = {"home": -10.0, "draw": (0.4 - 1 / 3) * 100.0,
                           "away": (0.2 - 1 / 6) * 100.0}
        for outcome, exp in expected_delta.items():
            if not approx(mv_a["delta_pp"][outcome], exp, 1e-4):
                fails.append(f"movement delta_pp[{outcome}] mismatch: "
                             f"{mv_a['delta_pp'][outcome]} vs {exp}")
        if mv_a["open_at"] != "2026-01-01T00:00:00+00:00" or \
                mv_a["latest_at"] != "2026-01-01T02:00:00+00:00":
            fails.append("movement open_at/latest_at mismatch")

    if movement(snaps, "no_such_book") is not None:
        fails.append("movement must return None for a book with no snapshots")

    # --- steam_flag: draw moves +6.67pp (bookA) and +8.33pp (bookB) --
    # both exceed 2.0pp in the same (positive) direction -> steam on "draw".
    st_true = steam_flag(snaps, threshold_pp=2.0)
    if not st_true["steam"] or st_true["direction"] != "draw" or \
            st_true["books_moved"] != ["bookA", "bookB"]:
        fails.append(f"steam_flag true-case mismatch: {st_true}")

    # Raise the bar past both books' draw moves and past every other
    # single-book move too -> nothing left with 2 agreeing books.
    st_false = steam_flag(snaps, threshold_pp=10.0)
    if st_false["steam"]:
        fails.append(f"steam_flag false-case unexpectedly triggered: {st_false}")

    return fails


if __name__ == "__main__":
    import sys
    problems = _selfcheck()
    print(problems if problems else "OK")
    sys.exit(1 if problems else 0)
