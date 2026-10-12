"""Poisson match model anchored on Elo, plus market pricing and scoring metrics.

Mode A skeleton per PROTOCOL.md:
- Elo difference -> expected score We = 1/(1+10^(-d/400))
- Given a total-goals assumption mu, solve (lam_home, lam_away) with
  lam_home + lam_away = mu and Poisson expected score = We.
- Situational adjustments (injuries, rest, rotation) are applied by editing
  lambdas directly at analysis time; the lock records final lambdas + reasons.

The score matrix is exact (no Monte Carlo needed for these markets).
Known limitation, pre-registered: independent Poisson underweights low-score
dependence (no Dixon-Coles correction) — it is part of the instrument under test.
"""
import math

MAX_GOALS = 15  # covers >0.999999 of probability mass for lambdas in play


def _pois(lam, k):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_matrix(lam_h, lam_a, max_goals=MAX_GOALS):
    ph = [_pois(lam_h, i) for i in range(max_goals + 1)]
    pa = [_pois(lam_a, j) for j in range(max_goals + 1)]
    return [[ph[i] * pa[j] for j in range(max_goals + 1)]
            for i in range(max_goals + 1)]


def outcome_probs(lam_h, lam_a):
    """(home win, draw, away win) over 90 minutes, renormalized."""
    m = score_matrix(lam_h, lam_a)
    w = d = l = 0.0
    for i, row in enumerate(m):
        for j, p in enumerate(row):
            if i > j:
                w += p
            elif i == j:
                d += p
            else:
                l += p
    s = w + d + l
    return w / s, d / s, l / s


def elo_expected(elo_diff):
    """Elo expected score (win + 0.5*draw) for the first side."""
    return 1.0 / (1.0 + 10.0 ** (-elo_diff / 400.0))


def solve_lambdas(elo_diff, total_goals=2.4, tol=1e-10, iters=200):
    """Split total_goals into (lam_h, lam_a) so the Poisson expected score
    matches the Elo expectation. Bisection on lam_h (monotone increasing)."""
    target = elo_expected(elo_diff)
    lo, hi = 1e-6, total_goals - 1e-6

    def exp_score(lh):
        w, d, _ = outcome_probs(lh, total_goals - lh)
        return w + 0.5 * d

    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if exp_score(mid) < target:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    lh = (lo + hi) / 2.0
    return lh, total_goals - lh


def totals_probs(lam_h, lam_a, line):
    """(over, push, under) for a totals line. Quarter lines are split into
    the two adjacent half-unit lines, half stake each (same as ah_probs)."""
    q = round(line * 4)
    if q % 2 == 1:  # quarter line
        o1, p1, u1 = totals_probs(lam_h, lam_a, (q - 1) / 4.0)
        o2, p2, u2 = totals_probs(lam_h, lam_a, (q + 1) / 4.0)
        return (o1 + o2) / 2.0, (p1 + p2) / 2.0, (u1 + u2) / 2.0
    m = score_matrix(lam_h, lam_a)
    over = push = under = 0.0
    for i, row in enumerate(m):
        for j, p in enumerate(row):
            t = i + j
            if t > line + 1e-9:
                over += p
            elif abs(t - line) <= 1e-9:
                push += p
            else:
                under += p
    return over, push, under


def ah_probs(lam_h, lam_a, line):
    """Asian handicap on the HOME side at `line` (e.g. -0.5, +0.25, -1.0).
    Returns (win, push, lose) for the home bet. Quarter lines are split into
    the two adjacent half-unit lines, half stake each."""
    q = round(line * 4)
    if q % 2 == 1:  # quarter line
        w1, p1, l1 = ah_probs(lam_h, lam_a, (q - 1) / 4.0)
        w2, p2, l2 = ah_probs(lam_h, lam_a, (q + 1) / 4.0)
        return (w1 + w2) / 2.0, (p1 + p2) / 2.0, (l1 + l2) / 2.0
    m = score_matrix(lam_h, lam_a)
    win = push = lose = 0.0
    for i, row in enumerate(m):
        for j, p in enumerate(row):
            adj = i + line - j
            if adj > 1e-9:
                win += p
            elif adj < -1e-9:
                lose += p
            else:
                push += p
    return win, push, lose


def qualify_prob(lam_h, lam_a, pk_h=0.5):
    """P(home advances) in a knockout: win in 90', or draw then extra time
    (Poisson at lam/3 per PROTOCOL), or ET draw then penalties at pk_h."""
    w90, d90, _ = outcome_probs(lam_h, lam_a)
    wet, det, _ = outcome_probs(lam_h / 3.0, lam_a / 3.0)
    return w90 + d90 * (wet + det * pk_h)


# --- calibrated instrument M v2 (fitted by calibrate.py -> calibration.json;
#     empirical Elo-diff -> outcome mapping, market-free) ---

_CALIB = None


def load_calibration(path=None):
    global _CALIB
    import json
    from pathlib import Path
    p = Path(path) if path else Path(__file__).parent / "calibration.json"
    _CALIB = json.loads(p.read_text(encoding="utf-8"))
    return _CALIB


def _calib():
    return _CALIB if _CALIB is not None else load_calibration()


def _ol(d_eff, c):
    s = c["ol_b"] * (d_eff / 100.0)
    pl = 1.0 / (1.0 + math.exp(-(c["ol_c1"] - s)))
    pld = 1.0 / (1.0 + math.exp(-(c["ol_c2"] - s)))
    return 1.0 - pld, pld - pl, pl


def predict_1x2(d_eff, neutral=True):
    """Calibrated Elo-diff -> (win, draw, loss) for the first side.

    The fit carries a small residual first-team bias even after the +100
    home-advantage term (about +4pp at d_eff=0). On neutral venues the
    first-listed team is arbitrary, so we symmetrize with the mirrored
    match. Pass neutral=False only when d_eff includes a real home edge."""
    c = _calib()
    w, d, l = _ol(d_eff, c)
    if not neutral:
        return w, d, l
    w2, d2, l2 = _ol(-d_eff, c)
    win = (w + l2) / 2.0
    loss = (l + w2) / 2.0
    return win, 1.0 - win - loss, loss


def predict_lambdas(d_eff):
    """Calibrated goal expectations (lambda_for, lambda_against)."""
    c = _calib()
    x = d_eff / 100.0
    return math.exp(c["g_a"] + c["g_b"] * x), math.exp(c["g_a"] - c["g_b"] * x)


def qualify_prob_v2(d_eff, pk_h=0.5, neutral=True):
    """P(first side advances): 90' from the calibrated map, extra time via
    Poisson at lambda/3, penalties at pk_h."""
    w90, d90, _ = predict_1x2(d_eff, neutral)
    lh, la = predict_lambdas(d_eff)
    wet, det, _ = outcome_probs(lh / 3.0, la / 3.0)
    return w90 + d90 * (wet + det * pk_h)


# --- scoring metrics (lower is better for both) ---

def brier(probs, outcome_idx):
    """Multiclass Brier: sum over classes of (p_k - o_k)^2."""
    return sum((p - (1.0 if k == outcome_idx else 0.0)) ** 2
               for k, p in enumerate(probs))


def rps(probs, outcome_idx):
    """Ranked probability score for ordered outcomes (home/draw/away).
    Standard metric for 1X2 football forecasts."""
    k = len(probs)
    cum_p = cum_o = total = 0.0
    for i in range(k - 1):
        cum_p += probs[i]
        cum_o += 1.0 if i == outcome_idx else 0.0
        total += (cum_p - cum_o) ** 2
    return total / (k - 1)
