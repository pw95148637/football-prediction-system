"""The pre-registered SOP gate as code (PROTOCOL.md section 3, "Phase 3 —
判定"), made deterministic so a decision can't drift by mood.

Gap thresholds (Mode A): <3pp Pass; 3-5pp Watch; >=5pp Bet candidate. But
the gap alone never clears a bet: "必答兩題:市場可能錯在哪(具體機制)/
我可能錯在哪。答不出市場錯誤的具體機制 -> gap 再大一律 Pass." A Bet
candidate additionally has to clear the SOP v4 anti-error checklist AND the
doc-10 fake-EV checks before it is allowed to become a real recommendation
-- "任一不過 -> 降級."

Stake is fixed at 1u paper, pre-registered, deliberately NOT a function of
how sure anyone feels: "禁止因把握感加碼——把握感不是本系統的輸入." That is
why `evaluate()` below takes no stake/confidence argument at all -- there
is nothing here for a confidence override to plug into.

MULTI-COMPETITION NOTE (2026-07-24): `evaluate()`/`can_add_bet()` take
optional `watch_pp`/`bet_pp`/`max_bets` overrides, defaulting to the module
constants below so every existing WC2026 caller is byte-behavior-identical
with zero changes. A new competition passes its own
`CompetitionConfig.gate` fields explicitly (see
`football_ai/competitions/epl_2026_27.py`) rather than this module ever
reading a WC2026 number for someone else's window -- these constants are
never silently reused as a cross-competition default beyond that.
"""

# The nine-item checklist, in the order PROTOCOL.md lists them: the first
# six are the SOP v4 anti-error checks, the last three are 文件10's fake-EV
# checks. Kept as an ordered list of (key, description) rather than a plain
# dict so the canonical order survives even where callers build partial
# dicts of their own.
CHECKLIST = [
    ("fame_bias",
     "Not betting on brand/name recognition rather than a computed edge (名氣偏誤)"),
    ("single_match_overreaction",
     "Not overreacting to one match's outcome (單場過度更新)"),
    ("odds_before_lock",
     "Model was built and locked before anyone looked at odds (建模前未碰過賠率)"),
    ("pulled_by_market",
     "The view was not pulled toward the market price after seeing it (未被市場拉動)"),
    ("why_wrong_if_market_right",
     "Can state concretely why I could be wrong if the market is right (若市場對,我可能錯在哪)"),
    ("sources_timestamped",
     "Every evidence source carries a source + timestamp (來源時間齊全)"),
    ("market_definition_match",
     "The market's bet definition matches the modeled proposition (市場定義一致, doc-10 fake-EV check)"),
    ("line_match",
     "The odds line used is the line actually being evaluated, not stale/mismatched (盤口線一致)"),
    # The protection is against EV computed on a stale price. Prices arrive
    # from a scheduled job, so a snapshot within 10 minutes of the decision
    # is rarely available; the decision may instead carry a minimum
    # acceptable price from sizing.min_odds and be placed only at or above
    # it, which ties the EV to the price actually taken (owner, 2026-10-10).
    ("timestamp_within_10min",
     "Odds snapshot within 10 minutes of the decision, OR the decision states a "
     "minimum acceptable price from sizing.min_odds and is placed only at or "
     "above it (價格新鮮,或以程式算出的最低可接受價下單)"),
]

CHECKLIST_KEYS = [key for key, _ in CHECKLIST]

# Gap thresholds in percentage points (PROTOCOL.md section 3, Phase 3).
WATCH_THRESHOLD_PP = 3.0
BET_THRESHOLD_PP = 5.0

# Fixed, pre-registered stake and per-match cap. STAKE_U is a constant, not
# a parameter anywhere in this module -- there is deliberately no code path
# that lets a stake be raised for a bet that merely "feels" more certain.
STAKE_U = 1.0
MAX_BETS_PER_MATCH = 2


def can_add_bet(existing_count, max_bets=MAX_BETS_PER_MATCH):
    """True if another bet may still be placed on this match under the
    per-match cap (PROTOCOL.md: "單場最多 2 筆" for WC2026; pass
    `max_bets=config.gate.max_bets_per_match` for another competition)."""
    return existing_count < max_bets


# DECISION_PROTOCOL section 4 -- what an abstention has to carry before it
# counts as an answer rather than as unfinished work.
ABSTENTION_FIELDS = ("missing", "flips_if", "known_by")


def _abstention_status(abstention):
    """Return (payload, missing_fields) for a PASS/WATCH's DECISION_PROTOCOL section 4 note.

    A gate that outputs "no nameable mechanism -> PASS" and stops has stated
    the RULE'S verdict, not the analyst's conclusion. Section 4.5 requires
    three more things: which class of information was missing, what
    observable event would flip it, and by when that event is knowable.
    Without them the output is unfalsifiable, and an unfalsifiable "no" is
    the cheapest thing any analyst can produce -- which is exactly why it
    has to be made expensive."""
    if not isinstance(abstention, dict):
        return None, list(ABSTENTION_FIELDS)
    missing = [f for f in ABSTENTION_FIELDS
               if not str(abstention.get(f, "")).strip()]
    return ({f: abstention.get(f) for f in ABSTENTION_FIELDS}
            if not missing else abstention), missing


def evaluate_for(config, gap_pp, market_error_mechanism=None, checklist=None,
                 abstention=None):
    """`evaluate()` with THIS competition's thresholds. This is the call a
    hand-made BET_CANDIDATE review must use: `evaluate()`'s own defaults are
    the WC2026 numbers (3/5pp), so calling it bare for another league can
    return BET_CANDIDATE on a gap that league's own gate (EPL: 6.5/10pp)
    would only PASS -- and the person calling it has no reason to notice."""
    return evaluate(gap_pp, market_error_mechanism, checklist,
                    watch_pp=config.gate.watch_pp, bet_pp=config.gate.bet_pp,
                    abstention=abstention)


def evaluate(gap_pp, market_error_mechanism=None, checklist=None,
             watch_pp=WATCH_THRESHOLD_PP, bet_pp=BET_THRESHOLD_PP,
             abstention=None):
    """Run the Phase-3 decision gate on one match's J-vs-market gap.

    gap_pp: the de-vigged market probability minus J's probability, in
        percentage points (sign doesn't matter here -- only magnitude).
    market_error_mechanism: a non-empty string naming the SPECIFIC
        mechanism by which the market could be wrong, or None/"" if no
        such mechanism can be named.
    checklist: dict[str, bool] covering (a subset of) CHECKLIST_KEYS.
        Only consulted once gap >= bet_pp with a mechanism named; missing
        or False entries all count as failed.
    watch_pp/bet_pp: gap thresholds in percentage points. Default to the
        WC2026 module constants so every existing caller is unaffected;
        pass `config.gate.watch_pp`/`config.gate.bet_pp` for another
        competition -- never assume these WC2026 numbers apply elsewhere.

    abstention: dict with "missing" / "flips_if" / "known_by" -- required
        by DECISION_PROTOCOL section 4 for any PASS or WATCH. Omit it and
        the verdict still returns, but carries `abstention_incomplete` and
        an INCOMPLETE reason: an abstention with no stated gap, no flipping
        observable and no deadline cannot be checked by anyone, and an
        uncheckable "no" is the cheapest output an analyst can produce.

    Returns {"decision": "PASS"|"WATCH"|"BET_CANDIDATE",
             "reasons": [...], "failed_checks": [...],
             "abstention": {...}|None, "abstention_incomplete": [...]?}.

    Deliberately takes no stake/confidence parameter -- see module
    docstring; sizing is a fixed unit, always, never adjusted here.
    """
    gap = abs(gap_pp)
    reasons = []
    note, note_missing = _abstention_status(abstention)

    def _abstain(decision, failed=None):
        """Attach the section 4 evidence to any non-BET verdict, and mark
        it INCOMPLETE when that evidence is absent -- so a bare 'PASS' can
        never render as a finished answer."""
        out = {"decision": decision, "reasons": reasons,
               "failed_checks": failed or []}
        out["abstention"] = note
        if note_missing:
            out["abstention_incomplete"] = list(note_missing)
            reasons.append(
                "INCOMPLETE under DECISION_PROTOCOL section 4: an abstention must "
                "also state " + ", ".join(note_missing)
                + " — a verdict without them is the rule's output, not the "
                  "analyst's conclusion, and is not a finished answer")
        return out

    if gap < watch_pp:
        reasons.append(
            f"gap {gap:.1f}pp is below the Watch threshold "
            f"(<{watch_pp:.1f}pp) -> Pass"
        )
        return _abstain("PASS")

    if not market_error_mechanism or not str(market_error_mechanism).strip():
        reasons.append(
            "cannot name the market's specific error -> Pass however large "
            "the gap (doctrine: 答不出市場錯誤的具體機制 -> gap 再大一律 Pass)"
        )
        return _abstain("PASS")

    if gap < bet_pp:
        reasons.append(
            f"gap {gap:.1f}pp is in the Watch band "
            f"[{watch_pp:.1f}pp, {bet_pp:.1f}pp) "
            f"even with a named mechanism ({market_error_mechanism})"
        )
        return _abstain("WATCH")

    # gap >= bet_pp with a named mechanism: only a CANDIDATE -- still
    # gated by the full anti-error + fake-EV checklist.
    reasons.append(
        f"gap {gap:.1f}pp clears the Bet-candidate threshold "
        f"(>={bet_pp:.1f}pp) with mechanism: {market_error_mechanism}"
    )

    checklist = checklist or {}
    failed_checks = [key for key in CHECKLIST_KEYS if not checklist.get(key, False)]

    if failed_checks:
        reasons.append(
            f"failed checklist item(s): {', '.join(failed_checks)} -> "
            f"downgrade to Watch (任一不過 -> 降級)"
        )
        return _abstain("WATCH", failed_checks)

    reasons.append("all anti-error and fake-EV checks passed -> Bet candidate")
    return {"decision": "BET_CANDIDATE", "reasons": reasons, "failed_checks": []}


def _selfcheck():
    """Planted-fault checks, called by audit/selftest. Returns a list of
    failure strings (empty = all good)."""
    fails = []

    # m95 case: a large gap with no named market-error mechanism must
    # still Pass -- the doctrine line is unconditional on gap size.
    r = evaluate(12.8, None)
    if r["decision"] != "PASS":
        fails.append(f"gap 12.8 with no mechanism should PASS (m95 case), got {r['decision']}")

    r = evaluate(4, "public money inflates the host nation's price")
    if r["decision"] != "WATCH":
        fails.append(f"gap 4 with a mechanism should be WATCH, got {r['decision']}")

    full_checklist = {key: True for key in CHECKLIST_KEYS}
    r = evaluate(6, "public money inflates the host nation's price", full_checklist)
    if r["decision"] != "BET_CANDIDATE":
        fails.append(f"gap 6 + mechanism + all-true checklist should be BET_CANDIDATE, got {r['decision']}")

    partial_checklist = dict(full_checklist)
    partial_checklist["sources_timestamped"] = False
    r = evaluate(6, "public money inflates the host nation's price", partial_checklist)
    if r["decision"] != "WATCH" or "sources_timestamped" not in r["failed_checks"]:
        fails.append(
            "gap 6 with one false checklist item should be WATCH with that "
            f"key in failed_checks, got decision={r['decision']!r} failed_checks={r['failed_checks']!r}"
        )

    # DECISION_PROTOCOL section 4: a bare abstention must be visibly unfinished
    bare = evaluate(12.8, None)
    if bare["decision"] != "PASS":
        fails.append("a large gap with no mechanism must still PASS")
    if sorted(bare.get("abstention_incomplete", [])) != sorted(ABSTENTION_FIELDS):
        fails.append("a PASS with no section 4 note must be flagged "
                     f"incomplete on every field, got "
                     f"{bare.get('abstention_incomplete')}")
    if not any("INCOMPLETE" in r for r in bare["reasons"]):
        fails.append("the incompleteness must appear in the reasons a human "
                     "reads, not only in a machine field")

    documented = evaluate(12.8, None, abstention={
        "missing": "no priced-information mechanism",
        "flips_if": "confirmed XI missing both first-choice centre-backs",
        "known_by": "T-75 lineup drop"})
    if documented.get("abstention_incomplete"):
        fails.append("a fully documented abstention must not be flagged "
                     f"incomplete: {documented['abstention_incomplete']}")
    if documented["abstention"]["known_by"] != "T-75 lineup drop":
        fails.append("the section 4 note must survive into the result")

    partial = evaluate(12.8, None, abstention={"missing": "x", "flips_if": ""})
    if partial.get("abstention_incomplete") != ["flips_if", "known_by"]:
        fails.append(f"partial notes must name exactly the missing fields, "
                     f"got {partial.get('abstention_incomplete')}")

    # a WATCH is an abstention too, and carries the same burden
    w = evaluate(4, "public money inflates the host nation's price")
    if w["decision"] != "WATCH" or not w.get("abstention_incomplete"):
        fails.append("WATCH must carry the same section 4 burden as PASS")

    # a BET_CANDIDATE is not an abstention and must NOT be flagged
    b = evaluate(6, "public money inflates the host nation's price",
                 {k: True for k in CHECKLIST_KEYS})
    if b["decision"] != "BET_CANDIDATE" or "abstention_incomplete" in b:
        fails.append("a bet candidate must not carry an abstention flag")

    if can_add_bet(2):
        fails.append("can_add_bet(2) should be False under MAX_BETS_PER_MATCH=2")
    if not can_add_bet(0) or not can_add_bet(1):
        fails.append("can_add_bet should be True below the per-match cap")

    # Multi-competition override: a gap of 6pp is a WC2026 BET_CANDIDATE
    # band (>=5pp default) but must stay WATCH-or-below under a stricter
    # competition's thresholds (e.g. 6.5/10pp) -- the same gap, a different
    # decision, purely from the passed-in thresholds, with WC2026's own
    # default-argument behavior above completely unaffected.
    r_default = evaluate(6, "public money inflates the host nation's price",
                          {key: True for key in CHECKLIST_KEYS})
    if r_default["decision"] != "BET_CANDIDATE":
        fails.append("sanity check: gap 6 under WC2026 defaults should still "
                      f"be BET_CANDIDATE, got {r_default['decision']}")
    r_strict = evaluate(6, "public money inflates the host nation's price",
                         {key: True for key in CHECKLIST_KEYS},
                         watch_pp=6.5, bet_pp=10.0)
    if r_strict["decision"] != "PASS":
        fails.append("gap 6 under watch_pp=6.5 override should PASS "
                      f"(below the override's watch band), got {r_strict['decision']}")
    if can_add_bet(1, max_bets=1):
        fails.append("can_add_bet(1, max_bets=1) override should be False")

    # evaluate_for takes the competition's own thresholds: the same 6pp gap is
    # a BET_CANDIDATE under the bare WC defaults and a PASS under EPL's 6.5/10
    from types import SimpleNamespace
    cfg = SimpleNamespace(gate=SimpleNamespace(watch_pp=6.5, bet_pp=10.0))
    all_ok = {k: True for k in CHECKLIST_KEYS}
    bare = evaluate(6.0, "a named mechanism", all_ok)
    scoped = evaluate_for(cfg, 6.0, "a named mechanism", all_ok)
    if bare["decision"] != "BET_CANDIDATE" or scoped["decision"] == "BET_CANDIDATE":
        fails.append(f"evaluate_for must apply the competition's thresholds: "
                     f"bare={bare['decision']} scoped={scoped['decision']}")

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
