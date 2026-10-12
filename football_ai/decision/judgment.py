"""The 研判 (judgment) layer's per-match record — a schema, not free prose.

WHY THIS EXISTS (2026-09-03). DECISION_PROTOCOL section 4 makes one committed,
confidence-carrying lean per fixture a MANDATORY deliverable, and section 2 says
the judgment layer must use at least one capability J structurally cannot
have. Both were being honored in conversation and then written to the store
as whatever shape that session happened to type: the two existing
`judgments` rows for this era share exactly one field name with each other
beyond `match_id`. A deliverable that only exists as prose in a transcript
cannot be scored after settlement, and scoring is the only way to find out
whether this layer earns its cost. So the fields are declared here once, and
`validate_record` refuses a row missing the parts that make the lean
checkable later.

Three things are mechanically enforced that used to rest on discipline:

  - **capabilities_used** (charter section 2). A judgment that used none of the
    things J cannot do is "a J with a looser standard", and a looser
    standard is a recipe for manufacturing edge. The vocabulary below is
    the charter's own list; each entry must also say WHAT was done, so
    "I ran the numbers" cannot pass as an answer.
  - **divergence_note is required even when the judgment AGREES with J.**
    Agreement is a finding and owes a reason; a blank note where the two
    layers coincide is indistinguishable from never having compared them.
  - **the arithmetic** (CLAUDE.md principle 2). `lambda` must equal the
    product of its own components, `prob` must equal
    `judgment_probs_90[direction]`, and `stake_frac` must equal
    kappa*lambda*f_star. The layer states the judgment; this module checks
    the multiplication.

Which model wrote the judgment is on the record too (2026-10-06): `model`
for the producer and `reviewer_models` for whoever challenged it. Neither
is required and nothing restricts the choice -- `record()` stores a row
that names no model as `model: "undeclared"` and says so on stderr, so a
missing declaration is visible in the ledger and in `ops models` instead of
silently absent. The reason it matters: a ledger that mixes models without
saying which cannot attribute a difference between layers to the layer, and
a reviewer that is the same model as the producer is a weaker check than one
that is not (CLAUDE.md principle 6).

Decision support only. Like the file it replaces
(history/falsification-2026/judgment_ledger.jsonl, frozen read-only since
2026-07-20), it never feeds B1/B2 and never writes back to M or J.
"""
from football_ai.decision import sizing

# The capabilities J structurally cannot have (DECISION_PROTOCOL section 2).
# J's inputs are exactly three (M, a validated bundle, a frozen spec) in an
# isolated subprocess with no tools -- so anything requiring the project's
# own code, its result history, a second look at a scout's source, or an
# adversarial reviewer is out of its reach by construction, not by effort.
CAPABILITIES = {
    "project_code_arithmetic":
        "computed with the project's own code rather than in the head",
    "historical_result_lookup":
        "checked the real result database for comparable fixtures",
    "scout_number_verification":
        "went back to the source and re-checked a number a scout reported",
    "player_or_phase_decomposition":
        "broke the estimate down by player or by phase of play",
    "mechanism_ledger_hit_rate":
        "cited this project's own hit rate for the mechanism class",
    "adversarial_review":
        "submitted the conclusion to an adversarial reviewer",
}

OUTCOMES = ("home", "draw", "away")
CONFIDENCE = ("low", "medium", "high")
# Whether a locked J exists to diverge FROM. "foreclosed" is the
# epl2627_gw03_ips_liv case -- a price landed before J was locked, so the
# comparison is permanently unavailable and its absence is NOT random
# (Friday/Monday fixtures were systematically exposed under the old
# schedule). Recording which of the three it is keeps a missing comparison
# from later reading as an agreement.
J_STATUS = ("locked", "foreclosed", "absent")

_TOL = 1e-6

# what a row is stored with when its writer did not say which model it was
MODEL_UNDECLARED = "undeclared"


def _prob_vector_errors(probs, label):
    errors = []
    if not isinstance(probs, dict):
        return [f"{label}: must be a dict of home/draw/away"]
    missing = [k for k in OUTCOMES if k not in probs]
    if missing:
        return [f"{label}: missing {missing}"]
    for k in OUTCOMES:
        v = probs[k]
        if not isinstance(v, (int, float)) or not (0.0 < v < 1.0):
            errors.append(f"{label}.{k}: must be a probability in (0,1), got {v!r}")
    if not errors and abs(sum(probs[k] for k in OUTCOMES) - 1.0) > 1e-6:
        errors.append(f"{label}: sums to {sum(probs[k] for k in OUTCOMES)!r}, not 1.0")
    return errors


def _require_nonempty_str(rec, key, errors):
    v = rec.get(key)
    if not isinstance(v, str) or not v.strip():
        errors.append(f"{key}: required non-empty string, got {v!r}")


def validate_record(rec):
    """Return a list of error strings for one judgment record (empty = ok).

    Never raises: a caller wants every problem in one pass, the same
    contract `evidence.schema.validate_claim` follows.
    """
    errors = []
    if not isinstance(rec, dict):
        return ["judgment record must be a dict"]

    _require_nonempty_str(rec, "match_id", errors)
    _require_nonempty_str(rec, "basis", errors)

    errors += _prob_vector_errors(rec.get("judgment_probs_90"), "judgment_probs_90")

    direction = rec.get("direction")
    if direction not in OUTCOMES:
        errors.append(f"direction: must be one of {list(OUTCOMES)}, got "
                      f"{direction!r} -- charter section 4 requires a committed "
                      f"lean on every fixture, so there is no 'none'")
    if rec.get("confidence") not in CONFIDENCE:
        errors.append(f"confidence: must be one of {list(CONFIDENCE)}, got "
                      f"{rec.get('confidence')!r}")

    prob = rec.get("prob")
    if not isinstance(prob, (int, float)) or not (0.0 < prob < 1.0):
        errors.append(f"prob: must be a probability in (0,1), got {prob!r}")
    elif direction in OUTCOMES and isinstance(rec.get("judgment_probs_90"), dict):
        stated = rec["judgment_probs_90"].get(direction)
        if isinstance(stated, (int, float)) and abs(stated - prob) > _TOL:
            errors.append(
                f"prob {prob!r} does not match judgment_probs_90[{direction!r}] "
                f"= {stated!r} -- the headline lean and the distribution it "
                f"came from must be the same number")

    # --- the J comparison (charter section 2 / CLAUDE.md, delivery of 研判) ------------
    j_status = rec.get("j_status")
    if j_status not in J_STATUS:
        errors.append(f"j_status: must be one of {list(J_STATUS)}, got {j_status!r}")
    diverges = rec.get("diverges_from_j")
    if not isinstance(diverges, bool):
        errors.append(f"diverges_from_j: must be a bool, got {diverges!r}")
    elif diverges and j_status != "locked":
        errors.append(
            f"diverges_from_j=True with j_status={j_status!r}: there is no "
            f"locked J to diverge from")
    _require_nonempty_str(rec, "divergence_note", errors)
    if j_status == "locked":
        errors += _prob_vector_errors(rec.get("j_probs_90"), "j_probs_90")

    # --- capabilities (charter section 2 acceptance test) ----------------------
    caps = rec.get("capabilities_used")
    if not isinstance(caps, list) or not caps:
        errors.append(
            "capabilities_used: required non-empty list -- a judgment that "
            "used nothing J structurally cannot do is a J with a looser "
            "standard, which charter section 2 sends back to be redone")
    else:
        for i, c in enumerate(caps):
            if not isinstance(c, dict):
                errors.append(f"capabilities_used[{i}]: must be a dict "
                              f"of capability and what")
                continue
            if c.get("capability") not in CAPABILITIES:
                errors.append(
                    f"capabilities_used[{i}].capability: must be one of "
                    f"{sorted(CAPABILITIES)}, got {c.get('capability')!r}")
            what = c.get("what")
            if not isinstance(what, str) or not what.strip():
                errors.append(
                    f"capabilities_used[{i}].what: required non-empty string "
                    f"-- naming the capability without saying what was "
                    f"actually done is not evidence that it was used")

    # --- sizing arithmetic, when present ---------------------------------
    lam = rec.get("lambda")
    if lam is not None:
        if not isinstance(lam, (int, float)) or not (0.0 <= lam <= 1.0):
            errors.append(f"lambda: must be in [0,1], got {lam!r}")
        comps = rec.get("lambda_components")
        if not isinstance(comps, dict):
            errors.append("lambda_components: required whenever lambda is "
                          "given (mech/bias/precedent/tail)")
        else:
            missing = [k for k in sizing.LAMBDA_COMPONENTS if k not in comps]
            if missing:
                errors.append(f"lambda_components: missing {missing}")
            elif isinstance(lam, (int, float)):
                product = 1.0
                for k in sizing.LAMBDA_COMPONENTS:
                    product *= comps[k]
                if abs(product - lam) > 1e-9:
                    errors.append(
                        f"lambda {lam!r} is not the product of its own "
                        f"components ({product!r}) -- the layer supplies the "
                        f"judgment, the code does the multiplication")

    f_star = rec.get("f_star")
    if f_star is not None and (not isinstance(f_star, (int, float)) or f_star < 0):
        errors.append(f"f_star: must be a non-negative number when present, "
                      f"got {f_star!r} (kelly_fraction clamps a losing edge "
                      f"to 0, it never goes negative)")

    stake = rec.get("stake_frac")
    if stake is not None:
        kappa = rec.get("kappa", sizing.KAPPA)
        if not isinstance(stake, (int, float)) or stake < 0:
            errors.append(f"stake_frac: must be a non-negative number, got {stake!r}")
        elif isinstance(lam, (int, float)) and isinstance(f_star, (int, float)):
            expected = kappa * lam * f_star
            if abs(expected - stake) > 1e-9:
                errors.append(
                    f"stake_frac {stake!r} != kappa*lambda*f_star "
                    f"({expected!r}) -- STAKING.md section 1.3")

    # --- a lean that places no stake owes the abstention note (charter
    # section 4): what is missing, what would flip it, when it is knowable
    if not (isinstance(stake, (int, float)) and stake > 0):
        ab = rec.get("abstention")
        if not isinstance(ab, dict) or not all(
                isinstance(ab.get(k), str) and ab[k].strip()
                for k in ("missing", "flips_if", "known_by")):
            errors.append(
                "abstention: a judgment with no stake needs {missing, flips_if, "
                "known_by}, each a non-empty string -- charter section 4: not "
                "betting owes the same proof as betting")

    if rec.get("outcome") is not None and rec.get("outcome") not in OUTCOMES:
        errors.append(f"outcome: must be one of {list(OUTCOMES)} or None "
                      f"(filled at settlement), got {rec.get('outcome')!r}")

    # --- a declared coverage waiver (see `record`) ------------------------
    if "coverage_waiver" in rec:
        _require_nonempty_str(rec, "coverage_waiver", errors)
        if rec.get("confidence") != "low":
            errors.append(
                "coverage_waiver: a judgment written without complete evidence "
                "coverage may not claim more than confidence 'low' -- it is "
                "built on evidence the instrument knows it is missing")

    # --- which model(s), when stated (never required; see module doc) -----
    if "model" in rec:
        _require_nonempty_str(rec, "model", errors)
    if "reviewer_models" in rec:
        rms = rec.get("reviewer_models")
        if not isinstance(rms, list) or not rms or not all(
                isinstance(m, str) and m.strip() for m in rms):
            errors.append(f"reviewer_models: must be a non-empty list of model "
                          f"names (omit the field when nobody reviewed), got {rms!r}")

    return errors


def rank_key(rec):
    """The lambda*f_star ordering the runbook calls for.

    NOT expected value, and NOT how good the mechanism story sounds: a
    confident read on a mechanism class with no track record and no positive
    edge against the real price belongs below a modest one that has both.
    A record with no price yet sorts last (None -> -1.0) rather than being
    silently dropped."""
    lam, f_star = rec.get("lambda"), rec.get("f_star")
    if lam is None or f_star is None:
        return -1.0
    return lam * f_star


def rank(records):
    """Records ordered by lambda*f_star, highest first (stable within ties)."""
    return sorted(records, key=lambda r: -rank_key(r))


def _coverage_problem(rec, era):
    """Why the evidence stored for this fixture does not license a judgment,
    or None. A declared `coverage_waiver` is the one explicit way past it
    (`validate_record` checks the waiver is stated and the confidence low)."""
    from football_ai.store import io as store_io
    if rec.get("coverage_waiver"):
        return None
    rows = [r for r in store_io.read("evidence", era=era)
            if r.get("match_id") == rec.get("match_id")]
    if not rows:
        return "no evidence bundle is stored for this fixture"
    silent = list(((rows[-1].get("data") or {}).get("coverage") or {})
                  .get("silent") or [])
    if silent:
        return (f"the stored evidence leaves {len(silent)} dimension(s) "
                f"silent: {', '.join(silent)}")
    return None


def record(rec, era, validate=True, check_coverage=True):
    """Append one validated judgment record to the store.

    Refuses an invalid record rather than writing it: a malformed row in an
    append-only ledger cannot be edited out later, only added to.

    A row that does not say which model wrote it is NOT refused (the choice
    of model is not restricted); it is stored as `model: "undeclared"` and
    reported on stderr, so the gap is part of the record.

    EVIDENCE COVERAGE IS ENFORCED (CLAUDE.md, evidence coverage; it was discipline
    until 2026-10-06 and `record` did not even look). The fixture must have a
    stored evidence bundle with no silent dimension -- a judgment on a
    bundle that skipped ten of the dimensions mostly reports "the evidence
    is thin", which is a finding about the dispatch, not about the match.
    If a bundle cannot be completed in time, say so IN the record:
    `coverage_waiver` = why, with confidence "low". The gap is then on the
    ledger, never silent. `check_coverage=False` is for importers only."""
    import sys
    from football_ai.store import io as store_io
    declared = isinstance(rec, dict) and "model" in rec
    if isinstance(rec, dict) and not declared:
        rec = dict(rec, model=MODEL_UNDECLARED)
    if validate:
        errors = validate_record(rec)
        if errors:
            raise ValueError(
                "REFUSED: judgment record is not valid and the ledger is "
                "append-only:\n  - " + "\n  - ".join(errors))
    if isinstance(rec, dict) and rec.get("mechanism_grade") == "track_record":
        # DECISION_PROTOCOL 5, track-record path: only while the
        # pre-registered measurement is active, recomputed here so the row
        # cannot carry a claim the ledger does not support
        from football_ai.decision import track_record
        sc = track_record.scorecard(era)
        if not sc["active"]:
            raise ValueError(
                f"REFUSED: mechanism_grade 'track_record' needs the pre-registered "
                f"track record to be active (n >= {sc['min_n']} and CI lower bound "
                f"> 0); now n={sc['n']}, CI={sc['ci']}")
        rec = dict(rec, track_record={k: sc[k] for k in ("n", "mean_d", "ci")})
    if check_coverage and isinstance(rec, dict):
        problem = _coverage_problem(rec, era)
        if problem:
            raise ValueError(
                f"REFUSED: {problem}. Store a complete bundle with `ops "
                f"evidence --match-id {rec.get('match_id')} --claims PATH "
                f"--store` (an honest negative finding with its sources counts "
                f"as coverage). If it cannot be completed before the deadline, "
                f"record the judgment with coverage_waiver=<why> and "
                f"confidence 'low': declared on the ledger, never silent.")
    if not declared:
        print(f"WARNING: judgment for {rec.get('match_id')!r} recorded with "
              f"model={MODEL_UNDECLARED!r}; pass model=<name> (and "
              f"reviewer_models=[...]) so the ledger can attribute it",
              file=sys.stderr)
    return store_io.append("judgments", rec, era=era,
                           match_id=rec.get("match_id"))


def _selfcheck():
    fails = []

    valid = {
        "match_id": "t_match",
        "judgment_probs_90": {"home": 0.50, "draw": 0.25, "away": 0.25},
        "direction": "home",
        "prob": 0.50,
        "confidence": "medium",
        "j_status": "locked",
        "j_probs_90": {"home": 0.48, "draw": 0.26, "away": 0.26},
        "diverges_from_j": True,
        "divergence_note": "J declined to move on the left-back absence; "
                           "judgment moves 2pp on the replacement's own numbers",
        "capabilities_used": [
            {"capability": "project_code_arithmetic",
             "what": "recomputed lambdas with decision.report.lambdas_at"}],
        "basis": "weighed the absence against the replacement's minutes",
        "abstention": {"missing": "confirmed starting eleven",
                       "flips_if": "the left-back is named in the XI",
                       "known_by": "team news, 75 minutes before kickoff"},
    }
    if validate_record(valid) != []:
        fails.append(f"a valid record should pass: {validate_record(valid)}")

    def bad(label, **over):
        r = dict(valid)
        r.update(over)
        if not validate_record(r):
            fails.append(f"{label} should be caught")

    bad("a direction outside home/draw/away", direction="none")
    bad("prob disagreeing with judgment_probs_90[direction]", prob=0.60)
    bad("a missing confidence", confidence=None)
    bad("a blank divergence note", divergence_note="   ")
    bad("an unknown j_status", j_status="maybe")
    bad("diverges_from_j=True with no locked J",
        j_status="foreclosed", j_probs_90=None)
    bad("probabilities that do not sum to 1",
        judgment_probs_90={"home": 0.5, "draw": 0.25, "away": 0.30})
    bad("a settlement outcome outside the three results", outcome="void")
    bad("no stake and no abstention note", abstention=None)
    bad("an abstention note with a blank field",
        abstention={"missing": "x", "flips_if": "y", "known_by": "  "})
    bad("an abstention note missing a field", abstention={"missing": "x", "flips_if": "y"})
    no_ab = {k: v for k, v in valid.items() if k != "abstention"}
    staked = dict(no_ab, lambda_=None)
    staked.pop("lambda_")
    staked.update({"lambda": 0.2, "lambda_components": {"mech": 0.2, "bias": 1.0,
                   "precedent": 1.0, "tail": 1.0}, "f_star": 0.1,
                   "stake_frac": 0.25 * 0.2 * 0.1})
    if validate_record(staked) != []:
        fails.append(f"a record that places a stake needs no abstention note: "
                     f"{validate_record(staked)}")

    # which model wrote it: optional, but if stated it must be a real name
    bad("a blank coverage waiver", coverage_waiver="  ", confidence="low")
    bad("a waiver on a confidence above low", coverage_waiver="evidence for "
        "two dimensions could not be found in time", confidence="high")
    if validate_record(dict(valid, coverage_waiver="two dimensions missing",
                            confidence="low")):
        fails.append("a stated waiver at confidence low is a valid record")
    bad("a blank model name", model="  ")
    bad("a non-string model name", model=7)
    bad("reviewer_models that is not a list", reviewer_models="fable")
    bad("a blank reviewer model", reviewer_models=["m-ok", ""])
    bad("an empty reviewer list", reviewer_models=[])
    stated = dict(valid, model="m-producer", reviewer_models=["m-critic"])
    if validate_record(stated) != []:
        fails.append(f"a stated model and reviewers should pass: "
                     f"{validate_record(stated)}")

    # charter section 2: the acceptance test that used to have no code behind it
    bad("an EMPTY capabilities_used list", capabilities_used=[])
    bad("a capability outside the charter's list",
        capabilities_used=[{"capability": "thought_about_it_harder",
                            "what": "at length"}])
    bad("a capability named with no account of what was done",
        capabilities_used=[{"capability": "historical_result_lookup",
                            "what": ""}])

    # a note is required even when the layers AGREE -- an unexplained
    # agreement is indistinguishable from never having compared them
    agree = dict(valid, diverges_from_j=False, divergence_note="")
    if not validate_record(agree):
        fails.append("agreement with J still owes a note, and a blank one "
                     "should be caught")

    # the arithmetic the layer is not trusted with
    comps = {"mech": 0.2, "bias": 1.0, "precedent": 0.5, "tail": 1.0}   # 0.10
    ok_lam = dict(valid, **{"lambda": 0.1, "lambda_components": comps,
                            "f_star": 0.08, "stake_frac": 0.25 * 0.1 * 0.08})
    if validate_record(ok_lam) != []:
        fails.append(f"consistent sizing arithmetic should pass: "
                     f"{validate_record(ok_lam)}")
    bad("a lambda that is not the product of its own components",
        **{"lambda": 0.5, "lambda_components": comps})
    bad("a stake_frac that is not kappa*lambda*f_star",
        **{"lambda": 0.1, "lambda_components": comps,
           "f_star": 0.08, "stake_frac": 0.05})
    bad("lambda given with no components", **{"lambda": 0.3})

    # ranking is lambda*f_star, not EV and not narrative strength
    a = dict(valid, match_id="a", **{"lambda": 0.2, "f_star": 0.10})  # 0.020
    b = dict(valid, match_id="b", **{"lambda": 0.9, "f_star": 0.05})  # 0.045
    c = dict(valid, match_id="c")                                     # no price
    order = [r["match_id"] for r in rank([a, b, c])]
    if order != ["b", "a", "c"]:
        fails.append(f"rank() should order by lambda*f_star with unpriced "
                     f"last, got {order}")

    # record() must refuse to write an invalid row (append-only: no undo)
    import tempfile
    from pathlib import Path
    from football_ai.store import io as store_io
    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            import contextlib
            import io as _io
            err = _io.StringIO()

            def evidence(mid, silent):
                store_io.append("evidence", {"match_id": mid, "claims": [],
                                             "coverage": {"covered": [], "silent": silent}},
                                era="t-era", match_id=mid)

            # coverage is enforced: no bundle, or a bundle with silent
            # dimensions, refuses; a complete one lets the record through
            for label, setup in (("no stored evidence", None),
                                 ("silent dimensions", ["referee", "incentive"])):
                if setup is not None:
                    evidence("t_match", setup)
                try:
                    record(valid, era="t-era")
                    fails.append(f"record() must refuse with {label}")
                except ValueError as e:
                    if "coverage_waiver" not in str(e):
                        fails.append(f"the refusal must say how to proceed: {e}")
            if store_io.read("judgments", era="t-era"):
                fails.append("a refused record must not have been written")
            # the declared way past it: waiver + confidence low
            with contextlib.redirect_stderr(err):
                record(dict(valid, match_id="t_waived", confidence="low",
                            coverage_waiver="referee and incentive evidence "
                                            "could not be found before the deadline"),
                       era="t-era")
            if not [r for r in store_io.read("judgments", era="t-era")
                    if r["data"]["match_id"] == "t_waived"]:
                fails.append("a declared waiver at confidence low must be accepted")
            try:
                record(dict(valid, match_id="t_waived2", confidence="high",
                            coverage_waiver="x"), era="t-era")
                fails.append("a waiver must not be a way to claim high confidence")
            except ValueError:
                pass
            evidence("t_match", [])           # now complete
            evidence("t_match2", [])
            with contextlib.redirect_stderr(err):
                record(valid, era="t-era")
            if len([r for r in store_io.read("judgments", era="t-era")
                    if r["data"]["match_id"] == "t_match"]) != 1:
                fails.append("record() did not append a valid judgment")
            row = [r["data"] for r in store_io.read("judgments", era="t-era")
                   if r["data"]["match_id"] == "t_match"][0]
            if row.get("model") != MODEL_UNDECLARED \
                    or MODEL_UNDECLARED not in err.getvalue() \
                    or "model" in valid:
                fails.append("a row naming no model must be stored as "
                             "'undeclared', warned about on stderr, and the "
                             "caller's dict left untouched")
            with contextlib.redirect_stderr(err):
                record(dict(valid, match_id="t_match2", model="m-producer",
                            reviewer_models=["m-critic"]), era="t-era")
            row2 = [r["data"] for r in store_io.read("judgments", era="t-era")
                    if r["data"]["match_id"] == "t_match2"][0]
            if row2["model"] != "m-producer" \
                    or row2["reviewer_models"] != ["m-critic"]:
                fails.append("a stated model must be stored as given")
            try:
                record(dict(valid, capabilities_used=[]), era="t-era")
                fails.append("record() must refuse an invalid row rather "
                             "than writing it to an append-only ledger")
            except ValueError:
                pass
        finally:
            store_io.STORE_DIR = old

    return fails


if __name__ == "__main__":
    result = _selfcheck()
    print("OK" if not result else f"FAILURES: {result}")
