"""Contamination state machine (Architecture Review v3 section 4.7).

In v2, each contamination incident was adjudicated by hand and the ruling
recorded as prose precedent (r16_por_esp 2026-07-06, m97 2026-07-07, m92
2026-07-06). This module encodes those precedents as rules so arm validity
is DERIVED from recorded events, not re-argued per incident. The rules are
exactly PROTOCOL section 4 + the logged precedents — nothing new:

- "odds_before_lock"  (v1 lock later than first odds fetch — the mechanical
  timestamp audit): match still scores B1-M (section 4: contaminated
  matches count for B1), but no honest independent J can exist and the
  match is excluded from B2. [r16_por_esp precedent]
- "leak_to_analyst"   (a market-computed number reached the analyst before
  lock — self-reported or firewall-detected): M is price-blind arithmetic
  and survives; J independence is unrecoverable; B2 excluded.
  [m97 precedent]
- "post_market_j"     (the FIRST lock version carrying j is later than the
  first odds fetch — the auto-M-lock flow guard): the j is dropped from
  J scoring; the M lock itself is clean, so B1-M and B2 stand.
  [score.py contaminated_j, 2026-07-08]
- "missing"           (never locked before kickoff): no arms — post-kickoff
  locks are never honest. [m92 precedent]

The shadow-CLV derived metric follows J validity (it is built on J's gap).
"""

# progression states (informational; the arms below are what scoring uses)
STATES = ["PENDING", "M_LOCKED", "J_LOCKED", "MARKET_OPEN", "CLOSED",
          "SETTLED", "MISSING"]

TAINT_EVENTS = ["odds_before_lock", "leak_to_analyst", "post_market_j",
                "missing"]


def derive_arm_validity(events):
    """{'b1_m','b1_j','b2','shadow': bool, 'reasons': [...]} from a list of
    taint-event names. Unknown event names raise — an unclassified incident
    must be adjudicated and added here deliberately, not silently ignored."""
    for e in events:
        if e not in TAINT_EVENTS:
            raise ValueError(
                f"unknown taint event '{e}' — adjudicate it against the "
                f"PROTOCOL precedents and add it to TAINT_EVENTS explicitly")
    arms = {"b1_m": True, "b1_j": True, "b2": True, "shadow": True}
    reasons = []
    if "missing" in events:
        arms = {k: False for k in arms}
        reasons.append("missing: never locked pre-kickoff — no arms "
                       "(m92 precedent)")
        return {**arms, "reasons": reasons}
    if "odds_before_lock" in events:
        arms["b1_j"] = arms["b2"] = arms["shadow"] = False
        reasons.append("odds_before_lock: B1-M scores (section 4), no "
                       "honest independent J, B2 excluded "
                       "(r16_por_esp precedent)")
    if "leak_to_analyst" in events:
        arms["b1_j"] = arms["b2"] = arms["shadow"] = False
        reasons.append("leak_to_analyst: M is price-blind arithmetic and "
                       "survives; J independence unrecoverable; B2 "
                       "excluded (m97 precedent)")
    if "post_market_j" in events:
        arms["b1_j"] = arms["shadow"] = False
        reasons.append("post_market_j: j dropped from J scoring; clean M "
                       "lock keeps B1-M and B2 (contaminated_j audit)")
    if not events:
        reasons.append("clean")
    return {**arms, "reasons": reasons}


def events_from_lock_row(row):
    """Map a scored B1 row (score.py schema) to taint events. MISSING
    matches never have rows — callers handle those from the registry."""
    events = []
    if row.get("contaminated"):
        events.append("odds_before_lock")
    if row.get("contaminated_j"):
        events.append("post_market_j")
    return events


def _selfcheck():
    fails = []

    def expect(events, b1_m, b1_j, b2, shadow, tag):
        got = derive_arm_validity(events)
        want = {"b1_m": b1_m, "b1_j": b1_j, "b2": b2, "shadow": shadow}
        if {k: got[k] for k in want} != want:
            fails.append(f"{tag}: got { {k: got[k] for k in want} }")

    expect([], True, True, True, True, "clean match keeps all arms")
    expect(["odds_before_lock"], True, False, False, False,
           "por_esp precedent: M-only, B2 excluded")
    expect(["leak_to_analyst"], True, False, False, False,
           "m97 precedent: M-only, B2 excluded")
    expect(["missing"], False, False, False, False,
           "m92 precedent: no arms")
    expect(["post_market_j"], True, False, True, False,
           "auto-lock guard: j dropped, clean M keeps B1-M/B2")
    expect(["odds_before_lock", "leak_to_analyst"], True, False, False,
           False, "combined taints stay M-only")
    try:
        derive_arm_validity(["novel_incident"])
        fails.append("unknown taint event must raise, not pass silently")
    except ValueError:
        pass
    return fails
