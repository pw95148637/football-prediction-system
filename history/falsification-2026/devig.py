"""De-vig (margin removal) for bookmaker odds.

Two methods per PROTOCOL.md section 6:
- additive: equal-margin split, used as cross-check
- shin: primary method (Shin 1993, insider-fraction model)

All functions take decimal odds and return probabilities summing to 1.
"""
import math


def implied(odds):
    return [1.0 / o for o in odds]


def devig_additive(odds):
    """Equal-margin de-vig: p_i = r_i - (R-1)/n.

    Can go non-positive for extreme longshots under a big margin; in that
    case clamp and renormalize (caller should prefer Shin there anyway).
    """
    r = implied(odds)
    big_r = sum(r)
    n = len(r)
    p = [ri - (big_r - 1.0) / n for ri in r]
    if min(p) <= 0:
        p = [max(pi, 1e-9) for pi in p]
        s = sum(p)
        p = [pi / s for pi in p]
    return p


def devig_shin(odds, z_max=0.2, tol=1e-12, iters=200):
    """Shin de-vig: assumes a fraction z of insider money.

    p_i = [sqrt(z^2 + 4(1-z) * r_i^2 / R) - z] / (2(1-z))

    Solves z in [0, z_max] by bisection so probabilities sum to 1.
    Falls back to proportional if the market has no margin, and to
    additive if the margin exceeds what Shin can absorb in range.
    """
    r = implied(odds)
    big_r = sum(r)

    def probs(z):
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * ri * ri / big_r) - z)
                / (2.0 * (1.0 - z)) for ri in r]

    def excess(z):
        return sum(probs(z)) - 1.0

    if excess(0.0) < 0:  # no margin to remove
        return [ri / big_r for ri in r]
    if excess(z_max) > 0:  # margin too large for Shin in range
        return devig_additive(odds)

    lo, hi = 0.0, z_max
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if excess(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return probs((lo + hi) / 2.0)


def devig_both(odds):
    """Returns {"shin": [...], "additive": [...], "max_diff_pp": float}."""
    s = devig_shin(odds)
    a = devig_additive(odds)
    diff = max(abs(x - y) for x, y in zip(s, a)) * 100.0
    return {"shin": s, "additive": a, "max_diff_pp": round(diff, 3)}
