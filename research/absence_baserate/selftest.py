"""Sanity checks for analyze.py estimators on simulated data with known effects.

1. OLS with team-season + opp-season FE recovers planted b_att / b_def.
2. Clustered SEs are calibrated: over many simulations, SD of estimates ~ mean SE.
3. PPML recovers planted multiplicative effects.
4. OOS test: positive MSE reduction when effects are real, ~0 when they are not.
Usage: python selftest.py
"""
import numpy as np
import pandas as pd

import analyze as A


def simulate(rng, b_att, b_def, n_seasons=4, n_teams=20, mult=False):
    rows = []
    mid = 0
    for s in range(2014, 2014 + n_seasons):
        att = rng.normal(0.0, 0.35, n_teams)
        dfn = rng.normal(0.0, 0.30, n_teams)
        for h in range(n_teams):
            for a in range(n_teams):
                if h == a:
                    continue
                mid += 1
                nh = rng.binomial(10, 0.12, size=len(b_att))   # absent counts by group
                na = rng.binomial(10, 0.12, size=len(b_att))
                for t, o, home, nt, no in ((h, a, 1, nh, na), (a, h, 0, na, nh)):
                    if mult:
                        mu = np.exp(np.log(1.35) + att[t] + dfn[o] + 0.15 * home + nt @ b_att + no @ b_def)
                        y = rng.gamma(2.0, mu / 2.0)
                    else:
                        y = 1.35 + att[t] + dfn[o] + 0.25 * home + nt @ b_att + no @ b_def + rng.normal(0, 0.75)
                    r = {"match_id": mid, "season": s, "team": f"T{t}", "opp": f"T{o}", "home": home,
                         "xg_for": y}
                    for g in range(len(b_att)):
                        r[f"own|n:G{g}"] = nt[g]
                        r[f"opp|n:G{g}"] = no[g]
                    rows.append(r)
    df = pd.DataFrame(rows)
    df["ts"] = df["season"].astype(str) + ":" + df["team"]
    df["os"] = df["season"].astype(str) + ":" + df["opp"]
    zc = [c for c in df.columns if c.startswith(("own|", "opp|"))]
    return df.drop(columns=zc), df[zc]


def main():
    rng = np.random.default_rng(1)
    b_att = np.array([-0.10, 0.0, -0.05])
    b_def = np.array([0.08, 0.0, 0.03])
    est, ses = [], []
    for _ in range(40):
        tm, Z = simulate(rng, b_att, b_def)
        c, meta, _, _ = A.ols(tm, Z)
        est.append([c[f"own|n:G{g}"]["b"] for g in range(3)] + [c[f"opp|n:G{g}"]["b"] for g in range(3)])
        ses.append([c[f"own|n:G{g}"]["se"] for g in range(3)] + [c[f"opp|n:G{g}"]["se"] for g in range(3)])
    est, ses = np.array(est), np.array(ses)
    truth = np.r_[b_att, b_def]
    print("OLS truth      ", np.round(truth, 3))
    print("OLS mean est   ", np.round(est.mean(0), 3))
    print("OLS sd of est  ", np.round(est.std(0), 4))
    print("OLS mean SE    ", np.round(ses.mean(0), 4))
    assert np.all(np.abs(est.mean(0) - truth) < 3 * est.std(0) / np.sqrt(len(est)) + 0.005), "OLS biased"
    ratio = ses.mean(0) / est.std(0)
    assert np.all((ratio > 0.7) & (ratio < 1.4)), f"SE miscalibrated {ratio}"

    tm, Z = simulate(rng, np.log([0.9, 1.0, 0.95]), np.log([1.08, 1.0, 1.03]), mult=True)
    cp, _, _ = A.ppml(tm, Z)
    print("PPML pct own   ", [round(cp[f"own|n:G{g}"]["pct"], 1) for g in range(3)], "truth [-10, 0, -5]")
    print("PPML pct opp   ", [round(cp[f"opp|n:G{g}"]["pct"], 1) for g in range(3)], "truth [8, 0, 3]")
    assert abs(cp["own|n:G0"]["pct"] + 10) < 3 and abs(cp["opp|n:G0"]["pct"] - 8) < 3

    big = np.array([-0.3, 0.0, -0.2]), np.array([0.3, 0.0, 0.2])
    tm, Z = simulate(rng, *big, n_seasons=6)
    r = A.oos_test(tm, Z, [2014, 2015, 2016, 2017], [2018, 2019])
    print("OOS with real effects: dMSE", round(r["B"]["mse_reduction"], 4), "CI", np.round(r["B"]["mse_reduction_ci95"], 4))
    assert r["B"]["mse_reduction_ci95"][0] > 0
    tm, Z = simulate(rng, np.zeros(3), np.zeros(3), n_seasons=6)
    r = A.oos_test(tm, Z, [2014, 2015, 2016, 2017], [2018, 2019])
    print("OOS with null effects: dMSE", round(r["B"]["mse_reduction"], 5), "CI", np.round(r["B"]["mse_reduction_ci95"], 5))
    assert r["B"]["mse_reduction"] < 0.002
    print("SELFTEST OK")


if __name__ == "__main__":
    main()
