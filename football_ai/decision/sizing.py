"""Cluster-aware fractional Kelly (Architecture Review v3 section 4.6).

ADVISORY ONLY in paper mode: the live experiment's stakes are fixed at 1u
by pre-registration (`decision/sop.py`'s STAKE_U) -- this module never
overrides that. Its purpose is (a) to inform the real-money ledger's
owner with a transparent, non-discretionary sizing reference, and
(b) to be the future v3 decision arm once paper-only status is lifted:
"paper 階段 Kelly 輸出僅記錄不執行(仍 1u,預登記),真錢層作為給 owner
的參考輸出" (section 4.6).

The confidence multiplier (lambda = lambda_mech * lambda_bias *
lambda_precedent * lambda_tail, `../../STAKING.md` sections
1.3 and 2) is implemented below: `confidence_lambda()` and its four
components, `stake_fraction()`, `below_minimum()`, `precedent_stats()`, and a
per-bet `lam` in `size_cluster()`. `kelly_fraction()` and the cluster-dedup
rule are the lower layer and are unchanged; `fraction` keeps its meaning as
kappa, the quarter-Kelly cap. The World Cup backtest (reports/kelly_staking_backtest_wc2026.md) is a
record, not a source of parameters for another league.

The cluster rule is the code form of a hard-won lesson: "相關的 edge 是
同一注——都在賭同一件事就當一個 cluster 抓總量,別疊注." Two bets that both
cash in on the same underlying market mistake are not two independent
edges to size and stack -- they are one edge wearing two tickets. Sizing
every ticket in a cluster independently would overstate the aggregate
edge and over-bet the bankroll on a single correlated view. So within a
cluster, only the single highest-EV bet gets sized; the rest are
recorded at zero with a note explaining why.
"""


def kelly_fraction(p, decimal_odds):
    """Fractional bankroll a full-Kelly bettor would stake, clamped to
    zero for a non-positive edge: max(0, (p*odds - 1) / (odds - 1)).

    Raises ValueError if odds <= 1 (no such market) or p is not strictly
    between 0 and 1 (a probability of exactly 0 or 1 breaks the formula's
    assumptions and almost certainly signals a data bug upstream).
    """
    if not (0 < p < 1):
        raise ValueError(f"p must be in the open interval (0, 1), got {p!r}")
    if decimal_odds <= 1:
        raise ValueError(f"decimal_odds must be > 1, got {decimal_odds!r}")
    edge = p * decimal_odds - 1
    denom = decimal_odds - 1
    return max(0.0, edge / denom)


def ev(p, decimal_odds):
    """Expected value per 1 unit staked at decimal odds: p*odds - 1."""
    return p * decimal_odds - 1


def min_odds(p_win, p_lose=None, margin=0.02):
    """Lowest decimal odds at which a bet still has expected value `margin`
    per unit, from the decision's own probabilities. With p_lose None the
    bet has no push (1X2): odds = (1 + margin) / p_win. With a push outcome
    (draw-no-bet, Asian 0): odds = 1 + (p_lose + margin) / p_win.

    A decision that names this price and is placed only at or above it is
    tied to the price actually taken, which is what the checklist's
    stale-price item protects (sop.CHECKLIST, timestamp_within_10min)."""
    if not (0 < p_win < 1):
        raise ValueError(f"p_win must be in (0, 1), got {p_win!r}")
    if p_lose is None:
        return (1 + margin) / p_win
    if not (0 <= p_lose < 1) or p_win + p_lose > 1 + 1e-9:
        raise ValueError(f"need 0 <= p_lose and p_win + p_lose <= 1, got {p_win}, {p_lose}")
    return 1 + (p_lose + margin) / p_win


# --- the confidence multiplier lambda (STAKING.md sections 1.3
# and 2), implemented 2026-09-03 ---------------------------------------------
#
# Until now this half of the staking spec existed only as prose, which meant the
# runbook's instruction to "rank the leans by lambda*f*" named a quantity
# nothing could compute -- so in practice the ranking either did not happen
# or happened by feel, which is the thing the spec was written to
# replace. These are pure functions of stated inputs: they hold no opinion
# about a match, they only turn one into a number the same way every time.
#
# The division of labour is deliberate. The four component functions each
# take a JUDGMENT (was a mechanism found? does this fixture sit in the shape
# where this window measured its own instrument to be worse? how many
# settled precedents does this mechanism class have?) and return a fixed
# number. Nothing here inspects a match, reads the store, or knows which
# competition is calling -- an era's own measured thresholds belong at the
# call site, next to the measurement that justifies them.

KAPPA = 0.25          # quarter-Kelly cap; the same constant `fraction` has
                      # always defaulted to in size_cluster below
MIN_STAKE_FRACTION = 0.0005   # 0.05% of bankroll; below this the position's
                              # expected value does not cover execution and
                              # the honest output is "no bet"
LAMBDA_COMPONENTS = ("mech", "bias", "precedent", "tail")

# section 2.1 -- can the market's specific error be named?
LAMBDA_MECH = {
    "verified": 1.0,            # concrete, falsifiable, passed all 9 checks
    "graded": None,             # a continuous score supplied by the caller
    "searched_not_found": 0.20,  # genuinely looked, found nothing: the gap
                                 # is unexplained, not diagnosed
    "actively_excluded": 0.10,   # a candidate mechanism was considered and
                                 # ruled out -- stronger counter-evidence
                                 # than an unexplained gap, hence lower
    "track_record": 0.30,        # no fixture-specific mechanism, but the
                                 # judgment layer's measured record against
                                 # the close is active (DECISION_PROTOCOL 5;
                                 # judgment.record refuses it otherwise)
}


def lambda_mech(grade, score=None):
    """Section 2.1. `grade` is one of LAMBDA_MECH; "graded" additionally
    requires `score` in [0,1] (reuse the edge-hunt's own confidence score
    rather than inventing a second subjective scale for the same thing)."""
    if grade not in LAMBDA_MECH:
        raise ValueError(f"grade must be one of {sorted(LAMBDA_MECH)}, got {grade!r}")
    if grade == "graded":
        if not isinstance(score, (int, float)) or not (0.0 <= score <= 1.0):
            raise ValueError(f"grade 'graded' requires score in [0,1], got {score!r}")
        return float(score)
    return LAMBDA_MECH[grade]


def gap_bias_pattern(gap_pp, threshold_pp):
    """True when this fixture sits in the band where the WINDOW'S OWN
    measurement says the instrument, not the market, is the likely error.

    The threshold is a parameter with no default on purpose. For EPL 2026-27
    it is 10pp, because that is the subset where M was measured to lose to
    the closing price by RPS +0.0225 over 970 out-of-time matches
    (`CONFIG.known_biases`) -- not because 10 is a round number, and not
    transferable to a window that has not measured its own."""
    return abs(gap_pp) >= threshold_pp


def lambda_bias(in_known_bias_pattern):
    """Section 2.2. A separate multiplicative penalty rather than a term
    folded into lambda_mech: a biased estimator's own confidence cannot be
    the evidence that it is unbiased here, so the penalty has to come from
    outside the mechanism argument."""
    return 0.10 if in_known_bias_pattern else 1.0


def lambda_precedent(n, hit_rate=None):
    """Section 2.3, from the mechanism ledger: 0.5 with no settled precedent
    for this mechanism class, otherwise clip(0.3 + 0.7*hit_rate, 0.3, 1.0).

    n=0 is the honest answer for a first-of-its-kind named mechanism -- it
    cleared the gate and has no track record, which is neither a discount
    nor a bonus."""
    if n <= 0:
        return 0.5
    if not isinstance(hit_rate, (int, float)) or not (0.0 <= hit_rate <= 1.0):
        raise ValueError(f"n={n} precedents requires hit_rate in [0,1], got {hit_rate!r}")
    return min(1.0, max(0.3, 0.3 + 0.7 * hit_rate))


def lambda_tail(p):
    """Section 2.4. Structural favourite-tail penalty on the backed side's
    own probability; applies whether or not the fixture is also in the
    measured-bias shape."""
    if not (0 < p < 1):
        raise ValueError(f"p must be in the open interval (0, 1), got {p!r}")
    if p <= 0.60:
        return 1.00
    if p <= 0.70:
        return 0.85
    return 0.70


def confidence_lambda(p, mech_grade, mech_score=None,
                      in_known_bias_pattern=False,
                      precedent_n=0, precedent_hit_rate=None):
    """lambda = lambda_mech * lambda_bias * lambda_precedent * lambda_tail.

    Returns {"lambda": float, "components": {mech, bias, precedent, tail}} --
    the components come back so a record can be re-checked against its own
    inputs later (`decision/judgment.py` does exactly that).

    Section 2.3's scoping rule is enforced here rather than left to the
    caller: with no mechanism found there is no ledger to consult, so
    lambda_precedent is 1.0 and the discount lives entirely in lambda_mech.
    Applying both would charge the same absence twice."""
    mech = lambda_mech(mech_grade, mech_score)
    found_mechanism = mech_grade in ("verified", "graded")
    components = {
        "mech": mech,
        "bias": lambda_bias(in_known_bias_pattern),
        "precedent": (lambda_precedent(precedent_n, precedent_hit_rate)
                      if found_mechanism else 1.0),
        "tail": lambda_tail(p),
    }
    value = 1.0
    for key in LAMBDA_COMPONENTS:
        value *= components[key]
    return {"lambda": value, "components": components}


def stake_fraction(lam, f_star, kappa=KAPPA):
    """STAKING.md section 1.3 formula: kappa * lambda * f_star, as a
    fraction of bankroll. Below MIN_STAKE_FRACTION the answer is no bet --
    use `below_minimum()` rather than eyeballing the decimal places."""
    return kappa * lam * f_star


def below_minimum(stake_frac):
    """True when a computed stake is too small to be worth executing."""
    return stake_frac < MIN_STAKE_FRACTION


def precedent_stats(mechanism_type, era, records=None):
    """(n, hit_rate) for one mechanism class from the mechanism ledger.

    n counts only SETTLED entries carrying a materialisation verdict --
    an entry whose mechanism has not yet been judged is not a precedent,
    and counting it as one would let an unresolved claim raise its own
    confidence. hit_rate is None when n is 0, which is what
    `lambda_precedent` expects.

    Ledger row shape: {"mechanism_types": [...], "materialized":
    "yes"|"no"|"opposite"} -- "yes" counts as a hit, "no"/"opposite" do not.
    Rows are written after settlement by `ops materialise` (MW3 was
    backfilled on 2026-10-10). A class with no judged rows returns (0, None),
    and that is the honest input to lambda_precedent -- never a borrowed
    WC2026 rate."""
    if records is None:
        from football_ai.store import io as store_io
        records = [r["data"] for r in store_io.read("mechanisms", era=era)]
    # a superseding verdict replaces the earlier one for the same factor
    # (ops.record_materialisation appends, the ledger is append-only)
    latest = {}
    for i, r in enumerate(records):
        key = ((r.get("match_id"), r.get("source"), r.get("factor_index"))
               if r.get("factor_index") is not None else ("row", i))
        latest[key] = r
    judged = [r for r in latest.values()
              if mechanism_type in (r.get("mechanism_types") or [])
              and r.get("materialized") in ("yes", "no", "opposite")]
    if not judged:
        return 0, None
    hits = sum(1 for r in judged if r.get("materialized") == "yes")
    return len(judged), hits / len(judged)


def size_cluster(bets, fraction=KAPPA, bankroll_u=100.0):
    """Size a list of candidate bets, honoring the one-cluster-one-bet
    rule. Each bet is a dict {id, cluster_id, p, odds, lam?}.

    `lam` is the per-bet confidence multiplier from `confidence_lambda()`
    (STAKING.md section 1.3: stake = kappa * lambda * f* *
    bankroll). It defaults to 1.0, so a caller that passes no lam gets
    byte-identical behaviour to before this parameter existed -- `fraction`
    keeps its meaning as kappa, the quarter-Kelly cap, and lambda is the
    thing that varies per bet. A bet whose sized stake falls below
    MIN_STAKE_FRACTION of bankroll is zeroed with its own note rather than
    recommending a position too small to be worth placing.

    Returns a list (same order as input) of
    {id, cluster_id, ev, kelly, lam, stake_u, note}:
      - within each cluster_id, only the highest-EV member is sized, at
        stake_u = fraction * lam * kelly_fraction(p, odds) * bankroll_u;
      - every other member of that cluster gets stake_u=0, note=
        "correlated cluster — sized as one bet" (they are not "wrong",
        they are the same edge as the winner, already counted once);
      - if even the best bet in a cluster (including a cluster of one)
        has ev <= 0, it gets stake_u=0, note="negative EV" instead.
    """
    # group original indices by cluster_id, preserving first-seen order
    clusters = {}
    for idx, bet in enumerate(bets):
        clusters.setdefault(bet["cluster_id"], []).append(idx)

    out = [None] * len(bets)
    for idxs in clusters.values():
        computed = []
        for idx in idxs:
            bet = bets[idx]
            bet_ev = ev(bet["p"], bet["odds"])
            bet_kelly = kelly_fraction(bet["p"], bet["odds"])
            computed.append((idx, bet_ev, bet_kelly))

        # highest-EV member of the cluster wins the (single) allowed stake;
        # ties keep the first one encountered (input order).
        winner_idx, winner_ev, _ = max(computed, key=lambda t: t[1])

        for idx, bet_ev, bet_kelly in computed:
            bet = bets[idx]
            lam = bet.get("lam", 1.0)
            if idx != winner_idx:
                stake_u, note = 0.0, "correlated cluster — sized as one bet"
            elif bet_ev <= 0:
                stake_u, note = 0.0, "negative EV"
            else:
                stake_u = fraction * lam * bet_kelly * bankroll_u
                note = ""
                if below_minimum(stake_u / bankroll_u if bankroll_u else 0.0):
                    stake_u, note = 0.0, (
                        f"below the {MIN_STAKE_FRACTION:.2%} minimum stake — "
                        f"the confidence multiplier leaves a position too "
                        f"small to be worth executing")
            out[idx] = {
                "id": bet["id"],
                "cluster_id": bet["cluster_id"],
                "ev": bet_ev,
                "kelly": bet_kelly,
                "lam": lam,
                "stake_u": stake_u,
                "note": note,
            }

    return out


def _selfcheck():
    """Planted-fault checks, called by audit/selftest. Returns a list of
    failure strings (empty = all good)."""
    fails = []
    # min_odds: 1X2 and push-bet break-evens, checked against ev()
    if abs(ev(0.25, min_odds(0.25, margin=0.0))) > 1e-12:
        fails.append("min_odds with no push must be the 1X2 break-even")
    o = min_odds(0.2485, p_lose=0.50, margin=0.0)
    if abs(0.2485 * (o - 1) - 0.50) > 1e-12:
        fails.append(f"min_odds with a push must zero win*(o-1) - lose, got {o}")
    if not min_odds(0.3, margin=0.02) > min_odds(0.3, margin=0.0):
        fails.append("a positive margin must raise the minimum price")

    # hand-computed: p=0.5, odds=2.2 -> f = (0.5*2.2 - 1) / 1.2
    #              = (1.1 - 1) / 1.2 = 0.1 / 1.2 = 0.08333...
    f = kelly_fraction(0.5, 2.2)
    expected = 0.08333333333333333
    if abs(f - expected) > 1e-9:
        fails.append(f"kelly_fraction(0.5, 2.2) expected ~{expected}, got {f}")

    # cluster of two correlated bets -> only the higher-EV one is sized
    bets = [
        {"id": "a", "cluster_id": "c1", "p": 0.55, "odds": 2.0},   # ev = 0.10
        {"id": "b", "cluster_id": "c1", "p": 0.50, "odds": 2.1},   # ev = 0.05
    ]
    sized = {r["id"]: r for r in size_cluster(bets)}
    if sized["a"]["stake_u"] <= 0:
        fails.append("cluster winner (higher EV) should receive a positive stake")
    if sized["b"]["stake_u"] != 0 or sized["b"]["note"] != "correlated cluster — sized as one bet":
        fails.append(
            "cluster loser should be zeroed with the correlated-cluster note, "
            f"got stake_u={sized['b']['stake_u']!r} note={sized['b']['note']!r}"
        )

    # negative-EV bet (own cluster of one) is zeroed with its own note
    neg_bets = [{"id": "z", "cluster_id": "c2", "p": 0.3, "odds": 2.0}]  # ev = -0.4
    sized_neg = size_cluster(neg_bets)[0]
    if sized_neg["stake_u"] != 0 or sized_neg["note"] != "negative EV":
        fails.append(
            "negative-EV bet should be zeroed with 'negative EV' note, "
            f"got stake_u={sized_neg['stake_u']!r} note={sized_neg['note']!r}"
        )

    # --- the confidence multiplier (STAKING.md section 2) ----------------
    # The four situations of STAKING.md section 3, with that table's own
    # inputs (p=0.60, D=1.90, precedents n=4 at a 0.5 hit rate). The document
    # prints these numbers, so this check is what keeps the document honest:
    # if a constant moves, the table in the spec has to move with it. (The
    # table this replaces carried two errors -- one f* reused across rows
    # whose p differed, and a rounded stake printed as exact -- which is why
    # the numbers now come from this code and not the other way round.)
    f_star = kelly_fraction(0.60, 1.90)
    if abs(f_star - 0.15556) > 1e-5:
        fails.append(f"f* for p=0.60, D=1.90 should be 0.1556, got {f_star}")
    spec = {  # name: (grade, in the bias shape, lambda, stake in % of bankroll)
        "A": ("verified", False, 0.65, 2.528),
        "B": ("searched_not_found", False, 0.20, 0.778),
        "C": ("verified", True, 0.065, 0.253),
        "D": ("searched_not_found", True, 0.020, 0.078),
    }
    res = {}
    for name, (grade, biased, lam_expected, stake_expected) in spec.items():
        r = confidence_lambda(p=0.60, mech_grade=grade,
                              in_known_bias_pattern=biased,
                              precedent_n=4, precedent_hit_rate=0.5)
        res[name] = r
        stake = stake_fraction(r["lambda"], f_star)
        if abs(r["lambda"] - lam_expected) > 1e-9 \
                or round(stake * 100, 3) != stake_expected:
            fails.append(f"STAKING.md section 3, situation {name}: expected "
                         f"lambda {lam_expected} and stake {stake_expected}%, "
                         f"got {r['lambda']} and {stake * 100:.3f}%")
        # the spec states that none of the four is stopped by the minimum
        # stake: the gate, not the threshold, is what stops a no-mechanism bet
        if below_minimum(stake):
            fails.append(f"situation {name} should clear the minimum stake, "
                         f"as STAKING.md section 3 says all four do")
    stake_a = stake_fraction(res["A"]["lambda"], f_star)
    stake_b = stake_fraction(res["B"]["lambda"], f_star)
    stake_d = stake_fraction(res["D"]["lambda"], f_star)
    if not (stake_a > 30 * stake_d):
        fails.append("situation A should outsize D by the full lambda ratio "
                     "(0.65 vs 0.02), not marginally")
    if not (stake_b > 3 * stake_d):
        fails.append("situation B should be materially larger than D")
    # a heavy favourite is charged the tail factor on top
    qt = confidence_lambda(p=0.75, mech_grade="searched_not_found",
                           in_known_bias_pattern=True)
    if abs(qt["lambda"] - 0.20 * 0.10 * 1.0 * 0.70) > 1e-12:
        fails.append(f"lambda mis-multiplied for p=0.75: {qt}")

    # section 2.3 scoping: with no mechanism there is no ledger to consult,
    # so precedent must NOT discount a second time on top of lambda_mech
    if res["B"]["components"]["precedent"] != 1.0 \
            or res["D"]["components"]["precedent"] != 1.0:
        fails.append("with no mechanism found, lambda_precedent must be 1.0 "
                     "— charging the same absence twice double-discounts")

    if lambda_precedent(0) != 0.5:
        fails.append("no settled precedent should give 0.5")
    if abs(lambda_precedent(4, 1.0) - 1.0) > 1e-12:
        fails.append("a perfect hit rate should clip to 1.0")
    if abs(lambda_precedent(4, 0.0) - 0.3) > 1e-12:
        fails.append("a zero hit rate should clip to the 0.3 floor")
    if (lambda_tail(0.60), lambda_tail(0.70), lambda_tail(0.71)) != (1.0, 0.85, 0.70):
        fails.append("lambda_tail bands are wrong at their boundaries")
    if lambda_bias(True) != 0.10 or lambda_bias(False) != 1.0:
        fails.append("lambda_bias should be 0.10 in the known pattern, else 1.0")
    if not gap_bias_pattern(10.0, 10.0) or gap_bias_pattern(9.9, 10.0):
        fails.append("gap_bias_pattern should trigger at the threshold, "
                     "inclusive, and not below it")
    if not gap_bias_pattern(-12.0, 10.0):
        fails.append("gap_bias_pattern must look at magnitude, not sign — a "
                     "large gap the other way is the same instrument warning")

    # the mechanism ledger, read rather than assumed: an entry with no
    # materialisation verdict is not yet a precedent
    ledger = [{"mechanism_types": ["rest_travel"], "materialized": "yes"},
              {"mechanism_types": ["rest_travel"], "materialized": "no"},
              {"mechanism_types": ["rest_travel"]},
              {"mechanism_types": ["injury_replacement"], "materialized": "yes"}]
    if precedent_stats("rest_travel", era="t", records=ledger) != (2, 0.5):
        fails.append(f"precedent_stats should count only judged entries, got "
                     f"{precedent_stats('rest_travel', era='t', records=ledger)}")
    if precedent_stats("nothing_like_this", era="t", records=ledger) != (0, None):
        fails.append("an unseen mechanism class should report (0, None), not "
                     "borrow another class's rate")

    # size_cluster honours a per-bet lambda, and stays byte-identical when
    # none is passed
    lam_bets = [{"id": "a", "cluster_id": "c1", "p": 0.55, "odds": 2.0},
                {"id": "b", "cluster_id": "c2", "p": 0.55, "odds": 2.0, "lam": 0.5}]
    lam_sized = {r["id"]: r for r in size_cluster(lam_bets)}
    if abs(lam_sized["b"]["stake_u"] - lam_sized["a"]["stake_u"] * 0.5) > 1e-12:
        fails.append("a bet carrying lam=0.5 should be sized at half the "
                     "stake of the identical bet with no lam")
    tiny = size_cluster([{"id": "t", "cluster_id": "c", "p": 0.55,
                          "odds": 2.0, "lam": 0.001}])[0]
    if tiny["stake_u"] != 0 or "minimum stake" not in tiny["note"]:
        fails.append(f"a sub-minimum stake should be zeroed with a stated "
                     f"reason, got {tiny}")

    # ValueError cases
    for bad_call, label in [
        (lambda: kelly_fraction(0.5, 1.0), "odds == 1"),
        (lambda: kelly_fraction(0.5, 0.5), "odds < 1"),
        (lambda: kelly_fraction(0.0, 2.0), "p == 0"),
        (lambda: kelly_fraction(1.0, 2.0), "p == 1"),
        (lambda: kelly_fraction(-0.1, 2.0), "p < 0"),
        (lambda: lambda_mech("made_up_grade"), "an unknown mechanism grade"),
        (lambda: lambda_mech("graded"), "grade 'graded' with no score"),
        (lambda: lambda_mech("graded", 1.5), "a graded score outside [0,1]"),
        (lambda: lambda_precedent(3), "precedents given with no hit rate"),
        (lambda: lambda_tail(0.0), "lambda_tail at p == 0"),
    ]:
        try:
            bad_call()
            fails.append(f"kelly_fraction should raise ValueError for {label}")
        except ValueError:
            pass

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
