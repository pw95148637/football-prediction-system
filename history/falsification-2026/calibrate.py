"""Instrument M v2: empirical calibration of Elo-diff -> match probabilities.

Replaces the theoretical chain (Elo expected-score formula + fixed-mu
independent Poisson split) that the 7/3 pilots showed to be overconfident
on favorites. Market-free by construction: fits on historical match RESULTS
only, never on odds.

Pipeline:
 1. Download martj42/international_results (results.csv, 1872->present).
 2. Replay Elo over all matches (eloratings.net-style rules: tiered K,
    goal-diff multiplier, +100 home advantage unless neutral).
 3. On competitive matches since 1990, fit:
    - ordered logit: P(win/draw/loss) as a function of effective Elo diff
    - Poisson goal curves: lambda_for = exp(a + b*d/100)
 4. Validate out-of-time (fit <=2017, test 2018+; RPS vs old skeleton),
    then refit on the full window and write calibration.json.

Run: python wc2026/calibrate.py
"""
import json
import math
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
DATA = BASE / "data" / "results.csv"
OUT = BASE / "calibration.json"
URL = ("https://raw.githubusercontent.com/martj42/international_results/"
       "master/results.csv")

HOME_ADV = 100.0
FIT_FROM = "1990-01-01"
VAL_FROM = "2018-01-01"


def download():
    DATA.parent.mkdir(parents=True, exist_ok=True)
    if DATA.exists():
        print(f"data present: {DATA}")
        return
    print(f"downloading {URL} ...")
    urllib.request.urlretrieve(URL, DATA)
    print(f"saved -> {DATA} ({DATA.stat().st_size/1e6:.1f} MB)")


def k_factor(tournament):
    t = str(tournament).lower()
    if "friendly" in t:
        return 20
    if "qualification" in t or "nations league" in t:
        return 40
    if "fifa world cup" in t:
        return 60
    for major in ("uefa euro", "copa américa", "copa america",
                  "african cup of nations", "africa cup of nations",
                  "afc asian cup", "gold cup", "concacaf championship",
                  "confederations cup", "oceania nations cup"):
        if major in t:
            return 50
    return 30


def gd_mult(gd):
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    if gd == 3:
        return 1.75
    return 1.75 + (gd - 3) / 8.0


def replay():
    df = pd.read_csv(DATA)
    df = df.dropna(subset=["home_score", "away_score"])
    df = df.sort_values("date").reset_index(drop=True)
    rating = {}
    rows = []
    for r in df.itertuples(index=False):
        eh = rating.get(r.home_team, 1500.0)
        ea = rating.get(r.away_team, 1500.0)
        neutral = bool(r.neutral)
        d_eff = eh - ea + (0.0 if neutral else HOME_ADV)
        hs, as_ = int(r.home_score), int(r.away_score)
        w = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        k = k_factor(r.tournament)
        rows.append((r.date, k, d_eff, hs, as_, w))
        we = 1.0 / (1.0 + 10.0 ** (-d_eff / 400.0))
        delta = k * gd_mult(abs(hs - as_)) * (w - we)
        rating[r.home_team] = eh + delta
        rating[r.away_team] = ea - delta
    rec = pd.DataFrame(rows, columns=["date", "k", "d_eff", "hs", "as_", "w"])
    return rec, rating


def fit_ordered_logit(d_eff, w):
    """Latent s = b*(d/100); P(loss)=sig(c1-s), P(loss or draw)=sig(c2-s).
    Newton on (b, c1, c2): analytic gradient, numerical Hessian (3 params)."""
    x = np.asarray(d_eff) / 100.0
    y = np.asarray(w)  # 1 win, 0.5 draw, 0 loss (home perspective)
    is_w, is_d, is_l = (y == 1.0), (y == 0.5), (y == 0.0)

    def sig(z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def gradient(th):
        b_, c1_, c2_ = th
        s = b_ * x
        pl = sig(c1_ - s)
        pld = sig(c2_ - s)
        pd_ = np.clip(pld - pl, 1e-9, 1.0)
        pw = np.clip(1.0 - pld, 1e-9, 1.0)
        g1 = pl * (1.0 - pl)      # d pl / d(c1-s)
        g2 = pld * (1.0 - pld)    # d pld / d(c2-s)
        dc1 = np.where(is_l, 1.0 - pl, np.where(is_d, -g1 / pd_, 0.0))
        dc2 = np.where(is_w, -g2 / pw, np.where(is_d, g2 / pd_, 0.0))
        ds = np.where(is_l, -(1.0 - pl),
                      np.where(is_d, (g1 - g2) / pd_, g2 / pw))
        return np.array([(ds * x).sum(), dc1.sum(), dc2.sum()])

    theta = np.array([0.5, -0.5, 0.5])
    eps = 1e-5
    for _ in range(60):
        g0 = gradient(theta)
        H = np.zeros((3, 3))
        for i in range(3):
            tp = theta.copy()
            tp[i] += eps
            H[:, i] = (gradient(tp) - g0) / eps
        try:
            step = np.linalg.solve(H, -g0)
        except np.linalg.LinAlgError:
            step = -0.001 * g0
        if not np.isfinite(step).all():
            break
        # damp huge steps to keep Newton stable far from optimum
        if np.abs(step).max() > 1.0:
            step = step / np.abs(step).max()
        theta = theta + step
        if np.abs(step).max() < 1e-10:
            break
    return float(theta[0]), float(theta[1]), float(theta[2])


def ol_probs(d_eff, b, c1, c2):
    s = b * (d_eff / 100.0)
    pl = 1.0 / (1.0 + math.exp(-(c1 - s)))
    pld = 1.0 / (1.0 + math.exp(-(c2 - s)))
    return (1.0 - pld, pld - pl, pl)  # win, draw, loss


def fit_goals(d_eff, hs, as_):
    """Poisson regression lambda = exp(a + b*d/100), one curve, both sides."""
    x = np.concatenate([np.asarray(d_eff), -np.asarray(d_eff)]) / 100.0
    y = np.concatenate([np.asarray(hs), np.asarray(as_)]).astype(float)
    a, b = 0.1, 0.1
    for _ in range(100):
        lam = np.exp(np.clip(a + b * x, -10, 5))
        ga = (y - lam).sum()
        gb = ((y - lam) * x).sum()
        haa = -(lam).sum()
        hab = -(lam * x).sum()
        hbb = -(lam * x * x).sum()
        H = np.array([[haa, hab], [hab, hbb]])
        step = np.linalg.solve(H, -np.array([ga, gb]))
        a, b = a + step[0], b + step[1]
        if np.abs(step).max() < 1e-12:
            break
    return float(a), float(b)


def rps3(p, w):
    o = {1.0: 0, 0.5: 1, 0.0: 2}[w]
    cum_p = cum_o = tot = 0.0
    for i in range(2):
        cum_p += p[i]
        cum_o += 1.0 if i == o else 0.0
        tot += (cum_p - cum_o) ** 2
    return tot / 2.0


def old_skeleton_probs(d_eff, mu=2.4):
    """The v1 chain (Elo We -> fixed-mu Poisson split) for comparison."""
    from model import solve_lambdas, outcome_probs
    lh, la = solve_lambdas(d_eff, mu)
    return outcome_probs(lh, la)


def main():
    download()
    rec, rating = replay()

    # sanity: replayed ratings vs eloratings.net published (2026-07-02)
    print("\nreplay sanity vs eloratings.net (2026-07-02):")
    for team, ref in [("Argentina", 2148), ("Colombia", 2004),
                      ("Australia", 1800), ("Egypt", 1742), ("Ghana", 1575)]:
        print(f"  {team:<10} replay={rating.get(team, float('nan')):7.0f}"
              f"  published={ref}")

    comp = rec[(rec.k >= 40) & (rec.date >= FIT_FROM)]
    fit_set = comp[comp.date < VAL_FROM]
    val_set = comp[comp.date >= VAL_FROM]
    print(f"\ncompetitive matches 1990+: {len(comp)} "
          f"(fit {len(fit_set)}, validate {len(val_set)})")

    b, c1, c2 = fit_ordered_logit(fit_set.d_eff.values, fit_set.w.values)
    print(f"ordered logit (fit<=2017): b={b:.4f} c1={c1:.4f} c2={c2:.4f}")

    # out-of-time validation: RPS on 2018+
    rps_new = np.mean([rps3(ol_probs(d, b, c1, c2), w)
                       for d, w in zip(val_set.d_eff, val_set.w)])
    rps_old = np.mean([rps3(old_skeleton_probs(d), w)
                       for d, w in zip(val_set.d_eff, val_set.w)])
    base = (np.mean(fit_set.w == 1.0), np.mean(fit_set.w == 0.5),
            np.mean(fit_set.w == 0.0))
    rps_base = np.mean([rps3(base, w) for w in val_set.w])
    print(f"validation RPS 2018+ (lower better): "
          f"calibrated={rps_new:.4f}  old-skeleton={rps_old:.4f}  "
          f"constant-baseline={rps_base:.4f}")

    # refit on the full competitive window, fit goals curve
    b, c1, c2 = fit_ordered_logit(comp.d_eff.values, comp.w.values)
    ga, gb = fit_goals(comp.d_eff.values, comp.hs.values, comp.as_.values)
    print(f"final params (1990+): ol b={b:.4f} c1={c1:.4f} c2={c2:.4f} | "
          f"goals a={ga:.4f} b={gb:.4f} "
          f"(lambda at d=0: {math.exp(ga):.2f})")

    calib = {
        "ol_b": b, "ol_c1": c1, "ol_c2": c2,
        "g_a": ga, "g_b": gb,
        "meta": {
            "source": URL,
            "fitted_on": f"competitive internationals {FIT_FROM}+ "
                         f"(K>=40), n={len(comp)}",
            "fitted_at": str(date.today()),
            "validation_2018plus_rps": {
                "calibrated": round(float(rps_new), 4),
                "old_skeleton": round(float(rps_old), 4),
                "constant_baseline": round(float(rps_base), 4)},
            "home_adv_elo": HOME_ADV,
            "known_limits": [
                "scores include extra time for some knockout matches "
                "(dataset limitation) — slight draw-rate deflation",
                "replayed Elo, not eloratings.net's exact seeds; see sanity "
                "print for scale agreement"],
        },
    }
    OUT.write_text(json.dumps(calib, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")

    print("\ncontext check -- pilot inputs under new M (neutral venue):")
    for name, d in [("AUS-EGY (adj +113)", 113), ("AUS-EGY (raw +58)", 58),
                    ("ARG-CPV (+526)", 526), ("COL-GHA (+439)", 439)]:
        w, dr, l = ol_probs(d, b, c1, c2)
        lh, la = math.exp(ga + gb * d / 100), math.exp(ga - gb * d / 100)
        print(f"  {name:<20} W/D/L = {w:.3f}/{dr:.3f}/{l:.3f}   "
              f"lambdas = {lh:.2f}/{la:.2f}")


if __name__ == "__main__":
    main()
