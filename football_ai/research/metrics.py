"""Proper scoring metrics — frozen copy of wc2026/model.py's rps/brier.

The v2 file remains authoritative until cutover; do not diverge. Copied so
research/ never has to import the v2 tree (self-containment for cutover).
"""


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


def _selfcheck():
    fails = []
    if abs(rps([0.8, 0.1, 0.1], 0) - 0.025) > 1e-12:
        fails.append("rps known value")
    if abs(brier([1.0, 0.0, 0.0], 0)) > 1e-12:
        fails.append("brier perfect forecast")
    if not rps([0.3, 0.5, 0.2], 0) < rps([0.3, 0.2, 0.5], 0):
        fails.append("rps ordering property")
    return fails
