"""Structured evidence claims (Architecture Review v3 section 4.4).

Evidence stops being prose. Before this schema, a scouting subagent could
write a paragraph of team-news prose that quietly buried a market-derived
number (the m97 failure, 2026-07-07: "Kalshi France 62%" reached the
analyst inside odds_exposure text). A structured claim with typed,
required fields — including an explicit `absorption` judgment and its
reason — forces every claim through the same mechanical gate before it
reaches the analyst or the J runner (see `validate_bundle`).

Each claim is one atomic assertion: `{claim, category, source, source_tier,
access_time, absorption, absorption_reason, ...optional fields}`. This
mirrors the Architecture Review v3 section 4.4 shape:
`{claim, category, source, source_tier, pub_time, access_time, direction,
magnitude_prior, absorption(+reason), expires}`, plus a post-match audit
field `materialized` (yes/no/opposite) added at settlement time (the m96
lesson — "mechanism didn't materialize but direction was right" — turned
from narrative into a field, section 4.4: "累積下來就是「證據類型 x 兌現率」表").
"""
from datetime import datetime


# Evidence categories a claim can belong to. "user_observation" covers
# the owner's own first-hand notes (e.g. watching a presser); "form_xg" covers
# recent-form / xG-style analytical read-outs; "other" is the escape hatch
# for anything not yet worth its own category.
CATEGORIES = [
    "injury", "suspension", "lineup", "rest_travel", "tactical",
    "incentive", "illness_camp", "user_observation", "form_xg", "other",
]

# Source tiers, most to least reliable: 1a = official/first-party (club,
# federation, player themselves); 1b = first-hand media (beat reporter who
# was there); 2 = secondary analysis (a stats site's writeup of someone
# else's reporting); 3 = low-grade (forum chatter, unverified aggregator).
TIERS = ["1a", "1b", "2", "3"]

# Whether the claim's information is judged already reflected in the market
# price at access_time. "likely_unpriced" is the interesting case — it is
# the whole reason to bother recording the claim for J to weigh.
ABSORPTION = ["priced", "likely_priced", "likely_unpriced"]

# The dimensions that can move a football probability estimate. This is a
# COVERAGE checklist, orthogonal to `category` (which says what KIND of
# claim this is): a set-piece claim is category "form_xg" or "tactical",
# but it is dimension "set_pieces". Depth is coverage of this list, not
# the number of agents pointed at the match.
#
# WHY IT EXISTS (2026-09-02): the first live judgment pass ran three
# forensics agents and converged straight back to M, and its own
# adversarial critic named the reason -- "no report produced Newcastle's
# actual press intensity (PPDA, high regains, counters conceded)". A leg
# was dropped for missing data that nobody had gone to fetch. A later
# single scout filled every named gap in one pass and REFUTED the premise
# rather than merely failing to support it. So the bottleneck was never
# analyst count; it was an evidence bundle with silent holes, and silent
# is the operative word -- nothing in the system could tell "we checked
# and there is nothing" apart from "nobody looked".
DIMENSIONS = [
    "availability",        # injuries, suspensions, confirmed/likely XI
    "squad_composition",   # transfers in/out WITH the departed player's actual output
    "rest_travel",         # days since last match, midweek load, journey
    "press_style",         # PPDA / OPPDA, high regains, counters conceded, block height
    "set_pieces",          # set-piece xG for and against, corners, aerial duels
    "phase_xg",            # xG split by open play / set piece / counter / penalty
    "regime",              # managerial change and how much evidence exists under it
    "referee",             # appointment and their record
    "venue_conditions",    # pitch, closed stands, weather
    "incentive",           # fixture context, rotation pressure, motivation
]

_DIRECTIONS = ("home", "away", "none")
_MATERIALIZED = ("yes", "no", "opposite")


def _parse_iso(value):
    """Return a datetime if `value` is an ISO-8601 string, else None.

    Accepts a trailing "Z" (not handled by fromisoformat on all builds)
    by normalizing it to "+00:00" first.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_iso(value):
    return _parse_iso(value) is not None


def _require_nonempty_str(d, key, errors):
    """Check `d[key]` is present and a non-empty string; append an error
    string to `errors` otherwise. Returns the value (or None on failure)."""
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        errors.append(f"{key}: required non-empty string, got {v!r}")
        return None
    return v


def validate_claim(claim, categories=CATEGORIES, require_dimension=False):
    """Validate one evidence claim dict. Returns a list of error strings
    (empty list = valid). Never raises — callers (bundle assembly, J
    runner input gate) need a report, not an exception, since a bundle of
    N claims wants to see every claim's problems in one pass.

    `categories` defaults to the module CATEGORIES (WC2026's list, which
    includes the national-team-specific "illness_camp") so every existing
    caller is unaffected; a club-competition caller should pass its own
    category list rather than inherit a national-team concept it has no
    use for (research/audit/final_report.md flags this coupling)."""
    errors = []
    if not isinstance(claim, dict):
        return ["claim record must be a dict"]

    _require_nonempty_str(claim, "claim", errors)

    category = claim.get("category")
    if category not in categories:
        errors.append(f"category: must be one of {categories}, got {category!r}")

    _require_nonempty_str(claim, "source", errors)

    source_tier = claim.get("source_tier")
    if source_tier not in TIERS:
        errors.append(f"source_tier: must be one of {TIERS}, got {source_tier!r}")

    access_time = claim.get("access_time")
    if not _is_iso(access_time):
        errors.append(f"access_time: required ISO-parseable string, got {access_time!r}")

    absorption = claim.get("absorption")
    if absorption not in ABSORPTION:
        errors.append(f"absorption: must be one of {ABSORPTION}, got {absorption!r}")

    _require_nonempty_str(claim, "absorption_reason", errors)

    # --- optional fields: only type/value-checked when present ---
    if claim.get("pub_time") is not None:
        if not _is_iso(claim.get("pub_time")):
            errors.append(f"pub_time: must be ISO-parseable string when present, got {claim.get('pub_time')!r}")

    if claim.get("direction") is not None:
        if claim.get("direction") not in _DIRECTIONS:
            errors.append(f"direction: must be one of {list(_DIRECTIONS)}, got {claim.get('direction')!r}")

    if require_dimension and claim.get("dimension") is None:
        # an unlabelled claim passes every other check and is then silently
        # not counted by `coverage`, which is the gate in front of the judgment
        errors.append(f"dimension: required, one of {DIMENSIONS}")
    if claim.get("dimension") is not None:
        if claim.get("dimension") not in DIMENSIONS:
            errors.append(f"dimension: must be one of {DIMENSIONS}, got "
                          f"{claim.get('dimension')!r}")

    if claim.get("magnitude_prior") is not None:
        if not isinstance(claim.get("magnitude_prior"), str):
            errors.append("magnitude_prior: must be a string when present (value not shown)")

    if claim.get("expires") is not None:
        if not _is_iso(claim.get("expires")):
            errors.append(f"expires: must be ISO-parseable string when present, got {claim.get('expires')!r}")

    if claim.get("refs") is not None:
        refs = claim.get("refs")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            errors.append("refs: must be a list of strings when present (value not shown)")

    if claim.get("materialized") is not None:
        if claim.get("materialized") not in _MATERIALIZED:
            errors.append(f"materialized: must be one of {list(_MATERIALIZED)}, got {claim.get('materialized')!r}")

    return errors


def coverage(claims, dimensions=DIMENSIONS):
    """Which dimensions this bundle actually covers, and which are silent.

    Returns {"covered": {dim: n}, "silent": [dim, ...]}. A dimension is
    COVERED by any claim tagged with it -- including a negative finding
    ("this metric is not published anywhere; I checked A, B, C"), which is
    a real result and stops the next analyst repeating the same search. A
    SILENT dimension is one nobody has said anything about, and it is the
    only failure mode here: it is indistinguishable, downstream, from a
    dimension that was checked and found empty.

    This is DECISION_PROTOCOL section 4 applied to evidence: an absence has to be
    declared, with where you looked, before it counts as an answer.
    """
    covered = {}
    for c in claims:
        d = (c or {}).get("dimension")
        if d:
            covered[d] = covered.get(d, 0) + 1
    return {"covered": covered,
            "silent": [d for d in dimensions if d not in covered]}


def _firewall_module():
    """The live leak validator, `football_ai/evidence/firewall.py`.

    Until 2026-08-26 this reached into
    `history/falsification-2026/firewall.py` by sys.path — i.e. the single
    most load-bearing guardrail in the system was executing out of a
    directory frozen read-only on 2026-07-20, so a newly discovered false
    positive had nowhere legal to be fixed. The validator now lives in this
    package; the archived copy is untouched, still tests itself from the
    archived selftest, and is held to behavioural equivalence with this one
    by `evidence/firewall.py`'s own equivalence gate (differences must be
    declared in its DIVERGENCES list, not discovered later)."""
    from football_ai.evidence import firewall
    return firewall


def validate_bundle(claims, run_firewall=True, categories=CATEGORIES,
                    raw_leaks=False, require_dimension=False):
    """Validate a list of claim dicts (an evidence bundle) before it may
    reach the analyst or the J runner.

    Returns {"errors": {index: [..]}, "leaks": {index: {field: {"n", "kinds"}}}}.
    The leak entries say HOW MANY hits and of WHAT KIND, never the flagged
    number or the text around it: this result goes to the analyst, and the
    analyst must not read flagged text (CLAUDE.md, price-blind reading). Until
    2026-10-06 it carried the number and its context, so the rule "do not
    read the flagged text" could not be followed -- the refusal printed it.
    `raw_leaks=True` returns the unredacted hits, for the validator's own
    tests and for nobody else.
    A bundle with ANY error or leak must not be passed through — the
    leak validator is the mechanical enforcement of "只有市場算好的數字禁入"
    (firewall.py's docstring); a schema error means the claim can't even
    be trusted to carry a real source/tier/absorption judgment.

    When `run_firewall`, every free-text field that reaches the J prompt
    (claim, source, absorption_reason, and magnitude_prior when present) is
    run through firewall.validate_bundle_text. `source` is in that list since
    2026-10-06: j_runner.assemble_prompt writes it into the evidence line
    verbatim, and a source of "Pinnacle opened at 2.10 3.30 3.80" used to
    pass with no leak reported.

    `categories` is forwarded to `validate_claim` -- see its docstring.
    """
    result = {"errors": {}, "leaks": {}}

    for i, c in enumerate(claims):
        errs = validate_claim(c, categories=categories,
                              require_dimension=require_dimension)
        if errs:
            result["errors"][i] = errs

    if run_firewall:
        fw = _firewall_module()
        for i, c in enumerate(claims):
            if not isinstance(c, dict):
                continue
            fields = {"claim": c.get("claim"), "source": c.get("source"),
                      "absorption_reason": c.get("absorption_reason")}
            if c.get("magnitude_prior") is not None:
                fields["magnitude_prior"] = c.get("magnitude_prior")
            failures = fw.validate_bundle(fields)
            if failures:
                result["leaks"][i] = (failures if raw_leaks else
                                      {k: fw.redact(v)
                                       for k, v in failures.items()})

    return result


def refusal_text(bundle):
    """Words for refusing a bundle, from a `validate_bundle` result: claim
    indices, fields and kinds of problem. Never flagged text, never a value
    -- the scout that wrote the claim knows its own wording, and the analyst
    must not see it."""
    parts = []
    if bundle["errors"]:
        parts.append("schema errors in claim(s) "
                     + ", ".join(f"#{i}" for i in sorted(bundle["errors"]))
                     + " (run validate_bundle for the messages)")
    for i in sorted(bundle["leaks"]):
        fields = "; ".join(f"{field}: {info['kinds']}"
                           for field, info in bundle["leaks"][i].items())
        parts.append(f"claim #{i} carries a market-derived number ({fields})")
    return " | ".join(parts)


def fresh(claim, now_iso):
    """False when `claim["expires"]` is present and has passed as of
    `now_iso` (freshness decay, Architecture Review v3 section 4.4 —
    "expires 過期的 claim 在 bundle 組裝時自動標記「須重查」", the m100
    prelock "時效性資訊務必重查" rule turned into data). A claim with no
    `expires` never decays on its own."""
    expires = claim.get("expires") if isinstance(claim, dict) else None
    if not expires:
        return True
    exp_dt = _parse_iso(expires)
    now_dt = _parse_iso(now_iso)
    if exp_dt is not None and now_dt is not None:
        return not (exp_dt <= now_dt)
    # fall back to string compare if either timestamp fails to parse
    # (still correct for same-shape ISO strings, which is the only format
    # this schema accepts elsewhere)
    return not (str(expires) <= str(now_iso))


def _selfcheck():
    """Planted-fault checks, called by audit/selftest. Returns a list of
    failure strings (empty = all good)."""
    fails = []

    # --- dimension coverage (2026-09-02) --------------------------------
    # A dimension nobody tagged is SILENT, and silent is indistinguishable
    # downstream from "checked, nothing there". That difference is the whole
    # point: one is a finding, the other is an unasked question.
    cov = coverage([{"dimension": "availability"},
                    {"dimension": "availability"},
                    {"dimension": "referee"}])
    if cov["covered"] != {"availability": 2, "referee": 1}:
        fails.append(f"coverage miscounts: {cov['covered']}")
    if "press_style" not in cov["silent"] or "referee" in cov["silent"]:
        fails.append(f"silent list wrong: {cov['silent']}")
    if not coverage([])["silent"] == DIMENSIONS:
        fails.append("an empty bundle must report every dimension silent")

    # a NEGATIVE finding counts as coverage -- that is the whole reason to
    # record "not published anywhere, I checked A/B/C" as a claim
    neg = coverage([{"dimension": "press_style"}])
    if "press_style" in neg["silent"]:
        fails.append("a negative finding must count as covering its "
                     "dimension, or scouts are punished for reporting one")

    bad_dim = validate_claim({
        "claim": "x", "category": "injury", "source": "s",
        "source_tier": "1a", "access_time": "2026-09-02T00:00:00+00:00",
        "absorption": "priced", "absorption_reason": "r",
        "dimension": "not_a_real_dimension"})
    if not any("dimension" in e for e in bad_dim):
        fails.append("an unknown dimension must be rejected, not silently "
                     "accepted as a new one")

    valid = {
        "claim": "Mbappe listed as a doubt, ankle knock picked up in training",
        "category": "injury",
        "source": "L'Equipe",
        "source_tier": "1b",
        "access_time": "2026-07-08T10:00:00+00:00",
        "absorption": "likely_unpriced",
        "absorption_reason": "Not yet carried by English-language outlets that circulate into market channels",
    }

    if validate_claim(valid) != []:
        fails.append("a valid claim should pass with no errors")

    bad_category = dict(valid, category="not_a_real_category")
    if not validate_claim(bad_category):
        fails.append("bad category should be caught")

    bad_tier = dict(valid, source_tier="4")
    if not validate_claim(bad_tier):
        fails.append("bad source_tier should be caught")

    missing_reason = dict(valid)
    del missing_reason["absorption_reason"]
    if not validate_claim(missing_reason):
        fails.append("missing absorption_reason should be caught")

    bad_materialized = dict(valid, materialized="maybe")
    if not validate_claim(bad_materialized):
        fails.append("bad materialized value should be caught")

    expired = dict(valid, expires="2026-01-01T00:00:00+00:00")
    if fresh(expired, "2026-07-08T12:00:00+00:00"):
        fails.append("a claim past its expires timestamp should not be fresh")

    not_expired = dict(valid, expires="2026-12-31T00:00:00+00:00")
    if not fresh(not_expired, "2026-07-08T12:00:00+00:00"):
        fails.append("a claim before its expires timestamp should be fresh")

    leak_claim = dict(valid, claim="Kalshi has France at 62% to advance")
    bundle = validate_bundle([leak_claim], run_firewall=True)
    if 0 not in bundle["leaks"]:
        fails.append("a market-derived number (Kalshi %) should surface as a leak")

    # a market number smuggled through the SOURCE field reaches the J prompt
    # verbatim and must be caught like one in the claim text
    leak_source = dict(valid, source="Pinnacle opened at 2.10 3.30 3.80 (draftkings)")
    if 0 not in validate_bundle([leak_source], run_firewall=True)["leaks"]:
        fails.append("a market price in the `source` field must surface as a "
                     "leak: source goes into the J prompt unchanged")
    # the refusal must not hand the flagged number back to the analyst
    probe = dict(valid, claim="Pinnacle opened Arsenal at 2.10 3.30 3.80, 62%")
    red = validate_bundle([probe], run_firewall=True)
    said = repr(red) + refusal_text(red)
    if any(tok in said for tok in ("2.10", "3.30", "3.80", "62", "Pinnacle")):
        fails.append(f"validate_bundle/refusal_text leaked flagged text: {said!r}")
    if "claim" not in red["leaks"].get(0, {}):
        fails.append("the redacted leak must still say WHICH field is flagged")
    raw = validate_bundle([probe], run_firewall=True, raw_leaks=True)
    if "2.10" not in repr(raw):
        fails.append("raw_leaks=True should return the unredacted hits")
    bad_mag = dict(valid, magnitude_prior=2.10)
    if "2.1" in " ".join(validate_claim(bad_mag)):
        fails.append("a schema error must not echo a field value that may be "
                     "market text")
    # `dimension` is required for a club-competition bundle: a claim without
    # one would pass and then silently not count toward coverage
    undimensioned = {k: v for k, v in valid.items() if k != "dimension"}
    if validate_claim(undimensioned, require_dimension=True) == []:
        fails.append("a claim with no dimension must be an error when required")
    if validate_claim(undimensioned) != []:
        fails.append("the default (WC2026) path must keep dimension optional")
    if not validate_bundle([undimensioned], require_dimension=True)["errors"]:
        fails.append("validate_bundle must forward require_dimension")
    clean_source = dict(valid, source="club site, official statement")
    if validate_bundle([clean_source], run_firewall=True)["leaks"].get(0):
        fails.append("an ordinary source name must not be flagged as a leak")

    # Multi-competition override: a club competition's category list has no
    # "illness_camp" (national-team training-camp concept) -- validate_claim
    # must honor a passed-in categories list rather than always checking
    # against the WC2026 module CATEGORIES, with the WC2026 default path
    # (no categories= argument) completely unaffected.
    club_categories = ("injury", "suspension", "lineup", "rest_travel",
                        "tactical", "incentive", "user_observation",
                        "form_xg", "other")
    illness_claim = dict(valid, category="illness_camp")
    if validate_claim(illness_claim) != []:
        fails.append("illness_camp should still validate under the WC2026 default categories")
    if not validate_claim(illness_claim, categories=club_categories):
        fails.append("illness_camp should be REJECTED under a club category list without it")
    if validate_claim(valid, categories=club_categories) != []:
        fails.append("a category present in both lists (injury) should validate under either")

    clean_claim = dict(valid, claim="xG 2.84 over last 5, Elo diff +404 favors the host")
    bundle2 = validate_bundle([clean_claim], run_firewall=True)
    if bundle2["leaks"].get(0):
        fails.append("analytical numbers (xG, Elo diff) must not be flagged as a leak")

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
