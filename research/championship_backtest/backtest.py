"""EFL Championship backtest: the EPL's M (Elo + Dixon-Coles) against the
Pinnacle closing price, out of time. Research, not a price-blind instrument:
it reads historical closing odds to SCORE M, never to build it.

Question (owner, 2026-10-10): in the Championship -- a softer market by the
charter's own account (DECISION_PROTOCOL section 10) -- how does the same M
fare against the closing price that beat it in the EPL (+0.0075 RPS over
970 matches)?

Protocol, fixed before the test window was scored:
  - M is built from a PRICE-FREE load of results (`load_seasons` without
    `with_closing_odds`); prices come from a second load, joined only for
    scoring. `main()` asserts the M inputs carry no price field.
  - Elo replay from 2005-06 (burn-in), one division at a time, with an
    entry-rating rule for clubs arriving from the division above or below
    (football_ai/models/division_elo.py).
  - Fit window 2015-08-01 .. 2023-08-01 (the EPL's own fit window). K, home
    advantage and the entry rule are chosen there by mean exact-score
    log-loss, the EPL's criterion; the Dixon-Coles goal curve is fitted
    there with m_dc.fit_goals / fit_rho. Nothing is re-chosen after the
    test window is seen.
  - Test window 2023-08-01 .. 2026-07-01, every match with a Pinnacle close
    (football-data.co.uk stopped carrying it partway through 2025-26). Elo
    keeps updating through the test window from results, as it would live.
  - Close = Shin-devigged PSCH/PSCD/PSCA (market.devig.devig_shin).
    d = RPS(M) - RPS(close) per match; positive = the close was better (the
    EPL known_biases sign). 95% CI by match bootstrap, 10,000 resamples.
  - gap = max over home/draw/away of |p_M - p_close| (the M gap ops_tools
    reports); the ">=10pp subset" uses it. The gap-only ROI stakes 1u at the
    closing price on the outcome with the largest POSITIVE p_M - p_close
    when that is >= the threshold.
  - The same code, rerun on the EPL (E0) with its declared M, is the
    comparison row; it must reproduce the recorded +0.0075 over 970.

Run from the repo root:  python research/championship_backtest/backtest.py
Writes research/championship_backtest/results.json; prints a summary.
"""
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from football_ai.data.football_data_co_uk import load_seasons  # noqa: E402
from football_ai.market.devig import devig_shin  # noqa: E402
from football_ai.models.division_elo import (  # noqa: E402
    classify_arrivals, replay_division, season_rosters)
from football_ai.models.m_dc import (  # noqa: E402
    MAX_GOALS, fit_goals, fit_rho, outcome_probs)
from football_ai.research.metrics import rps  # noqa: E402

OUT = Path(__file__).resolve().parent / "results.json"

REPLAY_FROM = 2005          # 2005-06; earlier E0 files do not decode as UTF-8
LAST_COMPLETE = 2025        # 2025-26, the last completed season
FIT_FROM, TEST_FROM, TEST_TO = "2015-08-01", "2023-08-01", "2026-07-01"
K_GRID = (10, 15, 20, 25, 30, 35, 40)
HA_GRID = (30, 45, 60, 75, 90, 105, 120)
DELTA_ABOVE_GRID = tuple(range(0, 301, 25))
DELTA_BELOW_GRID = tuple(range(-300, 1, 25))
THRESHOLDS_PP = (0.0, 2.5, 5.0, 6.5, 10.0, 15.0)
N_BOOT = 10_000
SEED = 20261010

# The EPL measurement this backtest is compared with (CONFIG.known_biases of
# football_ai/competitions/epl_2026_27.py; HANDOFF "已確立的結論").
EPL_RECORDED = {"n": 970, "d": 0.0075, "ci": [0.0039, 0.0110],
                "sub10_d": 0.0225, "sub10_ci": [0.0016, 0.0435]}
# The EPL's declared M (calibrations record, era v1-epl-2026-27).
EPL_DECLARED = {"k": 25, "home_adv": 60.0, "replay_from": 2015, "scheme": "carry"}


# --- data ------------------------------------------------------------------

def load_results(division, years):
    """Price-free results, tagged with the season of the FILE they came from.
    Never infer the season from the date: 2019-20 ran into late July 2020
    (COVID), and a month >= 7 rule files those matches under 2020-21 --
    the pyramid check caught exactly that on the first run (2026-10-10)."""
    rows = []
    for y in years:
        for r in load_seasons(division, [y]):
            r["season"] = y
            rows.append(r)
    rows.sort(key=lambda r: r["date"])
    return rows


def load_closing(division, years):
    """{(date, home, away): (PSCH, PSCD, PSCA)} -- scoring only."""
    out = {}
    for r in load_seasons(division, years, with_closing_odds=True):
        if "pinnacle_close_h" in r:
            key = (r["date"], r["home_team"], r["away_team"])
            if key in out:
                raise ValueError(f"duplicate fixture in the closing load: {key}")
            out[key] = (r["pinnacle_close_h"], r["pinnacle_close_d"],
                        r["pinnacle_close_a"])
    return out


def membership(division, years):
    return {y: {t for r in load_seasons(division, [y])
                for t in (r["home_team"], r["away_team"])} for y in years}


def check_pyramid(rosters, above, below, first, last, n_above=3, n_below=3):
    """Mechanical invariants (AGENT_DISPATCH section 5, data source): every
    summer exactly n_above clubs arrive from / leave to the division above
    and n_below from / to the one below, and every arrival / departure is
    found in the adjacent division -- so a club spelled differently across
    files cannot pass as a 'new' club."""
    problems = []
    for s in range(first + 1, last + 1):
        fa, fb, lu, ld = classify_arrivals(rosters, above, s)
        if not (len(fa) == len(lu) == n_above and len(fb) == len(ld) == n_below):
            problems.append(f"{s}: from_above={sorted(fa)} from_below={sorted(fb)} "
                            f"left_up={sorted(lu)} left_down={sorted(ld)}")
        if below is not None:
            stray = (fb - below.get(s - 1, set())) | (ld - below.get(s, set()))
            if stray:
                problems.append(f"{s}: not found in the division below: {sorted(stray)}")
    return problems


# --- the goal curve and its score log-loss -----------------------------------

def _pois_table(lam):
    xs = np.arange(MAX_GOALS + 1)
    logf = np.array([math.lgamma(x + 1) for x in xs])
    return np.exp(-lam[:, None] + xs[None, :] * np.log(lam)[:, None] - logf[None, :])


def score_logloss(d_eff, hs, as_, g_a, g_b, rho):
    """Mean exact-score NLL, vectorized; equal to m_dc._score_logloss (the
    selftest checks it). The DC taus cancel in total mass, so normalizing by
    the product of the two truncated Poisson masses is exact."""
    d = np.asarray(d_eff, float) / 100.0
    lam = np.exp(np.clip(g_a + g_b * d, -10, 5))
    mu = np.exp(np.clip(g_a - g_b * d, -10, 5))
    h = np.minimum(np.asarray(hs, int), MAX_GOALS)
    a = np.minimum(np.asarray(as_, int), MAX_GOALS)
    ph, pa = _pois_table(lam), _pois_table(mu)
    idx = np.arange(len(d))
    p = ph[idx, h] * pa[idx, a]
    tau = np.ones_like(p)
    tau = np.where((h == 0) & (a == 0), 1.0 - lam * mu * rho, tau)
    tau = np.where((h == 1) & (a == 0), 1.0 + mu * rho, tau)
    tau = np.where((h == 0) & (a == 1), 1.0 + lam * rho, tau)
    tau = np.where((h == 1) & (a == 1), 1.0 - rho, tau)
    z = ph.sum(axis=1) * pa.sum(axis=1)
    return float(-np.log(np.maximum(p * tau / z, 1e-300)).mean())


def fit_curve(rows):
    d = np.array([r["d_eff"] for r in rows])
    hs = np.array([r["hs"] for r in rows])
    as_ = np.array([r["as_"] for r in rows])
    g_a, g_b = fit_goals(d, hs, as_)
    rho = fit_rho(d, hs, as_, g_a, g_b)
    return g_a, g_b, rho, score_logloss(d, hs, as_, g_a, g_b, rho)


def in_window(rows, lo, hi):
    return [r for r in rows if lo <= r["date"] < hi]


# --- choosing M on the fit window only ---------------------------------------

def candidates(schemes, k_grid=K_GRID, ha_grid=HA_GRID,
               da_grid=DELTA_ABOVE_GRID, db_grid=DELTA_BELOW_GRID):
    for scheme in schemes:
        for k in k_grid:
            for ha in ha_grid:
                if scheme == "offset":
                    for da in da_grid:
                        for db in db_grid:
                            yield {"scheme": scheme, "k": k, "home_adv": float(ha),
                                   "delta_above": float(da), "delta_below": float(db)}
                else:
                    yield {"scheme": scheme, "k": k, "home_adv": float(ha),
                           "delta_above": 0.0, "delta_below": 0.0}


def grid_edges(best, grid):
    """Parameters whose chosen value sits on the edge of a searched grid --
    an optimum there is not an optimum, so it is reported, not hidden."""
    axes = [("k", grid["k_grid"]), ("home_adv", grid["ha_grid"])]
    if best["scheme"] == "offset":
        axes += [("delta_above", grid["da_grid"]), ("delta_below", grid["db_grid"])]
    return [name for name, g in axes
            if len(g) > 1 and best[name] in (min(g), max(g))]


def replay(matches, above, params):
    rows, _ = replay_division(matches, params["k"], params["home_adv"],
                              params["scheme"], above=above,
                              delta_above=params["delta_above"],
                              delta_below=params["delta_below"])
    return rows


def select(matches, above, cands, fit_to=TEST_FROM):
    """Best candidate by fit-window score log-loss. Only matches dated
    before `fit_to` are passed to the replay, so a test-window result cannot
    influence the choice even in principle (the selftest plants one)."""
    pre = [m for m in matches if m["date"] < fit_to]
    best, table = None, []
    for p in cands:
        rows = in_window(replay(pre, above, p), FIT_FROM, fit_to)
        g_a, g_b, rho, ll = fit_curve(rows)
        rec = dict(p, g_a=g_a, g_b=g_b, rho=rho, fit_logloss=ll, n_fit=len(rows))
        table.append(rec)
        if best is None or ll < best["fit_logloss"]:
            best = rec
    return best, table


# --- scoring against the close ----------------------------------------------

def outcome_index(hs, as_):
    return 0 if hs > as_ else (1 if hs == as_ else 2)


def score_test(rows, closing, curve):
    """Per test match: M probs, Shin close, RPS of each, gaps, ROI legs."""
    g_a, g_b, rho = curve
    out = []
    for r in rows:
        odds = closing.get((r["date"], r["home_team"], r["away_team"]))
        if odds is None:
            continue
        d = r["d_eff"] / 100.0
        pm = outcome_probs(math.exp(g_a + g_b * d), math.exp(g_a - g_b * d), rho)
        pc = devig_shin(list(odds))
        o = outcome_index(r["hs"], r["as_"])
        diffs = [pm[i] - pc[i] for i in range(3)]
        side = int(np.argmax(diffs))
        out.append({"date": r["date"], "season": r["season"],
                    "home_team": r["home_team"], "away_team": r["away_team"],
                    "new": bool(r["home_new"] or r["away_new"]),
                    "p_m": [float(x) for x in pm], "p_close": pc,
                    "rps_m": rps(pm, o), "rps_close": rps(pc, o),
                    "gap_abs": max(abs(x) for x in diffs),
                    "edge": diffs[side], "side": side,
                    "profit": (odds[side] - 1.0) if side == o else -1.0,
                    "overround": sum(1.0 / x for x in odds) - 1.0,
                    "outcome": o})
    return out


def boot_diff(x, y, n_boot=N_BOOT, seed=SEED):
    """mean(x) - mean(y) for two independent samples (two leagues), each
    resampled by match; 95% percentile CI."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    rng = np.random.default_rng(seed)
    diffs = np.array([x[rng.integers(0, len(x), len(x))].mean()
                      - y[rng.integers(0, len(y), len(y))].mean()
                      for _ in range(n_boot)])
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {"mean": float(x.mean() - y.mean()), "ci": [float(lo), float(hi)]}


def boot_ci(x, n_boot=N_BOOT, seed=SEED):
    """Mean and 95% percentile CI, resampling matches with replacement."""
    x = np.asarray(x, float)
    if len(x) == 0:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    step = 1000
    for i in range(0, n_boot, step):
        idx = rng.integers(0, len(x), size=(min(step, n_boot - i), len(x)))
        means[i:i + len(idx)] = x[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"n": int(len(x)), "mean": float(x.mean()), "ci": [float(lo), float(hi)]}


def summarize(scored):
    d = [s["rps_m"] - s["rps_close"] for s in scored]
    pick = lambda f: [s["rps_m"] - s["rps_close"] for s in scored if f(s)]  # noqa: E731
    res = {
        "n": len(scored),
        "rps_m": float(np.mean([s["rps_m"] for s in scored])),
        "rps_close": float(np.mean([s["rps_close"] for s in scored])),
        "d": boot_ci(d),
        "d_gap_abs_ge10": boot_ci(pick(lambda s: s["gap_abs"] >= 0.10)),
        "d_edge_ge10": boot_ci(pick(lambda s: s["edge"] >= 0.10)),
        "buckets_gap_abs": {},
        "by_season": {},
        "newcomer_match": boot_ci(pick(lambda s: s["new"])),
        "no_newcomer": boot_ci(pick(lambda s: not s["new"])),
        "gap_abs_quantiles_pp": [float(q) for q in np.percentile(
            [100 * s["gap_abs"] for s in scored], [50, 70, 88, 95])],
        # the gate's input is the largest POSITIVE gap; the EPL's 6.5/10pp
        # are about its 70th/88th percentile (reproduced on E0 below)
        "edge_quantiles_pp": [float(q) for q in np.percentile(
            [100 * s["edge"] for s in scored], [50, 70, 88, 95])],
        "overround": float(np.mean([s["overround"] for s in scored])),
        "draw_rate": {"m": float(np.mean([s["p_m"][1] for s in scored])),
                      "close": float(np.mean([s["p_close"][1] for s in scored])),
                      "actual": float(np.mean([s["outcome"] == 1 for s in scored]))},
        "roi": {},
    }
    for lo, hi in ((0, .03), (.03, .06), (.06, .10), (.10, 9)):
        res["buckets_gap_abs"][f"{int(lo * 100)}-{int(hi * 100) if hi < 9 else 'inf'}pp"] = \
            boot_ci(pick(lambda s, lo=lo, hi=hi: lo <= s["gap_abs"] < hi))
    for season in sorted({s["season"] for s in scored}):
        res["by_season"][str(season)] = boot_ci(pick(lambda s, y=season: s["season"] == y))
    for t in THRESHOLDS_PP:
        legs = [s["profit"] for s in scored if s["edge"] * 100 >= t]
        res["roi"][str(t)] = boot_ci(legs)
    return res


# --- one league, end to end --------------------------------------------------

def run_league(division, above, below, schemes, replay_from, label,
               fixed=None, grid=None, rho0=False):
    years = range(replay_from, LAST_COMPLETE + 1)
    matches = load_results(division, years)
    if any(k.startswith("pinnacle") for m in matches for k in m):
        raise AssertionError("M's inputs must be price-free")
    rosters = season_rosters(matches)
    # the top flight has no division above: 0 arrive from above, 3 from below
    pyramid = check_pyramid(rosters, above, below, replay_from, LAST_COMPLETE,
                            n_above=3 if above else 0) \
        if above is not None else []
    if pyramid:
        raise AssertionError(f"{label}: promotion/relegation invariants fail, "
                             f"the entry rule would misfire: {pyramid}")
    if fixed is not None:
        pre = [m for m in matches if m["date"] < TEST_FROM]
        g_a, g_b, rho, ll = fit_curve(in_window(replay(pre, above, fixed), FIT_FROM, TEST_FROM))
        best, table = dict(fixed, g_a=g_a, g_b=g_b, rho=rho, fit_logloss=ll), []
    else:
        grid = dict({"k_grid": K_GRID, "ha_grid": HA_GRID, "da_grid": DELTA_ABOVE_GRID,
                     "db_grid": DELTA_BELOW_GRID}, **(grid or {}))
        best, table = select(matches, above, candidates(schemes, **grid))
        best["on_grid_edge"] = grid_edges(best, grid)
    rows = replay(matches, above, best)
    test = in_window(rows, TEST_FROM, TEST_TO)
    # The EPL's Dixon-Coles admission gate (m_dc.run_gate): out of time, on
    # every test match, DC must beat independent Poisson on exact-score
    # log-loss. Results only.
    tri = ([r["d_eff"] for r in test], [r["hs"] for r in test], [r["as_"] for r in test])
    dc_gate = {"n": len(test),
               "logloss_dc": score_logloss(*tri, best["g_a"], best["g_b"], best["rho"]),
               "logloss_independent": score_logloss(*tri, best["g_a"], best["g_b"], 0.0)}
    dc_gate["dc_better"] = dc_gate["logloss_dc"] < dc_gate["logloss_independent"]
    if rho0:   # sensitivity: the same M with the DC correction switched off
        best = dict(best, rho=0.0)
    closing = load_closing(division, range(2023, LAST_COMPLETE + 1))
    scored = score_test(test, closing, (best["g_a"], best["g_b"], best["rho"]))
    out = {"label": label, "division": division, "replay_from": replay_from,
           "params": best, "pyramid_problems": pyramid, "dc_gate": dc_gate,
           "n_test_matches": len(test), "n_test_with_close": len(scored),
           "summary": summarize(scored)}
    # per-scheme best on the fit window (selection evidence, not test results)
    if table:
        out["fit_window_best_by_scheme"] = {
            s: min((t for t in table if t["scheme"] == s), key=lambda t: t["fit_logloss"])
            for s in schemes}
    return out, scored


def main():
    t0 = time.time()
    e0 = membership("E0", range(REPLAY_FROM, LAST_COMPLETE + 1))
    e2 = membership("E2", range(REPLAY_FROM, LAST_COMPLETE + 1))
    results = {"protocol": {
        "replay_from": REPLAY_FROM, "fit_window": [FIT_FROM, TEST_FROM],
        "test_window": [TEST_FROM, TEST_TO], "k_grid": K_GRID, "ha_grid": HA_GRID,
        "delta_above_grid": DELTA_ABOVE_GRID, "delta_below_grid": DELTA_BELOW_GRID,
        "selection": "min mean exact-score log-loss on the fit window",
        "n_boot": N_BOOT, "seed": SEED, "epl_recorded": EPL_RECORDED}}

    # PRIMARY: Championship, entry rule chosen on the fit window.
    primary, scored = run_league("E1", e0, e2, ("carry", "replace", "offset"),
                                 REPLAY_FROM, "E1 primary")
    results["e1"] = primary
    # SENSITIVITY (declared with the protocol, not chosen after): each entry
    # rule at its own fit-window optimum.
    results["e1_by_scheme"] = {}
    for scheme, p in primary["fit_window_best_by_scheme"].items():
        fixed = {k: p[k] for k in ("scheme", "k", "home_adv", "delta_above", "delta_below")}
        r, _ = run_league("E1", e0, e2, (scheme,), REPLAY_FROM, f"E1 {scheme}", fixed=fixed)
        results["e1_by_scheme"][scheme] = r
    # SENSITIVITY (added after the first run showed Dixon-Coles failing the
    # EPL's admission gate on E1): the primary M with rho = 0.
    fixed = {k: primary["params"][k]
             for k in ("scheme", "k", "home_adv", "delta_above", "delta_below")}
    results["e1_rho0"], _ = run_league("E1", e0, e2, (fixed["scheme"],), REPLAY_FROM,
                                       "E1 rho=0", fixed=fixed, rho0=True)

    # COMPARISON 1: the EPL's declared M, exactly as recorded.
    epl, s_epl = run_league("E0", None, None, ("carry",), EPL_DECLARED["replay_from"],
                            "E0 declared M", fixed={"scheme": "carry", "k": EPL_DECLARED["k"],
                                                    "home_adv": EPL_DECLARED["home_adv"],
                                                    "delta_above": 0.0, "delta_below": 0.0})
    results["e0_declared"] = epl
    # COMPARISON 2: the EPL under this backtest's protocol (same burn-in, same
    # fit-window selection), so the two leagues differ only in the league.
    e1_membership = membership("E1", range(REPLAY_FROM, LAST_COMPLETE + 1))
    e0_same, s_same = run_league("E0", {}, e1_membership, ("carry", "replace", "offset"),
                                 REPLAY_FROM, "E0 same protocol",
                                 grid={"da_grid": (0,)})
    results["e0_same_protocol"] = e0_same

    # E1 minus E0, two independent samples: is the Championship gap smaller?
    def dd(sc, f=lambda s: True):
        return [s["rps_m"] - s["rps_close"] for s in sc if f(s)]
    big = lambda s: s["gap_abs"] >= 0.10  # noqa: E731
    results["e1_minus_e0"] = {
        "d_vs_declared": boot_diff(dd(scored), dd(s_epl)),
        "d_vs_same_protocol": boot_diff(dd(scored), dd(s_same)),
        "d_gap_abs_ge10_vs_declared": boot_diff(dd(scored, big), dd(s_epl, big)),
    }

    results["elapsed_s"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False, default=float),
                   encoding="utf-8")
    report(results)
    return results


def _fmt(b, scale=1.0, pct=False):
    if not b:
        return "n=0"
    f = (lambda v: f"{100 * v:+.1f}%") if pct else (lambda v: f"{scale * v:+.4f}")
    return f"{f(b['mean'])} [{f(b['ci'][0])}, {f(b['ci'][1])}] n={b['n']}"


def report(results):
    for key in ("e1", "e0_declared", "e0_same_protocol"):
        r = results[key]
        s, p = r["summary"], r["params"]
        print(f"== {r['label']} ({r['division']}): {p['scheme']} K={p['k']} HA={p['home_adv']:g} "
              f"da={p['delta_above']:g} db={p['delta_below']:g} | g_a={p['g_a']:.4f} "
              f"g_b={p['g_b']:.4f} rho={p['rho']:.4f}")
        if r["pyramid_problems"]:
            print("   PYRAMID PROBLEMS:", r["pyramid_problems"])
        print(f"   test n={s['n']} RPS M={s['rps_m']:.4f} close={s['rps_close']:.4f} "
              f"overround={100 * s['overround']:.2f}% draw M/close/actual="
              f"{s['draw_rate']['m']:.3f}/{s['draw_rate']['close']:.3f}/"
              f"{s['draw_rate']['actual']:.3f}")
        print(f"   |gap| q50/70/88/95 {[round(q, 1) for q in s['gap_abs_quantiles_pp']]}  "
              f"edge q50/70/88/95 {[round(q, 1) for q in s['edge_quantiles_pp']]}")
        print(f"   d all        {_fmt(s['d'])}")
        print(f"   d |gap|>=10  {_fmt(s['d_gap_abs_ge10'])}")
        print(f"   d edge>=10   {_fmt(s['d_edge_ge10'])}")
        print(f"   newcomer     {_fmt(s['newcomer_match'])}   none {_fmt(s['no_newcomer'])}")
        for b, v in s["buckets_gap_abs"].items():
            print(f"   bucket {b:9s} {_fmt(v)}")
        for y, v in s["by_season"].items():
            print(f"   season {y}   {_fmt(v)}")
        for t, v in s["roi"].items():
            print(f"   ROI edge>={t:>4}pp {_fmt(v, pct=True)}")
    for scheme, r in results["e1_by_scheme"].items():
        p = r["params"]
        print(f"-- E1 sensitivity {scheme}: K={p['k']} HA={p['home_adv']:g} "
              f"da={p['delta_above']:g} db={p['delta_below']:g} fit_ll={p['fit_logloss']:.5f} "
              f"| d {_fmt(r['summary']['d'])} | >=10 {_fmt(r['summary']['d_gap_abs_ge10'])}"
              f" | newcomer {_fmt(r['summary']['newcomer_match'])}"
              f" none {_fmt(r['summary']['no_newcomer'])}")
    r0 = results["e1_rho0"]["summary"]
    print(f"-- E1 rho=0: d {_fmt(r0['d'])} | >=10 {_fmt(r0['d_gap_abs_ge10'])}")
    for key in ("e1", "e0_declared", "e0_same_protocol"):
        g = results[key]["dc_gate"]
        print(f"-- DC gate {key}: n={g['n']} dc={g['logloss_dc']:.6f} "
              f"ind={g['logloss_independent']:.6f} dc_better={g['dc_better']}")
    for k, v in results["e1_minus_e0"].items():
        print(f"-- E1 minus E0 {k}: {v['mean']:+.4f} [{v['ci'][0]:+.4f}, {v['ci'][1]:+.4f}]")
    print(f"elapsed {results['elapsed_s']}s -> {OUT}")


if __name__ == "__main__":
    main()
