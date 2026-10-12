"""Dixon-Coles low-score correction for the calibrated goal model (m_dc).

WHY (Architecture Review v3 section 3.1): M's worst DOCUMENTED bug is not
the 1X2 mapping (verified twice to be at its ceiling) but the MARGIN
distribution — naive independent Poisson gives a strong favourite
P(win by 2+ | win) ~ 63% where the empirical knockout base rate is ~30%
(m98 edge-hunt, Lens 4), and its draw mass fights the market on every
tight match. Dixon-Coles (1997) perturbs only the low-score cells
(0-0, 1-0, 0-1, 1-1) with one parameter rho, leaving the marginals'
structure intact — the textbook fix, fitted here on RESULTS ONLY
(market-free by construction, same discipline as calibrate.py).

ADMISSION GATE (review section 4.2: any M-family upgrade must pass the
out-of-time harness BEFORE adoption): fit lambda-curve + rho on <=2017
competitive matches, evaluate 2018+ on
  (1) exact-score log-loss, DC vs independent Poisson (the decision metric
      — strictly better required),
  (2) draw-rate calibration (predicted vs empirical),
  (3) big-favourite tails |d_eff|>=300: P(win by 2+ | win) and P(draw),
      predicted vs empirical (the documented failure mode),
  (4) 1X2 RPS vs the ordered-logit mapping (informational non-inferiority —
      the OL stays the 1X2 instrument regardless; m_dc's customers are
      margin-sensitive markets and the trading arm).
Then refit on the full window for the adopted parameters. The gate result
is recorded in the store (kind "calibrations") and the experiments registry;
a failed gate is recorded too — a rejected upgrade is a result, not a
failure to hide.

The 1X2 instrument M v2 (ordered logit) is pre-registered for the live
WC2026 sample and is NOT touched by this module.
"""
import json
import math
import sys
from datetime import date

import numpy as np
import pandas as pd

from football_ai.core.paths import v2_archive_dir

V2_DIR = v2_archive_dir()
DATA = V2_DIR / "data" / "results.csv"
CALIB_V2 = V2_DIR / "calibration.json"

HOME_ADV = 100.0
FIT_FROM = "1990-01-01"
VAL_FROM = "2018-01-01"
MAX_GOALS = 10
RHO_RANGE = (-0.3, 0.3)


# --- Elo replay (frozen copy of wc2026/calibrate.py replay logic; the v2
# file remains authoritative for the live experiment) -------------------------

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


def replay_df(df, k_fn, home_adv=HOME_ADV, start_rating=1500.0):
    """Elo replay over an ALREADY-LOADED results frame (date/home_team/
    away_team/home_score/away_score/neutral/tournament columns) -- pure
    function, no file I/O, no WC2026-specific defaults baked in beyond
    HOME_ADV's default value (itself overridable). `k_fn(tournament)` is
    the K-factor lookup; pass a `lambda _t: 20` (or any constant) for a
    single-competition league with no tier structure to speak of.

    Generalization prompt task 3/4: this is what a new competition's own
    calibration script should call, feeding it that competition's own
    results (e.g. football_ai/data/football_data_co_uk.py's output) instead
    of re-deriving this replay loop from scratch. `replay()` below is now a
    thin WC2026-specific wrapper around this, kept byte-identical to its
    pre-refactor output (verified 2026-07-24: same 49501-row, same
    sha256-hashed result) so nothing about the archived pipeline's inputs
    changes.
    """
    df = df.dropna(subset=["home_score", "away_score"])
    df = df.sort_values("date").reset_index(drop=True)
    rating = {}
    rows = []
    for r in df.itertuples(index=False):
        eh = rating.get(r.home_team, start_rating)
        ea = rating.get(r.away_team, start_rating)
        neutral = bool(r.neutral)
        d_eff = eh - ea + (0.0 if neutral else home_adv)
        hs, as_ = int(r.home_score), int(r.away_score)
        w = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        k = k_fn(r.tournament)
        rows.append((r.date, k, d_eff, hs, as_, w))
        we = 1.0 / (1.0 + 10.0 ** (-d_eff / 400.0))
        delta = k * gd_mult(abs(hs - as_)) * (w - we)
        rating[r.home_team] = eh + delta
        rating[r.away_team] = ea - delta
    return pd.DataFrame(rows, columns=["date", "k", "d_eff", "hs", "as_",
                                       "w"])


def replay():
    return replay_df(pd.read_csv(DATA), k_factor, home_adv=HOME_ADV,
                      start_rating=1500.0)


def fit_goals(d_eff, hs, as_):
    """Poisson regression lambda = exp(a + b*d/100) — same form as
    calibrate.py, refit per window so the OOT comparison is clean."""
    x = np.concatenate([np.asarray(d_eff), -np.asarray(d_eff)]) / 100.0
    y = np.concatenate([np.asarray(hs), np.asarray(as_)]).astype(float)
    a, b = 0.1, 0.1
    for _ in range(100):
        lam = np.exp(np.clip(a + b * x, -10, 5))
        ga = (y - lam).sum()
        gb = ((y - lam) * x).sum()
        haa = -lam.sum()
        hab = -(lam * x).sum()
        hbb = -(lam * x * x).sum()
        H = np.array([[haa, hab], [hab, hbb]])
        step = np.linalg.solve(H, -np.array([ga, gb]))
        a, b = a + step[0], b + step[1]
        if np.abs(step).max() < 1e-12:
            break
    return float(a), float(b)


# --- Dixon-Coles core --------------------------------------------------------

def dc_tau(x, y, lam, mu, rho):
    """The DC low-score correction. Vectorized over arrays."""
    t = np.ones_like(lam, dtype=float) if hasattr(lam, "shape") else 1.0
    if hasattr(x, "shape"):
        t = np.ones(x.shape, dtype=float)
        t = np.where((x == 0) & (y == 0), 1.0 - lam * mu * rho, t)
        t = np.where((x == 1) & (y == 0), 1.0 + mu * rho, t)
        t = np.where((x == 0) & (y == 1), 1.0 + lam * rho, t)
        t = np.where((x == 1) & (y == 1), 1.0 - rho, t)
        return t
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lam, mu, rho, max_goals=MAX_GOALS):
    """Joint P(X=x, Y=y) grid with the DC correction. The four tau
    corrections cancel exactly, so the grid sums to the same truncated
    mass as the independent grid (checked in _selfcheck)."""
    xs = np.arange(0, max_goals + 1)
    px = np.exp(-lam) * lam ** xs / np.array(
        [math.factorial(i) for i in xs])
    py = np.exp(-mu) * mu ** xs / np.array(
        [math.factorial(i) for i in xs])
    grid = np.outer(px, py)
    grid[0, 0] *= 1.0 - lam * mu * rho
    grid[1, 0] *= 1.0 + mu * rho
    grid[0, 1] *= 1.0 + lam * rho
    grid[1, 1] *= 1.0 - rho
    return grid / grid.sum()


def outcome_probs(lam, mu, rho):
    g = score_matrix(lam, mu, rho)
    home = np.tril(g, -1).sum()
    draw = np.trace(g)
    away = np.triu(g, 1).sum()
    return home, draw, away


def fit_rho(d_eff, hs, as_, g_a, g_b):
    """1-D MLE for rho given the lambda curve, by golden-section on the
    penalized log-likelihood (taus must stay positive on every observed
    low-score cell)."""
    d = np.asarray(d_eff) / 100.0
    lam = np.exp(np.clip(g_a + g_b * d, -10, 5))
    mu = np.exp(np.clip(g_a - g_b * d, -10, 5))
    x = np.asarray(hs)
    y = np.asarray(as_)

    def nll(rho):
        t = dc_tau(x, y, lam, mu, rho)
        if (t <= 1e-9).any():
            return np.inf
        # Poisson terms are rho-free; only the tau term matters for argmin
        return -np.log(t).sum()

    lo, hi = RHO_RANGE
    gr = (math.sqrt(5) - 1) / 2
    c = hi - gr * (hi - lo)
    dd = lo + gr * (hi - lo)
    for _ in range(200):
        if nll(c) < nll(dd):
            hi = dd
        else:
            lo = c
        c = hi - gr * (hi - lo)
        dd = lo + gr * (hi - lo)
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


# --- evaluation --------------------------------------------------------------

def _score_logloss(rows, g_a, g_b, rho):
    """Mean exact-score negative log-likelihood on capped grid."""
    total = 0.0
    n = 0
    for d, h, a in rows:
        lam = math.exp(max(-10, min(5, g_a + g_b * d / 100.0)))
        mu = math.exp(max(-10, min(5, g_a - g_b * d / 100.0)))
        g = score_matrix(lam, mu, rho)
        h, a = min(h, MAX_GOALS), min(a, MAX_GOALS)
        total += -math.log(max(g[h, a], 1e-300))
        n += 1
    return total / n


def _rps3(p, w):
    o = {1.0: 0, 0.5: 1, 0.0: 2}[w]
    cum_p = cum_o = tot = 0.0
    for i in range(2):
        cum_p += p[i]
        cum_o += 1.0 if i == o else 0.0
        tot += (cum_p - cum_o) ** 2
    return tot / 2.0


def _tail_stats(rows, g_a, g_b, rho, d_min=300.0):
    """Big-favourite diagnostics on |d_eff| >= d_min: predicted vs empirical
    P(fav wins by 2+ | fav wins) and P(draw)."""
    pred_blowout, pred_draw, emp_blowout, emp_draw, fav_wins = [], [], 0, 0, 0
    n = 0
    for d, h, a in rows:
        if abs(d) < d_min:
            continue
        n += 1
        if d < 0:  # recode so the favourite is the first side
            d, h, a = -d, a, h
        lam = math.exp(max(-10, min(5, g_a + g_b * d / 100.0)))
        mu = math.exp(max(-10, min(5, g_a - g_b * d / 100.0)))
        g = score_matrix(lam, mu, rho)
        win = np.tril(g, -1).sum()
        blow = sum(g[i, j] for i in range(g.shape[0])
                   for j in range(g.shape[1]) if i - j >= 2)
        pred_blowout.append(blow / win if win > 0 else 0.0)
        pred_draw.append(np.trace(g))
        if h > a:
            fav_wins += 1
            if h - a >= 2:
                emp_blowout += 1
        elif h == a:
            emp_draw += 1
    if n == 0:
        return None
    return {
        "n": n,
        "pred_p_blowout_given_win": float(np.mean(pred_blowout)),
        "emp_p_blowout_given_win": (emp_blowout / fav_wins
                                    if fav_wins else None),
        "pred_p_draw": float(np.mean(pred_draw)),
        "emp_p_draw": emp_draw / n,
    }


def run_gate():
    """Fit + OOT gate + full-window refit. Returns the result dict; the
    caller (CLI) persists it. Decision metric: OOT exact-score log-loss,
    DC strictly better than independent Poisson (same refit lambdas)."""
    rec = replay()
    comp = rec[(rec.k >= 40) & (rec.date >= FIT_FROM)]
    fit_set = comp[comp.date < VAL_FROM]
    val_set = comp[comp.date >= VAL_FROM]

    g_a, g_b = fit_goals(fit_set.d_eff.values, fit_set.hs.values,
                         fit_set.as_.values)
    rho = fit_rho(fit_set.d_eff.values, fit_set.hs.values,
                  fit_set.as_.values, g_a, g_b)

    val_rows = list(zip(val_set.d_eff.values,
                        val_set.hs.values.astype(int),
                        val_set.as_.values.astype(int)))
    ll_dc = _score_logloss(val_rows, g_a, g_b, rho)
    ll_ind = _score_logloss(val_rows, g_a, g_b, 0.0)

    # 1X2 RPS: DC matrix vs the v2 ordered logit (informational)
    calib = json.loads(CALIB_V2.read_text(encoding="utf-8"))

    def ol_probs(d):
        s = calib["ol_b"] * (d / 100.0)
        pl = 1.0 / (1.0 + math.exp(-(calib["ol_c1"] - s)))
        pld = 1.0 / (1.0 + math.exp(-(calib["ol_c2"] - s)))
        return (1.0 - pld, pld - pl, pl)

    rps_dc, rps_ol = [], []
    for d, w in zip(val_set.d_eff.values, val_set.w.values):
        lam = math.exp(max(-10, min(5, g_a + g_b * d / 100.0)))
        mu = math.exp(max(-10, min(5, g_a - g_b * d / 100.0)))
        rps_dc.append(_rps3(outcome_probs(lam, mu, rho), w))
        rps_ol.append(_rps3(ol_probs(d), w))

    tails_dc = _tail_stats(val_rows, g_a, g_b, rho)
    tails_ind = _tail_stats(val_rows, g_a, g_b, 0.0)

    boundary = (abs(rho - RHO_RANGE[0]) < 1e-6
                or abs(rho - RHO_RANGE[1]) < 1e-6)
    adopted = ll_dc < ll_ind and not boundary

    # adopted parameters come from the FULL competitive window
    g_a_full, g_b_full = fit_goals(comp.d_eff.values, comp.hs.values,
                                   comp.as_.values)
    rho_full = fit_rho(comp.d_eff.values, comp.hs.values, comp.as_.values,
                       g_a_full, g_b_full)

    return {
        "model": "m_dc",
        "fitted_at": str(date.today()),
        "fit_window": f"{FIT_FROM}..{VAL_FROM} (n={len(fit_set)})",
        "val_window": f"{VAL_FROM}+ (n={len(val_set)})",
        "oot": {
            "rho_fit": rho,
            "score_logloss_dc": ll_dc,
            "score_logloss_independent": ll_ind,
            "rps_1x2_dc": float(np.mean(rps_dc)),
            "rps_1x2_ordered_logit": float(np.mean(rps_ol)),
            "big_favourite_tails_dc": tails_dc,
            "big_favourite_tails_independent": tails_ind,
        },
        "gate": {
            "decision": "adopted" if adopted else "rejected",
            "criterion": "OOT exact-score log-loss strictly better than "
                         "independent Poisson, rho not on search boundary",
            "rho_on_boundary": boundary,
        },
        "full_window_params": {"g_a": g_a_full, "g_b": g_b_full,
                               "rho": rho_full,
                               "n": len(comp)},
        "notes": [
            "market-free by construction: fitted on historical results only",
            "the pre-registered 1X2 instrument (ordered logit) is untouched;"
            " m_dc's customers are margin/score/totals distributions",
            "dataset limitation inherited from calibrate.py: some knockout"
            " rows include extra time (slight draw-rate deflation)",
        ],
    }


def _selfcheck():
    fails = []
    # tau corrections cancel: DC grid mass == independent grid mass
    lam, mu, rho = 1.4, 1.1, -0.08
    xs = np.arange(0, MAX_GOALS + 1)
    px = np.exp(-lam) * lam ** xs / np.array(
        [math.factorial(i) for i in xs])
    py = np.exp(-mu) * mu ** xs / np.array(
        [math.factorial(i) for i in xs])
    ind = np.outer(px, py)
    dc = ind.copy()
    dc[0, 0] *= 1.0 - lam * mu * rho
    dc[1, 0] *= 1.0 + mu * rho
    dc[0, 1] *= 1.0 + lam * rho
    dc[1, 1] *= 1.0 - rho
    if abs(dc.sum() - ind.sum()) > 1e-12:
        fails.append("DC tau corrections must cancel in total mass")
    # rho=0 reduces to independent Poisson
    h0, d0, a0 = outcome_probs(1.3, 1.1, 0.0)
    g = np.outer(np.exp(-1.3) * 1.3 ** xs / np.array(
        [math.factorial(i) for i in xs]),
        np.exp(-1.1) * 1.1 ** xs / np.array(
        [math.factorial(i) for i in xs]))
    g = g / g.sum()
    if abs(d0 - np.trace(g)) > 1e-12:
        fails.append("rho=0 must equal independent Poisson")
    # negative rho moves mass toward the diagonal's low cells (more draws)
    _, d_neg, _ = outcome_probs(1.3, 1.1, -0.1)
    if not d_neg > d0:
        fails.append("negative rho must raise draw probability")
    # seeded synthetic recovery: generate from DC(rho=-0.1), refit
    rng = np.random.default_rng(20260708)
    lam_s, mu_s = 1.3, 1.1
    grid = score_matrix(lam_s, mu_s, -0.1, max_goals=8)
    flat = grid.ravel()
    idx = rng.choice(len(flat), size=4000, p=flat / flat.sum())
    hs, as_ = np.unravel_index(idx, grid.shape)
    d0v = np.zeros(4000)  # d_eff=0 -> lambda symmetric; use g_b=0 curve
    g_a_syn = math.log(math.sqrt(lam_s * mu_s))
    rho_hat = fit_rho(d0v, hs, as_, g_a_syn, 0.0)
    if not (-0.2 < rho_hat < -0.02):
        fails.append(f"synthetic rho recovery off: {rho_hat:.4f} "
                     f"(true -0.1)")
    return fails


def main():
    if not DATA.exists():
        sys.exit(f"missing {DATA} — run "
                 f"wc2026/history/falsification-2026/calibrate.py once to "
                 f"fetch the historical results (never fabricate data)")
    res = run_gate()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    from football_ai.store import io as store_io
    store_io.append("calibrations", res, era="v3")
    try:
        from football_ai.audit import experiments
        experiments.record(
            exp_id=f"m_dc_gate_{res['fitted_at']}",
            hypothesis="Dixon-Coles rho improves the score distribution "
                       "(margin/draw cells) over independent Poisson, "
                       "out-of-time",
            change="fit rho on <=2017 competitive internationals; evaluate "
                   "2018+ exact-score log-loss + tails",
            metric="OOT exact-score log-loss (decision), big-favourite "
                   "tails + 1X2 RPS (informational)",
            result=f"dc={res['oot']['score_logloss_dc']:.5f} vs "
                   f"ind={res['oot']['score_logloss_independent']:.5f}; "
                   f"rho={res['oot']['rho_fit']:.4f}",
            decision=res["gate"]["decision"],
            notes="full-window params stored in calibrations (era v3)")
        print("experiments registry updated")
    except Exception as e:  # registry is agent-built; degrade loudly
        print(f"NOTE: experiments registry unavailable ({e}) — gate result "
              f"persisted in calibrations only")


if __name__ == "__main__":
    main()
