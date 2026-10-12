"""Planted-fault checks for backtest.py. Offline: synthetic data only.

1. The vectorized score log-loss equals m_dc._score_logloss.
2. The bootstrap CI covers a known mean at about the nominal rate, and is
   reproducible for a fixed seed.
3. Selection cannot see the test window: rewriting every test-window result
   leaves the chosen parameters and goal curve unchanged.
4. Scoring: RPS sign convention (positive d = the close was better), the
   gap/edge definitions and the ROI leg arithmetic, on hand-made matches.
5. The pyramid invariant check flags a club spelled two ways.

Usage (repo root): python research/championship_backtest/selftest.py
"""
import math
import random
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import backtest as B  # noqa: E402
from football_ai.models.m_dc import _score_logloss  # noqa: E402


def synthetic_league(seed=7, seasons=range(2010, 2026), n_teams=8):
    """A tiny two-division world: strengths drift, 1 club up / 1 down a year."""
    rng = random.Random(seed)
    strength = {f"T{i}": rng.gauss(0, 0.3) for i in range(n_teams)}
    league = sorted(strength)[:6]
    above = {}
    matches = []
    for s in seasons:
        above.setdefault(s, set()).update({f"UP{s}", f"UP{s}b"})
        for h in league:
            for a in league:
                if h == a:
                    continue
                lam = math.exp(0.25 + strength[h] - strength[a] + 0.15)
                mu = math.exp(0.25 + strength[a] - strength[h])
                day = rng.randint(0, 250)
                matches.append({
                    "date": (np.datetime64(f"{s}-08-01")
                             + np.timedelta64(day, "D")).astype(str),
                    "season": s, "home_team": h, "away_team": a,
                    "home_score": int(np.random.default_rng(rng.randint(0, 10**9)).poisson(lam)),
                    "away_score": int(np.random.default_rng(rng.randint(0, 10**9)).poisson(mu))})
        # summer: the best club leaves up, the worst down, two arrive
        ranked = sorted(league, key=lambda t: strength[t])
        up_club, down_club = ranked[-1], ranked[0]
        newcomer_up = f"UP{s}"           # was above in season s
        newcomer_down = f"N{s}"
        strength[newcomer_up] = 0.3
        strength[newcomer_down] = -0.3
        league = [t for t in league if t not in (up_club, down_club)] + [newcomer_up, newcomer_down]
        above.setdefault(s + 1, set()).add(up_club)
    return matches, above


def main():
    fails = []

    # 1. vectorized log-loss == the project's scalar implementation
    rng = np.random.default_rng(1)
    d = rng.normal(0, 150, 400)
    hs = rng.poisson(1.5, 400)
    as_ = rng.poisson(1.1, 400)
    hs[0], as_[0] = 12, 0          # beyond MAX_GOALS: both must cap
    for rho in (-0.08, 0.0, 0.05):
        ref = _score_logloss(list(zip(d, hs, as_)), 0.25, 0.17, rho)
        mine = B.score_logloss(d, hs, as_, 0.25, 0.17, rho)
        if abs(ref - mine) > 1e-10:
            fails.append(f"score_logloss != m_dc._score_logloss at rho={rho}: {mine} vs {ref}")

    # 2. bootstrap: coverage near 95% for a known mean; same seed, same CI
    cover = 0
    for i in range(200):
        x = np.random.default_rng(100 + i).normal(0.01, 0.1, 300)
        b = B.boot_ci(x, n_boot=1000, seed=i)
        cover += b["ci"][0] <= 0.01 <= b["ci"][1]
    if not 0.89 <= cover / 200 <= 0.99:
        fails.append(f"bootstrap 95% CI covered the true mean {cover}/200 times")
    x = np.random.default_rng(5).normal(0, 1, 50)
    if B.boot_ci(x, n_boot=500) != B.boot_ci(x, n_boot=500):
        fails.append("bootstrap must be reproducible for a fixed seed")
    if B.boot_ci([]) is not None:
        fails.append("an empty subset must report nothing, not a fake CI")

    # 3. selection never sees the test window
    matches, above = synthetic_league()
    small = list(B.candidates(("carry", "offset"), k_grid=(15, 25), ha_grid=(45, 75),
                              da_grid=(0, 50), db_grid=(-50, 0)))
    best1, _ = B.select(matches, above, small)
    planted = [dict(m, home_score=m["away_score"] + 3, away_score=0)
               if m["date"] >= B.TEST_FROM else m for m in matches]
    best2, _ = B.select(planted, above, small)
    if best1 != best2:
        fails.append(f"rewriting test-window results changed the selection: {best1} vs {best2}")
    # ...and the planted change is real: the full replay does see it
    r1 = [r["d_eff"] for r in B.replay(matches, above, best1) if r["date"] >= B.TEST_FROM]
    r2 = [r["d_eff"] for r in B.replay(planted, above, best1) if r["date"] >= B.TEST_FROM]
    if r1 == r2:
        fails.append("planted test-window change had no effect at all; check 3 proves nothing")

    # 4. scoring on hand-made rows
    curve = (0.25, 0.17, -0.03)
    row = {"date": "2024-01-01", "season": 2023, "home_team": "H", "away_team": "A",
           "hs": 2, "as_": 0, "d_eff": 200.0, "home_new": False, "away_new": True}
    close_home_fav = {("2024-01-01", "H", "A"): (1.30, 5.5, 11.0)}
    s = B.score_test([row], close_home_fav, curve)[0]
    if not s["new"]:
        fails.append("a match with an arriving club must be flagged new")
    pm = s["p_m"]
    want_edge = max(pm[i] - s["p_close"][i] for i in range(3))
    if abs(s["edge"] - want_edge) > 1e-12 or s["gap_abs"] < abs(s["edge"]) - 1e-12:
        fails.append("edge must be the largest positive M-minus-close difference, "
                     "gap_abs the largest absolute one")
    odds = (1.30, 5.5, 11.0)
    want_profit = odds[s["side"]] - 1.0 if s["side"] == 0 else -1.0
    if abs(s["profit"] - want_profit) > 1e-12:
        fails.append(f"ROI leg wrong: {s['profit']} vs {want_profit}")
    # the close nails it, M does not -> d > 0 (close better)
    sharp = B.score_test([row], {("2024-01-01", "H", "A"): (1.01, 60.0, 120.0)}, curve)[0]
    if not sharp["rps_m"] - sharp["rps_close"] > 0:
        fails.append("sign: a near-certain correct close must beat M (d > 0)")
    if B.score_test([row], {}, curve):
        fails.append("a match with no closing price must be dropped, not scored")
    roi = B.summarize([s] * 3)["roi"]
    if roi["0.0"]["n"] != (3 if s["edge"] >= 0 else 0):
        fails.append("ROI at threshold 0 must bet every match with a positive edge")

    # 5a. seasons come from the file, not the date: 2019-20 ended in late
    # July 2020, and a date rule would file those matches under 2020-21
    real_load = B.load_seasons
    B.load_seasons = lambda division, years, **kw: (
        [{"date": "2020-07-22", "home_team": "A", "away_team": "B",
          "home_score": 1, "away_score": 0}] if list(years) == [2019] else [])
    try:
        tagged = B.load_results("E1", [2019, 2020])
    finally:
        B.load_seasons = real_load
    if [r["season"] for r in tagged] != [2019]:
        fails.append(f"a July 2020 match from the 2019-20 file must be season 2019: {tagged}")

    # 5b. the pyramid check catches a club spelled two ways
    rosters = {2020: {"A", "B", "C"}, 2021: {"A", "B", "Cee"}}
    probs = B.check_pyramid(rosters, {2020: set(), 2021: set()}, {2020: set(), 2021: set()},
                            2020, 2021, n_above=0, n_below=1)
    if not probs:
        fails.append("pyramid check must flag 'C' -> 'Cee' (not found below)")
    ok = B.check_pyramid({2020: {"A", "B", "C"}, 2021: {"A", "B", "D"}},
                         {}, {2020: {"D"}, 2021: {"C"}}, 2020, 2021, n_above=0, n_below=1)
    if ok:
        fails.append(f"a clean one-down / one-up summer must pass: {ok}")

    print("OK" if not fails else "FAILURES:\n  " + "\n  ".join(fails))
    return fails


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
