"""Elo replay for ONE division of a promotion/relegation pyramid.

WHY (2026-10-10, EFL Championship trial): the EPL replay
(`competitions.epl_2026_27.current_ratings`, the same loop as
`m_dc.replay_df`) gives a club it has never seen 1500.0 and a returning
club whatever rating it left with, possibly years stale. In the Premier
League that touches 3 clubs of 20 each summer, all entering from below. In
the Championship 6 of 24 clubs are new every season, from two directions
-- 3 relegated from the Premier League, 3 promoted from League One -- and a
club's last Championship rating can be a decade old. Results within one
division say nothing about how strong an arriving club is (there are no
cross-division league fixtures), so the entry rating has to be a rule.

Three rules, all using only facts known at the season's start (who is in
the division, and who was in the division above last season):

  - "carry":   the EPL behaviour, kept for comparison -- a club keeps its
               last rating in this division, else `start_rating`.
  - "replace": a club arriving from above takes the mean rating the clubs
               that left upward had at the end of last season; one arriving
               from below takes the mean of those that left downward.
               Parameter-free, and it conserves the pool's rating sum.
  - "offset":  arriving from above -> mean of the continuing clubs +
               `delta_above`; from below -> that mean + `delta_below`. Two
               parameters, to be fitted on results before any test window.

Any arriving club is "new" under "replace" and "offset" even if it was in
the division before: its old rating describes a different squad.

Results only. Nothing here reads a price; the caller fits the parameters
(research/championship_backtest/backtest.py) and states where they came from.
"""
from football_ai.models.m_dc import gd_mult

ENTRY_SCHEMES = ("carry", "replace", "offset")


def season_rosters(matches):
    """{season: set(teams)} from the matches themselves."""
    out = {}
    for m in matches:
        out.setdefault(m["season"], set()).update((m["home_team"], m["away_team"]))
    return out


def classify_arrivals(rosters, above, season):
    """(from_above, from_below, left_up, left_down) for the summer before
    `season`. `above[s]` is the set of clubs in the division above in season
    s. A club new to this division that was not above last season came from
    below; one that left and is above this season went up."""
    prev, cur = rosters.get(season - 1, set()), rosters[season]
    new, gone = cur - prev, prev - cur
    up_prev, up_now = above.get(season - 1, set()), above.get(season, set())
    from_above = new & up_prev
    left_up = gone & up_now
    return from_above, new - from_above, left_up, gone - left_up


def _mean(values, default):
    values = list(values)
    return sum(values) / len(values) if values else default


def replay_division(matches, k, home_adv, scheme="carry", above=None,
                    delta_above=0.0, delta_below=0.0, start_rating=1500.0,
                    rosters=None):
    """Chronological Elo replay over one division's results.

    `matches`: dicts with date (ISO string), season (start year), home_team,
    away_team, home_score, away_score -- in any order; sorted here by date.
    `above`: {season: set(clubs in the division above)}; required unless
    scheme == "carry". `rosters`: {season: set(clubs)}; defaults to the clubs
    seen in `matches` (pass the known roster to rate a season that has not
    kicked off yet, e.g. a pre-season lock).

    Returns (rows, ratings). `rows[i]` belongs to the i-th match in date
    order: dict(date, season, home_team, away_team, hs, as_, elo_home,
    elo_away, d_eff, home_new, away_new) -- the ratings are PRE-match, so a
    row never sees its own result. `home_new`/`away_new` is True while that
    club is in its first season after arriving. `ratings` is the state after
    the last match, with the entry rule already applied for every season in
    `rosters`, including one with no match yet.
    """
    if scheme not in ENTRY_SCHEMES:
        raise ValueError(f"scheme must be one of {ENTRY_SCHEMES}, got {scheme!r}")
    if scheme != "carry" and above is None:
        raise ValueError(f"scheme {scheme!r} needs `above` (who was in the "
                         f"division above, per season)")
    ordered = sorted(matches, key=lambda m: m["date"])
    rosters = dict(rosters) if rosters else {}
    for s, teams in season_rosters(ordered).items():
        rosters.setdefault(s, set()).update(teams)
    seasons = sorted(rosters)
    first = seasons[0] if seasons else None

    rating, last_seen = {}, {}
    arrived = {}  # club -> season it last entered this division
    entered_seasons = set()

    def enter(season):
        """Apply the entry rule for `season` once, before its first match."""
        if season in entered_seasons:
            return
        entered_seasons.add(season)
        cur = rosters[season]
        if season == first or (season - 1) not in rosters:
            for t in cur:
                rating.setdefault(t, start_rating)
                arrived.setdefault(t, season)
            return
        from_above, from_below, left_up, left_down = classify_arrivals(
            rosters, above or {}, season)
        new = from_above | from_below
        continuing = cur - new
        if scheme == "carry":
            for t in new:
                rating[t] = last_seen.get(t, start_rating)
        elif scheme == "replace":
            up = _mean((rating[t] for t in left_up), start_rating)
            down = _mean((rating[t] for t in left_down), start_rating)
            for t in from_above:
                rating[t] = up
            for t in from_below:
                rating[t] = down
        else:  # offset
            base = _mean((rating[t] for t in continuing), start_rating)
            for t in from_above:
                rating[t] = base + delta_above
            for t in from_below:
                rating[t] = base + delta_below
        for t in new:
            arrived[t] = season
        # clubs that left keep a memory only for "carry"
        for t in (rosters[season - 1] - cur):
            last_seen[t] = rating.pop(t)

    rows = []
    for m in ordered:
        s = m["season"]
        enter(s)
        h, a = m["home_team"], m["away_team"]
        eh, ea = rating[h], rating[a]
        d_eff = eh - ea + home_adv
        hs, as_ = int(m["home_score"]), int(m["away_score"])
        rows.append({"date": m["date"], "season": s, "home_team": h,
                     "away_team": a, "hs": hs, "as_": as_,
                     "elo_home": eh, "elo_away": ea, "d_eff": d_eff,
                     "home_new": arrived.get(h) == s and s != first,
                     "away_new": arrived.get(a) == s and s != first})
        w = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        we = 1.0 / (1.0 + 10.0 ** (-d_eff / 400.0))
        delta = k * gd_mult(abs(hs - as_)) * (w - we)
        rating[h] = eh + delta
        rating[a] = ea - delta
    for s in seasons:   # a season in `rosters` with no match yet
        enter(s)
    return rows, dict(rating)


def _selfcheck():
    fails = []

    def mk(date, season, h, a, hs, as_):
        return {"date": date, "season": season, "home_team": h,
                "away_team": a, "home_score": hs, "away_score": as_}

    # season 1: A, B, C, D. Summer: A goes up, D goes down; U arrives from
    # above, L from below. Season 2: B, C, U, L.
    s1 = [mk("2001-08-01", 2001, "A", "D", 3, 0), mk("2001-08-02", 2001, "B", "C", 1, 1),
          mk("2001-09-01", 2001, "A", "B", 2, 0), mk("2001-09-02", 2001, "C", "D", 1, 0),
          mk("2001-10-01", 2001, "D", "A", 0, 1), mk("2001-10-02", 2001, "C", "B", 0, 0)]
    s2 = [mk("2002-08-01", 2002, "U", "L", 1, 0), mk("2002-08-02", 2002, "B", "C", 0, 0)]
    above = {2001: {"U", "X"}, 2002: {"A", "X"}}

    rows, r_end_s1 = replay_division(s1, 20, 50, "carry")
    if any(r["home_new"] or r["away_new"] for r in rows):
        fails.append("the first season is burn-in: nobody is 'new' in it")
    if abs(sum(r_end_s1.values()) - 4 * 1500.0) > 1e-9:
        fails.append("Elo within one season must conserve the rating sum")
    a_end, d_end = r_end_s1["A"], r_end_s1["D"]
    if not a_end > 1500 > d_end:
        fails.append(f"A won everything, D lost: {r_end_s1}")

    # a row's pre-match ratings must not contain its own result
    rows_a, _ = replay_division(s1[:1], 20, 50, "carry")
    if rows_a[0]["elo_home"] != 1500.0 or rows_a[0]["d_eff"] != 50.0:
        fails.append("pre-match rating leaked the match's own result")

    rows, _ = replay_division(s1 + s2, 20, 50, "replace", above=above)
    u = next(r for r in rows if r["home_team"] == "U")
    if abs(u["elo_home"] - a_end) > 1e-9 or abs(u["elo_away"] - d_end) > 1e-9:
        fails.append(f"replace: U should enter at A's {a_end:.2f} (left up) "
                     f"and L at D's {d_end:.2f} (left down), got "
                     f"{u['elo_home']:.2f}/{u['elo_away']:.2f}")
    if not (u["home_new"] and u["away_new"]):
        fails.append("arrivals must be flagged new in their first season")
    bc = next(r for r in rows if r["home_team"] == "B" and r["season"] == 2002)
    if bc["home_new"] or bc["away_new"]:
        fails.append("continuing clubs must not be flagged new")

    rows, _ = replay_division(s1 + s2, 20, 50, "offset", above=above,
                              delta_above=100.0, delta_below=-80.0)
    base = (r_end_s1["B"] + r_end_s1["C"]) / 2
    u = next(r for r in rows if r["home_team"] == "U")
    if abs(u["elo_home"] - (base + 100)) > 1e-9 or abs(u["elo_away"] - (base - 80)) > 1e-9:
        fails.append("offset: arrivals must enter at the continuing mean + delta")

    # planted fault: had L (not U) been above last season, U would be the
    # arrival from below and take D's rating. Proves the direction is read
    # from `above`, not assumed from the order clubs appear in.
    swapped = {2001: {"L", "X"}, 2002: {"A", "X"}}
    rows_sw, _ = replay_division(s1 + s2, 20, 50, "replace", above=swapped)
    u_sw = next(r for r in rows_sw if r["home_team"] == "U")
    if abs(u_sw["elo_home"] - d_end) > 1e-9:
        fails.append("arrival direction must come from `above`, not be assumed")

    # carry: a returning club keeps its stale rating, a stranger gets 1500
    s3 = [mk("2003-08-01", 2003, "A", "B", 1, 0), mk("2003-08-02", 2003, "C", "Z", 1, 0)]
    rows, _ = replay_division(s1 + s2 + s3, 20, 50, "carry")
    ab = next(r for r in rows if r["season"] == 2003 and r["home_team"] == "A")
    cz = next(r for r in rows if r["season"] == 2003 and r["home_team"] == "C")
    if abs(ab["elo_home"] - a_end) > 1e-9:
        fails.append("carry must restore a returning club's last rating")
    if cz["elo_away"] != 1500.0:
        fails.append("carry must give a never-seen club start_rating")

    # a season in `rosters` with no match yet still gets its entry rule
    _, pre = replay_division(s1, 20, 50, "replace", above=above,
                             rosters={2002: {"B", "C", "U", "L"}})
    if abs(pre.get("U", 0) - a_end) > 1e-9 or "A" in pre:
        fails.append("a pre-season roster must apply the entry rule and drop leavers")

    # "carry" over a single division equals m_dc.replay_df's d_eff
    import pandas as pd
    from football_ai.models.m_dc import replay_df
    frame = pd.DataFrame([dict(m, neutral=False, tournament="x") for m in s1 + s2 + s3])
    ref = replay_df(frame, lambda _t: 20, home_adv=50)
    mine, _ = replay_division(s1 + s2 + s3, 20, 50, "carry")
    if max(abs(x - y) for x, y in zip(ref.d_eff, (r["d_eff"] for r in mine))) > 1e-9:
        fails.append("carry must reproduce m_dc.replay_df exactly")

    for bad in (lambda: replay_division(s1, 20, 50, "nope"),
                lambda: replay_division(s1, 20, 50, "replace")):
        try:
            bad()
            fails.append("bad scheme / missing `above` must raise")
        except ValueError:
            pass
    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
